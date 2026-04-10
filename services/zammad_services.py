import asyncio
import httpx
import os
import re
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, insert
from config.db import SessionLocal
from models.users import users
from models.notified_tickets import notified_tickets

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
                notified_at=datetime.utcnow()
            )
        )
        db.commit()
    except Exception as e:
        print(f"[POLLING] Error guardando ticket notificado: {e}")
        db.rollback()
    finally:
        db.close()


async def get_recently_closed_tickets() -> list:
    url = f"{ZAMMAD_URL}/api/v1/tickets/search"
    print(f"[POLLING] Consultando: {url}")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Token token={ZAMMAD_TOKEN}"},
            params={
                "query": "state_id:4",
                "limit": 50,
                "order_by": "desc",
                "expand": "true"
            }
        )

    if resp.status_code != 200:
        print(f"[POLLING] Error consultando Zammad: {resp.status_code} {resp.text[:200]}")
        return []

    data = resp.json()
    
    
    if isinstance(data, list):
        tickets = data
    elif isinstance(data, dict):
        ticket_ids = data.get("tickets", [])
        assets = data.get("assets", {}).get("Ticket", {})
        tickets = [assets[str(tid)] for tid in ticket_ids if str(tid) in assets]
    else:
        print(f"[POLLING] Respuesta inesperada: {type(data)}")
        return []

    print(f"[POLLING] {len(tickets)} tickets cerrados encontrados")
    return tickets


async def get_customer_email(client: httpx.AsyncClient, customer_id: int) -> str:
    resp = await client.get(
        f"{ZAMMAD_URL}/api/v1/users/{customer_id}",
        headers={"Authorization": f"Token token={ZAMMAD_TOKEN}"}
    )
    if resp.status_code == 200:
        return resp.json().get("email", "")
    return ""


async def get_ticket_close_note(client: httpx.AsyncClient, ticket_id: int) -> str:
    resp = await client.get(
        f"{ZAMMAD_URL}/api/v1/ticket_articles/by_ticket/{ticket_id}",
        headers={"Authorization": f"Token token={ZAMMAD_TOKEN}"}
    )
    if resp.status_code != 200:
        return ""

    articles = resp.json()
    if not isinstance(articles, list) or not articles:
        return ""

    last_article = articles[-1]
    body = last_article.get("body", "").strip()
    body = re.sub(r"<[^>]+>", "", body).strip()

    return body



