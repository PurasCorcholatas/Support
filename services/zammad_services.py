import asyncio
import httpx
import os
import html
from config.logger import logger
import re
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, insert, update 
from config.db import SessionLocal
from models.users import users
from models.notified_tickets import notified_tickets
from models.conversations import conversation 
from models.tickets import tickets 
from models.bot_messages import bot_messages

ZAMMAD_TOKEN = os.getenv("ZAMMAD_HTTP_TOKEN", "")
ZAMMAD_URL = os.getenv("ZAMMAD_URL", "")
CHATWOOT_URL = os.getenv("CHATWOOT_URL", "")
ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID", "")
API_TOKEN = os.getenv("CHATWOOT_API_TOKEN", "")


def already_notified(ticket_number: str) -> bool:
    db = SessionLocal()
    try:
        row = db.execute(
            select(notified_tickets).where(
                notified_tickets.c.ticket_id == str(ticket_number)
            )
        ).fetchone()
        return row is not None
    finally:
        db.close()


def mark_as_notified(ticket_number: str):
    db = SessionLocal()
    try:
        db.execute(
            insert(notified_tickets).values(
                ticket_id=str(ticket_number),
                notified_at=datetime.now(timezone.utc)
            )
        )
        db.commit()
    except Exception as e:
        logger.error(f"[POLLING] Error guardando ticket notificado: {e}")
        db.rollback()
    finally:
        db.close()


async def get_recently_closed_tickets() -> list:
    url = f"{ZAMMAD_URL}/api/v1/tickets/search"
    logger.info(f"[POLLING] Consultando: {url}")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Token token={ZAMMAD_TOKEN}"},
            params={
                "query": "state_id:4",
                "limit": 50,
                "sort_by": "updated_at",
                "order_by": "desc",
                "expand": "true"
            }
        )

    if resp.status_code != 200:
        logger.error(f"[POLLING] Error consultando Zammad: {resp.status_code} {resp.text[:200]}")
        return []

    data = resp.json()
    
    
    if isinstance(data, list):
        tickets_list = data
    elif isinstance(data, dict):
        ticket_ids = data.get("tickets", [])
        assets = data.get("assets", {}).get("Ticket", {})
        tickets_list = [assets[str(tid)] for tid in ticket_ids if str(tid) in assets]

    else:
        logger.info(f"[POLLING] Respuesta inesperada: {type(data)}")
        return []

    logger.info(f"[POLLING] {len(tickets_list)} tickets cerrados encontrados")
    return tickets_list




async def get_ticket_close_note(client: httpx.AsyncClient, ticket_id: int) -> str:
    # Reintento: Zammad a veces indexa el estado 'closed' milisegundos antes que el artículo
    for attempt in range(3):
        resp = await client.get(
            f"{ZAMMAD_URL}/api/v1/ticket_articles/by_ticket/{ticket_id}",
            headers={"Authorization": f"Token token={ZAMMAD_TOKEN}"}
        )
        if resp.status_code != 200:
            logger.error(f"[POLLING] Error consultando artículos del ticket {ticket_id}: {resp.status_code}")
            return ""

        articles = resp.json()
        if not isinstance(articles, list) or len(articles) <= 1:
            logger.info(f"[POLLING] Intento {attempt+1}: No hay artículos suficientes para el ticket {ticket_id}")
            await asyncio.sleep(2)
            continue

        # Buscamos de la más reciente a la más antigua (saltando la primera que es la creación)
        for article in reversed(articles[1:]):
            original_body = article.get("body", "").strip()
            # Unescape HTML entities (like &nbsp;)
            original_body = html.unescape(original_body)
            body_low = original_body.lower()
            
            # Filtros
            if "imagen adjunta" in body_low:
                continue
            
            # Permitir notas cortas >= 3 caracteres
            if len(body_low) < 3:
                continue
            
            # Limpiar etiquetas HTML sobrantes
            clean_note = re.sub(r"<[^>]+>", "", original_body).strip()
            if clean_note:
                logger.info(f"[POLLING] Nota encontrada para ticket {ticket_id} (intento {attempt+1}): {clean_note[:50]}...")
                return clean_note

        await asyncio.sleep(2)

    logger.warning(f"[POLLING] No se encontró nota válida tras 3 intentos para ticket {ticket_id}")
    return ""


async def get_customer_info(client: httpx.AsyncClient, customer_id: int) -> dict:
    resp = await client.get(
        f"{ZAMMAD_URL}/api/v1/users/{customer_id}",
        headers={"Authorization": f"Token token={ZAMMAD_TOKEN}"}
    )
    if resp.status_code == 200:
        data = resp.json()
        return {
            "email": data.get("email", "") or "",
            "phone": data.get("mobile") or data.get("phone") or ""
        }
    return {"email": "", "phone": ""}


def normalize_phone(phone: str) -> str:
    """Normaliza número a formato internacional colombiano."""
    phone = re.sub(r"[^\d+]", "", phone.strip())
    if phone.startswith("+"):
        return phone
    if phone.startswith("57") and len(phone) >= 11:
        return f"+{phone}"
    if len(phone) == 10 and phone.startswith("3"):
        return f"+57{phone}"
    return phone


async def send_whatsapp_template(
    client: httpx.AsyncClient,
    conversation_id_cw: int,
    ticket_number: str,
    ticket_title: str,
    agent_name: str,
    close_note: str
) -> bool:
    
    headers_cw = {
        "api_access_token": API_TOKEN,
        "Content-Type": "application/json"
    }
    
    payload = {
        "template_params": [
            ticket_title,
            ticket_number,
            agent_name,
            close_note if close_note else "Sin nota adicional"
        ],
        "message_type": "template",
        "content": "ticket_resuelto",
        "private": False
    }
    
    resp = await client.post(
        f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id_cw}/messages",
        headers=headers_cw,
        json=payload
    )

    if resp.status_code in (200, 201):
        logger.info(f"[POLLING] Plantilla enviada correctamente para conversacion {conversation_id_cw}")
        return True 
    else:
        logger.error(f"[POLLING] Error enviando plantilla: {resp.status_code} {resp.text[:300]}")
        return False 
        
        