async def notify_closed_ticket(ticket: dict):
    ticket_number = str(ticket.get("number", ""))

    if not ticket_number:
        return

    if already_notified(ticket_number):
        print(f"[POLLING] Ticket #{ticket_number} ya notificado, ignorando")
        return

    ticket_title = ticket.get("title", "Sin título")
    owner_id = ticket.get("owner_id")
    customer_id = ticket.get("customer_id")
    ticket_id = ticket.get("id")

    async with httpx.AsyncClient(timeout=10) as client:

        customer_email = ""
        if customer_id:
            customer_email = await get_customer_email(client, customer_id)
            print(f"[POLLING] Email obtenido para customer_id {customer_id}: {customer_email}")

        agent_name = "el equipo de soporte"
        if owner_id:
            resp_owner = await client.get(
                f"{ZAMMAD_URL}/api/v1/users/{owner_id}",
                headers={"Authorization": f"Token token={ZAMMAD_TOKEN}"}
            )
            if resp_owner.status_code == 200:
                owner = resp_owner.json()
                agent_name = f"{owner.get('firstname', '')} {owner.get('lastname', '')}".strip() or agent_name

        close_note = ""
        if ticket_id:
            close_note = await get_ticket_close_note(client, ticket_id)
            print(f"[POLLING] Nota de cierre: {close_note}")

        if not customer_email:
            print(f"[POLLING] Ticket #{ticket_number} sin email de cliente, ignorado")
            mark_as_notified(ticket_number)
            return

        db = SessionLocal()
        try:
            row = db.execute(
                select(users).where(users.c.email == customer_email)
            ).fetchone()
            phone_number = row.phone_number if row else None
        finally:
            db.close()

        if not phone_number:
            print(f"[POLLING] No se encontró usuario con email {customer_email}")
            mark_as_notified(ticket_number)
            return

        headers = {
            "api_access_token": API_TOKEN,
            "Content-Type": "application/json"
        }

        search = await client.get(
            f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/contacts/search",
            headers=headers,
            params={"q": phone_number, "include_contacts": "true"}
        )

        if search.status_code != 200:
            print(f"[POLLING] Error buscando contacto en Chatwoot: {search.status_code}")
            return

        results = search.json().get("payload", [])
        if not results:
            print(f"[POLLING] Contacto no encontrado en Chatwoot para {phone_number}")
            return

        contact_id = results[0].get("id")

        conv_resp = await client.get(
            f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/contacts/{contact_id}/conversations",
            headers=headers
        )

        if conv_resp.status_code != 200:
            print(f"[POLLING] Error obteniendo conversaciones: {conv_resp.status_code}")
            return

        conversations = conv_resp.json().get("payload", [])
        if not conversations:
            print(f"[POLLING] Sin conversaciones para contacto {contact_id}")
            return

        conversation_id = conversations[0].get("id")

        mensaje = await generate_message_closed(ticket_number, ticket_title, agent_name, close_note)

        msg_resp = await client.post(
            f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": mensaje, "message_type": "outgoing", "private": False}
        )

        if msg_resp.status_code in (200, 201):
            mark_as_notified(ticket_number)  
            print(f"[POLLING] Notificado - Ticket #{ticket_number} -> {phone_number}")

            resolve_resp = await client.patch(
                f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}",
                headers=headers,
                json={"status": "resolved"}
            )
            print(f"[POLLING] Conversación resuelta: {resolve_resp.status_code}")
        else:
            print(f"[POLLING] Error enviando mensaje: {msg_resp.status_code} {msg_resp.text[:200]}")
            
            
        
async def start_zammad_polling(interval: int = 60):
    print(f"[POLLING] Iniciando polling cada {interval}s")
    await asyncio.sleep(10)
    while True:
        try:
            tickets = await get_recently_closed_tickets()
            print(f"[POLLING] Revisando... {len(tickets)} tickets cerrados encontrados")
            nuevos = 0
            for ticket in tickets:
                numero = str(ticket.get("number", ""))
                if not already_notified(numero):
                    await notify_closed_ticket(ticket)
                    nuevos += 1
            if nuevos == 0:
                print(f"[POLLING] Sin tickets nuevos por procesar")
        except Exception as e:
            print(f"[POLLING] Error en ciclo: {e}")
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
                "messages": [{
                    "role": "system",
                    "content": "Eres un agente de soporte tecnico amigable y cercano que escribe mensajes por WhatsApp."
                },{
                    "role": "user",
                    "content": (
                        f"Redacta un mensaje corto por WhatsApp informando que el ticket fue resuelto "
                        f"Sin saludos como 'Hola', ve directo al mensaje. "
                        f"Usa el nombre completo del agente, no solo el primero. "
                        f"Di que 'resolvió la incidencia', nunca 'logró solucionar'. "
                        f"Sé natural, cálido y breve. Sin lenguaje corporativo.\n\n"
                        f"Ticket: #{ticket_number}\n"
                        f"Asunto: {ticket_title}\n"
                        f"Atendido por: {agent_name}\n"
                        f"Nota del técnico: {close_note or 'Sin nota adicional'}\n\n"
                        f"Máximo 4 líneas, con 1 emoji al final."
                        f"Intena que la nota adicional quede igual o muy parecida en el mensaje que crearas"
                        f"No le digas que todo listo para trabajar, ni todo en orden  ni nada por el estilo, no termines asi"
                        f"Ejeplo de como deberia sonar Tu ticket #91065 ha sido resuelto. Simon Restrepo Yepes atendió tu incidencia y solucionó el error RPC_E_SERVERFAULT (0x80010105) al iniciar sesión en la VM Ubuntu 18.04.3 en VirtualBox"
                    )
                }
                             ]
                
            }
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            return (
                f"Tu ticket #{ticket_number} fue resuelto por {agent_name}."
                f"Si tienes dudas, escribenos"
            )