async def notify_closed_ticket(ticket: dict):
    ticket_number = str(ticket.get("number", ""))
    if not ticket_number or already_notified(ticket_number):
        return 
    
    ticket_id = ticket.get("id")
    ticket_title = ticket.get("title", "Sin titulo")
    owner_id = ticket.get("owner_id")
    customer_id = ticket.get("customer_id")

    if not ticket_id or not customer_id:
        logger.warning(f"[POLLING] Ticket #{ticket_number} sin id o customer_id, ignorado")
        mark_as_notified(ticket_number)
        return
    
    WHATSAPP_NOREPLY_DOMAIN = "@whatsapp.noreply"

    async with httpx.AsyncClient(timeout=10) as client:
        
        customer_info = await get_customer_info(client, customer_id)
        customer_email = customer_info["email"]
        customer_phone_zammad = customer_info["phone"]
        
        logger.info(f"[POLLING] Ticket #{ticket_number} -> email: {customer_email} | phone Zammad: {customer_phone_zammad}")
        
        phone_number = None
        is_known_user = False

        # ── Fix: email falso generado por el bot ──────────────────
        if customer_email and customer_email.endswith(WHATSAPP_NOREPLY_DOMAIN):
            extracted_phone = customer_email.replace(WHATSAPP_NOREPLY_DOMAIN, "")
            phone_number = normalize_phone(extracted_phone)
            is_known_user = True
            logger.error(f"[POLLING] Ticket #{ticket_number} -> email falso detectado, phone extraído: {phone_number}")
        else:
            # Búsqueda normal por email en BD
            if customer_email:
                db = SessionLocal()
                try:
                    row = db.execute(
                        select(users).where(users.c.email == customer_email)
                    ).fetchone()
                    if row and row.phone_number:
                        phone_number = str(row.phone_number)
                        is_known_user = True
                        logger.info(f"[POLLING] Ticket #{ticket_number} -> usuario encontrado por email en BD: {phone_number}")
                finally:
                    db.close()
            
            if not phone_number and customer_phone_zammad:
                phone_number = normalize_phone(customer_phone_zammad)
                is_known_user = False
                logger.info(f"[POLLING] Ticket #{ticket_number} -> usando phone de zammad normalizado: {phone_number}")
        
        if not phone_number:
            logger.warning(f"[POLLING] Ticket #{ticket_number} -> no se encontro numero, ignorado")
            mark_as_notified(ticket_number)
            return
        
        created_by_bot = False
        db = SessionLocal()
        try:
            ticket_row = db.execute(
                select(tickets).where(tickets.c.zammad_ticket_id == ticket_id)
            ).fetchone()
            created_by_bot = ticket_row is not None
        finally:
            db.close()
        
        logger.info(f"[POLLING] Ticket #{ticket_number} -> created_by_bot: {created_by_bot} | is_known_user: {is_known_user}")
        
        agent_name = "el equipo de soporte"
        if owner_id:
            resp_owner = await client.get(
                f"{ZAMMAD_URL}/api/v1/users/{owner_id}",
                headers={"Authorization": f"Token token={ZAMMAD_TOKEN}"}
            )
            if resp_owner.status_code == 200:
                owner = resp_owner.json()
                agent_name = f"{owner.get('firstname', '')} {owner.get('lastname', '')}".strip() or agent_name
        
        close_note = await get_ticket_close_note(client, ticket_id)
        
        if close_note:
            close_note = close_note.strip().rstrip(".")
        else:
            close_note = "Sin nota adicional"
        
        headers_cw = {
            "api_access_token": API_TOKEN,
            "Content-Type": "application/json"
        }
        
        phone_search = phone_number.lstrip("+")
        
        search = await client.get(
            f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/contacts/search",
            headers=headers_cw,
            params={"q": phone_search, "include_contacts": "true"}
        )
        
        if search.status_code != 200:
            logger.error(f"[POLLING] Error buscando contacto en Chatwoot: {search.status_code}")
            return

        results = search.json().get("payload", [])

        if not results:
            if created_by_bot:
                logger.warning(f"[POLLING] Ticket #{ticket_number} creado por bot pero sin contacto en Chatwoot, ignorado")
                mark_as_notified(ticket_number)
                return

            logger.info(f"[POLLING] Contacto no encontrado, creando en Chatwoot para {phone_number}")
            
            create_contact_resp = await client.post(
                f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/contacts",
                headers=headers_cw,
                json={
                    "name": phone_number,
                    "phone_number": phone_number,
                    "inbox_id": int(os.getenv("CHATWOOT_WHATSAPP_INBOX_ID", "0"))
                }
            )
            
            if create_contact_resp.status_code not in (200, 201):
                logger.error(f"[POLLING] No se pudo crear contacto: {create_contact_resp.status_code} {create_contact_resp.text[:200]}")
                mark_as_notified(ticket_number)
                return
            
            contact_data: dict = create_contact_resp.json()
            payload = contact_data.get("payload", {})
            contact_id = (
                contact_data.get("id")
                or payload.get("id")
                or payload.get("contact", {}).get("id")
            )

            if not contact_id:
                logger.error(f"[POLLING] Ticket #{ticket_number} -> no se pudo obtener contact_id. Data: {contact_data}")
                mark_as_notified(ticket_number)
                return

            conversations = []
        else:
            contact_id = results[0].get("id")

            conv_resp = await client.get(
                f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/contacts/{contact_id}/conversations",
                headers=headers_cw
            )
            
            if conv_resp.status_code != 200:
                logger.error(f"[POLLING] Error obteniendo conversaciones: {conv_resp.status_code}")
                return

            conversations = conv_resp.json().get("payload", [])

        if not conversations:
            if not created_by_bot:
                WHATSAPP_INBOX_ID = int(os.getenv("CHATWOOT_WHATSAPP_INBOX_ID", "0"))
                
                new_conv_resp = await client.post(
                    f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations",
                    headers=headers_cw,
                    json={
                        "contact_id": contact_id,
                        "inbox_id": WHATSAPP_INBOX_ID,
                        "status": "open"
                    }
                )
                
                if new_conv_resp.status_code not in (200, 201):
                    logger.error(f"[POLLING] No se pudo crear conversación para contacto {contact_id}: {new_conv_resp.status_code} {new_conv_resp.text[:200]}")
                    mark_as_notified(ticket_number)
                    return
                
                new_conv_data: dict = new_conv_resp.json()
                conversation_id_cw = new_conv_data.get("id")
                logger.info(f"[POLLING] Conversación nueva creada: {conversation_id_cw}")
                await asyncio.sleep(3)
            else:
                logger.warning(f"[POLLING] Ticket #{ticket_number} creado por bot pero sin conversación en Chatwoot, ignorado")
                mark_as_notified(ticket_number)
                return
        else:
            open_conv = next(
                (c for c in conversations if c.get("status") == "open"),
                conversations[0]
            )
            conversation_id_cw = open_conv.get("id")
        
        # Tickets del bot siempre usan mensaje libre (is_known_user=True)
        use_template = not created_by_bot and not is_known_user
        
        if use_template:
            logger.info(f"[POLLING] Ticket #{ticket_number} -> enviando plantilla Meta")
            rendered_content = (
                f"Hola, te contactamos desde Serviunix. Tu incidencia {ticket_title} "
                f"del ticket #{ticket_number} ha sido resuelta por {agent_name}. "
                f"{close_note} Cualquier duda adicional, con gusto te ayudamos."
            )
            payload = {
                "content": "alert_ticket",
                "message_type": "template",
                "template_params": {
                    "name": "alert_ticket",
                    "category": "UTILITY",
                    "language": "es",
                    "processed_params": {
                        "body": {
                            "1": str(ticket_title),
                            "2": str(ticket_number),
                            "3": str(agent_name),
                            "4": str(close_note)
                        }
                    }
                },
                "private": False
            }
            db_bm = SessionLocal()
            try:
                db_bm.execute(insert(bot_messages).values(content_preview="alert_ticket"[:100]))
                db_bm.commit()
            except Exception as e:
                logger.error(f"[POLLING] Error guardando bot_message: {e}")
            finally:
                db_bm.close()
                
            msg_resp = await client.post(
                f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id_cw}/messages",
                headers=headers_cw,
                json=payload
            )
            logger.error(f"[POLLING] Respuesta Chatwoot template: {msg_resp.status_code} {msg_resp.text[:500]}")
        else:
            logger.info(f"[POLLING] Ticket #{ticket_number} -> enviando mensaje libre")
            mensaje = await generate_message_closed(ticket_number, ticket_title, agent_name, close_note)
            
            db_bm = SessionLocal()
            try:
                db_bm.execute(insert(bot_messages).values(content_preview=mensaje[:100]))
                db_bm.commit()
            except Exception as e:
                logger.error(f"[POLLING] Error guardando bot_message: {e}")
            finally:
                db_bm.close()
                
            msg_resp = await client.post(
                f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id_cw}/messages",
                headers=headers_cw,
                json={"content": mensaje, "message_type": "outgoing", "private": False}
            )
        
        if msg_resp.status_code in (200, 201):
            mark_as_notified(ticket_number)
            logger.info(f"[POLLING] Notificado - Ticket #{ticket_number} -> {phone_number} ({'plantilla' if use_template else 'mensaje libre'})")

            await client.patch(
                f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id_cw}",
                headers=headers_cw,
                json={"status": "resolved"}
            )
            
            if created_by_bot:
                db = SessionLocal()
                try:
                    t_row = db.execute(
                        select(tickets).where(tickets.c.zammad_ticket_id == ticket_id)
                    ).fetchone()
                    if t_row:
                        db.execute(
                            update(conversation)
                            .where(conversation.c.id == t_row.conversation_id)
                            .values(status="closed")
                        )
                        db.commit()
                        logger.info(f"[POLLING] Conversacion cerrada en BD para ticket #{ticket_number}")
                except Exception as e:
                    logger.error(f"[POLLING] Error cerrando conversacion en BD: {e}")
                    db.rollback()
                finally:
                    db.close()
                    
            if not use_template:
                db = SessionLocal()
                try:
                    from sqlalchemy import text
                    db.execute(
                        update(users)
                        .where(users.c.phone_number == phone_number)
                        .values(
                            bot_active=True,
                            greeting_step="confirm_branch"
                        )
                    )
                    db.commit()
                    
                    db.execute(text("DELETE FROM checkpoint_writes WHERE thread_id = :tid"), {"tid": phone_number})
                    db.execute(text("DELETE FROM checkpoint_blobs WHERE thread_id = :tid"), {"tid": phone_number})
                    db.execute(text("DELETE FROM checkpoints WHERE thread_id = :tid"), {"tid": phone_number})
                    db.commit()
                    
                    logger.info(f"[POLLING] Bot reactivado y contexto limpiado para {phone_number}")
                except Exception as e:
                    logger.error(f"[POLLING] Error reactivando bot: {e}")
                    db.rollback()
                finally:
                    db.close()
        else:
            logger.error(f"[POLLING] Error enviando mensaje: {msg_resp.status_code} {msg_resp.text[:300]}")
                
                
async def start_zammad_polling(interval: int = 60):
    logger.info(f"[POLLING] Iniciando polling cada {interval}s")
    await asyncio.sleep(10)
    while True:
        try:
            ticket_list = await get_recently_closed_tickets()
            logger.info(f"[POLLING] Revisando... {len(ticket_list)} tickets cerrados encontrados")
            nuevos = 0
            for ticket in ticket_list:
                numero = str(ticket.get("number", ""))
                if not already_notified(numero):
                    await notify_closed_ticket(ticket)
                    nuevos += 1
            if nuevos == 0:
                logger.info(f"[POLLING] Sin tickets nuevos por procesar")
        except Exception as e:
            logger.error(f"[POLLING] Error en ciclo: {e}")
        await asyncio.sleep(interval)
    




async def generate_message_closed(ticket_number: str, ticket_title: str, agent_name: str, close_note: str):
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '')}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 300,
                "temperature": 0,
                "messages": [{
                    "role": "system",
                    "content": "Eres un agente de soporte tecnico amigable y cercano que escribe mensajes por WhatsApp."
                },{
                    "role": "user",
                    "content": (
                        f"Redacta un mensaje corto por WhatsApp informando que el ticket fue resuelto. "
                        f"Sin saludos como 'Hola', ve directo al mensaje. "
                        f"Usa el nombre completo del agente, no solo el primero. "
                        f"Di que 'resolvió la incidencia', nunca 'logró solucionar'. "
                        f"Sé natural, cálido y breve. Sin lenguaje corporativo.\n\n"
                        f"Ticket: #{ticket_number}\n"
                        f"Asunto: {ticket_title}\n"
                        f"Atendido por: {agent_name}\n"
                        f"Nota del técnico: {close_note if close_note else 'SIN NOTA'}\n\n"
                        f"REGLAS ESTRICTAS — CUMPLIRLAS TODAS SIN EXCEPCIÓN:\n"
                        f"- Si la nota es 'SIN NOTA': el mensaje termina después de confirmar que fue resuelto. PROHIBIDO agregar instrucciones, pasos, recomendaciones o sugerencias de ningún tipo. No inventes nada.\n"
                        f"- Si la nota tiene información: inclúyela de forma natural. Si describe acciones realizadas (ej: 'se migró el correo', 'se amplió el espacio'), menciónalas. Si tiene instrucciones (ej: 'reinicia tu equipo'), dila en imperativo.\n"
                        f"- NUNCA inventes pasos ni recomendaciones que no estén en la nota.\n"
                        f"- NO menciones imágenes adjuntas.\n"
                        f"- NO termines con 'todo listo', 'todo en orden', ni frases genéricas de cierre.\n"
                        f"- Máximo 3 líneas si no hay nota. Máximo 5 si hay nota. Un emoji al final.\n\n"
                        f"Ejemplo SIN nota: 'Tu ticket #91065 ha sido resuelto. Simon Restrepo Yepes resolvió la incidencia con el error en la VM de Ubuntu. 🙌'\n"
                        f"Ejemplo CON nota: 'Tu ticket #91065 ha sido resuelto. Simon Restrepo Yepes resolvió la incidencia. Se migró tu buzón y se amplió el espacio. Por favor valida el ingreso. 🙌'"
                    )
                }]
            }
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            return (
                f"Tu ticket #{ticket_number} fue resuelto por {agent_name}. "
                f"Si tienes dudas, escríbenos 👋"
            )


async def generate_message_closed_external(ticket_number: str, ticket_title: str, agent_name: str, close_note: str):
    hora = datetime.now(timezone(timedelta(hours=-5)))
    h = hora.hour
    if 5 <= h < 12:
        saludo = "Buenos días"
    elif 12 <= h < 18:
        saludo = "Buenas tardes"
    else:
        saludo = "Buenas noches"

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '')}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 300,
                "temperature": 0,
                "messages": [
                    {
                        "role": "system",
                        "content": "Eres un agente de soporte técnico amigable que escribe mensajes por WhatsApp."
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Redacta un mensaje corto por WhatsApp notificando que un ticket fue resuelto. "
                            f"El usuario NO conoce este canal de WhatsApp, así que preséntate brevemente. "
                            f"Empieza con '{saludo},' luego di que hablas del soporte de Serviunix. "
                            f"Luego informa que el ticket fue resuelto de forma natural y cálida. "
                            f"Usa el nombre completo del agente.\n\n"
                            f"Ticket: #{ticket_number}\n"
                            f"Asunto: {ticket_title}\n"
                            f"Atendido por: {agent_name}\n"
                            f"Nota del técnico: {close_note or 'ninguna'}\n\n"
                            f"REGLAS ESTRICTAS:\n"
                            f"- Si la nota dice 'ninguna': termina el mensaje después de confirmar que fue resuelto. NO agregues instrucciones, recomendaciones ni pasos de ningún tipo.\n"
                            f"- Si la nota tiene información o acciones realizadas: inclúyelas de forma natural en el mensaje.\n"
                            f"- NUNCA inventes pasos ni recomendaciones que no estén en la nota.\n"
                            f"- NO menciones nada de imágenes adjuntas.\n"
                            f"- Máximo 5 líneas, con 1 emoji al final.\n"
                        )
                    }
                ]
            }
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            return (
                f"{saludo}, te habla el soporte de Serviunix.\n"
                f"Tu ticket #{ticket_number} sobre \"{ticket_title}\" fue resuelto por {agent_name}.\n"
                f"Cualquier duda, con gusto te ayudamos 👋"
            )