import os
import re
import json
import asyncio
import httpx
from typing import Optional, List, Dict
from sqlalchemy import select, insert, update
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from config.db import SessionLocal
from models.users import users
from models.conversations import conversation
from models.messages import messages
from models.tickets import tickets
from models.user_memory import user_memory
import aiosmtplib
from email.mime.text import MIMEText
from services.mcp_client import get_mcp_tools
from config.logger import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from ..state import State
from ..llms import llm, llm_diagnosis, tools
from ..helpers import (
    _normalize_images, guided_response, detect_intent_simple,
    _hacer_pregunta_tecnica, _parse_ticket_result, generate_ticket_summary,
    generate_technical_details, clean_html_entities, get_or_request_email
)
from ..integrations import (
    save_conversation_memory, user_has_email, get_user_email,
    update_chatwoot_contact, detected_incident_severity,
    attach_images_to_zammad, load_servicio_values, get_servicio_value
)


PRIORIDAD_MAP = {
    "baja":     "1 baja",
    "moderada": "2 moderada",
    "alta":     "3 alta",
    "critica":  "4 critica",
}

def offer_support_options(state: State):
    messages_state = state.get("messages", [])
    last = str(messages_state[-1].content).lower().strip()

    # ── waiting_similar va primero, tiene su propia lógica ──
    if state.get("support_option_step") == "waiting_similar":
        detected = detect_intent_simple(
            last,
            {
                "confirma": "dice que sí, es el mismo problema",
                "niega": "dice que no es lo mismo, que es diferente",
                "otro": "otra cosa"
            }
        )
        if "confirma" in detected:
            return {
                "support_option_step": "waiting_choice",
                "support_option_retries": 0,
                "messages": [AIMessage(content="Ok, es el mismo.\n¿Prefieres que cree un ticket o te paso a alguien del equipo?")]
            }
        if "niega" in detected:
            return {
                "support_option_step": None,
                "support_option_retries": None,
                "intent": "diagnosis_flow",
                "diagnosis_step": 0,
                "diagnosis_history": [f"problema inicial del usuario: {last}"]
            }
        return {
            "support_option_step": "waiting_similar",
            "messages": [guided_response(
                objetivo="Preguntarle si el problema que tiene ahora es el mismo que tuvo antes.",
                contexto="No quedó claro si es el mismo problema.",
                historial=[last]
            )]
        }
    
    if state.get("support_option_step") != "waiting_choice":
        return {
            "support_option_step": "waiting_choice",
            "support_option_retries": 0,
        }
    
    if re.fullmatch(r"[1١]", last) or re.search(r"\bticket\b", last):
        detected = "ticket"
    elif re.fullmatch(r"[2²]", last) or re.search(r"\bagente\b|\bhumano\b|\bpersona\b|\bayuda\b", last):
        detected = "agente"
    
    else:
        detected = detect_intent_simple(
            last,
            {
                "ticket": "dice 1, o quiere crear un ticket",
                "agente": "dice 2, o quiere hablar con un agente",
                "otro": "respuesta tecnica o no eligio ninguna opcion clara"
            },
            context="El bot pregunto si quiere ticket o agente."
        )
        
    if "ticket" in detected:
        email_state = get_or_request_email(state, "crear_ticket")
        if email_state:
            return email_state
        return {
            "intent": "crear_ticket",
            "support_option_step": None,
            "support_option_retries": None,
            "diagnosis_step": None,
            "diagnosis_history": state.get("diagnosis_history"),
            "severity": state.get("severity"),
            "servicio": state.get("servicio"),
            "diagnosis_images": state.get("diagnosis_images") or [],
        }

    if "agente" in detected:
        email_state = get_or_request_email(state, "human")
        if email_state:
            return email_state
        return {
            "intent": "human",
            "support_option_step": None,
            "support_option_retries": None,
            "diagnosis_step": None,
            "diagnosis_history": state.get("diagnosis_history"),
            "severity": state.get("severity"),
            "servicio": state.get("servicio"),
            "diagnosis_images": state.get("diagnosis_images") or []
        }

    retries = state.get("support_option_retries") or 0
    if retries >= 2:
        return {
            "intent": "chat_general",
            "support_option_step": None,
            "support_option_retries": None,
            "messages": [AIMessage(content="No entendí bien la opción. Si necesitas ayuda más tarde dime.")]
        }

    return {
        "support_option_step": "waiting_choice",
        "support_option_retries": retries + 1,
        "messages": [guided_response(
            objetivo="Pedirle al usuario que elija entre crear un ticket (1) o hablar con alguien (2).",
            contexto="El usuario no eligió una opción válida.",
            historial=[last]
        )]
    }

async def support_agent(state: State):
    print("ENTRANDO A SUPPORT AGENT")
    print(f"history: {state.get('diagnosis_history')}")
    print(f"severity: {state.get('severity')}")
    print(f"servicio: {state.get('servicio')}")


    zammad_url = os.environ.get("ZAMMAD_URL", "")
    zammad_token = os.environ.get("ZAMMAD_HTTP_TOKEN", "")

    if not zammad_url or not zammad_token:
        print("[support_agent] ZAMMAD_URL o ZAMMAD_HTTP_TOKEN no configurados")
        return {
            "intent": "human",
            "messages": [AIMessage(content="No pude crear el ticket, te conecto con alguien.")]
        }

    history = state.get("diagnosis_history") or []
    severity: str = state.get("severity") or "moderada"
    thread_id = state.get("thread_id") or ""
    servicio_nombre: str = state.get("servicio") or "Otras Aplicaciones"
    servicio_value = get_servicio_value(servicio_nombre)

    if len(history) < 2:
        print(f"[support_agent] diagnosis_history vacio para {thread_id} - usando mensajes crudos")
        messages_state = state.get("messages", [])
        history = [
            f"{'usuario' if m.type == 'human' else 'bot'}: {m.content}"
            for m in messages_state
            if hasattr(m, 'content') and isinstance(m.content, str)
        ][-10:]

    title, description = generate_ticket_summary(history, severity)

    create_tool = next((t for t in tools if t.name == "zammad_create_ticket"), None)
    create_user_tool = next((t for t in tools if t.name == "zammad_create_user"), None)
    tag_tool = next((t for t in tools if t.name == "zammad_add_ticket_tag"), None)

    if create_tool is None:
        return {
            "intent": "human",
            "messages": [AIMessage(content="No pude crear el ticket, te conecto con alguien.")]
        }

    db = SessionLocal()
    try:
        stmt_user = select(users).where(users.c.phone_number == thread_id)
        user = db.execute(stmt_user).fetchone()

        customer = None
        if user and user.email:
            email_clean = user.email.strip().lower()
            if (
                email_clean and
                email_clean not in ("pending", "null", "none", "na", "n/a", "sin correo", "") and
                re.match(r"^[^@]+@[^@]+\.[^@]+$", email_clean)
            ):
                customer = email_clean

        if not customer:
            print(f"[support_agent] WARN: usuario {thread_id} sin email válido")

        user_name = (user.name if user else None) or ""

        if user:
            detalles_tecnicos = generate_technical_details(history, description)
            correo = user.email if user.email and user.email not in ("pending", "", None) else "No registrado"
            telefono = user.phone_number if user.phone_number else "No registrado"

            user_info = (
                "Informacion del cliente\n"
                f"{user.name or 'No registrado'}\n"
                f"{f'{user.company} - {user.sede}' if user.company and user.company != 'pending' and user.sede else (user.company if user.company and user.company != 'pending' else 'No registrada')}\n"
                f"{telefono}\n"
                f"{correo}\n\n"
                f"Diagnostico del problema\n"
                f"{description}\n\n"
                f"Detalles tecnicos\n"
                f"{detalles_tecnicos}"
            )
        else:
            user_info = description

    finally:
        db.close()

    if create_user_tool:
        try:
            name_parts = user_name.strip().split(" ", 1)
            firstname = name_parts[0] if name_parts else "Usuario"
            lastname = name_parts[1] if len(name_parts) > 1 else "Cliente"
            
            if not customer:
                customer = f"{thread_id}@whatsapp.noreply"
                print(f"[support_agent] Sin email, usando fallback: {customer}")
            
            # Usamos una función interna con retry para asegurar el usuario
            @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
            async def ensure_user():
                await create_user_tool.ainvoke({
                    "params": {
                        "email": customer,
                        "firstname": firstname,
                        "lastname": lastname,
                    }
                })
            
            await ensure_user()
            print(f"Usuario asegurado en Zammad: {customer}")
        except Exception as e:
            print(f"[support_agent] Error persistente con usuario en Zammad: {e}")

    # Función interna con retry para la creación del ticket
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    async def create_ticket_with_retry():
        return await create_tool.ainvoke({
            "params": {
                "title": title,
                "group": "Users",
                "customer": customer if customer else "soporte@soporteAI.com",
                "article_body": user_info,
                "priority": PRIORIDAD_MAP.get(severity, "2 moderada"),
            }
        })

    result = await create_ticket_with_retry()

    print("Resultado create_ticket", result)

    ticket_number = "N/A"
    ticket_id = None

    # FIXED: unified ticket parsing with a helper to avoid silent failures
    ticket_number, ticket_id_raw = _parse_ticket_result(result)
    ticket_id: Optional[int] = int(ticket_id_raw) if ticket_id_raw is not None else None

    if ticket_number == "N/A":
        print(f"[support_agent] WARN: no se pudo extraer ticket_number del resultado: {result}")

    if ticket_id is not None and tag_tool:
        try:
            await tag_tool.ainvoke({
                "params": {
                    "ticket_id": ticket_id,
                    "tag": "whatsapp_bot"
                }
            })
            print(f"[support_agent] Etiqueta whatsapp_bot agregada al ticket {ticket_id}")
        except Exception as e:
            print(f"[support_agent] Error agregando etiqueta al ticket {ticket_id}: {e}")

    if ticket_id is not None and servicio_value:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.put(
                    f"{zammad_url}/api/v1/tickets/{ticket_id}",
                    json={"servicio": servicio_value},
                    headers={
                        "Authorization": f"Token token={zammad_token}",
                        "Content-Type": "application/json",
                    },
                    timeout=30,
                )
                r.raise_for_status()
                print(f"Campo servicio actualizado: {servicio_value} en ticket {ticket_id}")
        except Exception as e:
            print(f"[support_agent] Error actualizando campo servicio: {e}")

    if ticket_id is not None and thread_id:
        db_save = SessionLocal()
        try:
            stmt_user2 = select(users).where(users.c.phone_number == thread_id)
            user2 = db_save.execute(stmt_user2).fetchone()

            if user2:
                conv2 = db_save.execute(
                    select(conversation).where(
                        conversation.c.users == user2.id,
                        conversation.c.status == "open"
                    )
                ).fetchone()

                if not conv2:
                    result_conv2 = db_save.execute(
                        insert(conversation).values(
                            users=user2.id,
                            status="open"
                        ).returning(conversation.c.id)
                    )
                    conv_id2 = result_conv2.scalar()
                    db_save.commit()
                else:
                    conv_id2 = conv2.id

                db_save.execute(
                    insert(tickets).values(
                        conversation_id=conv_id2,
                        zammad_ticket_id=ticket_id,
                        subject=title,
                        status="open",
                        description=description
                    )
                )
                db_save.commit()
                print(f"[support_agent] Ticket #{ticket_number} guardado en BD local")

        except Exception as e:
            print(f"[support_agent] Error guardando ticket en BD: {e}")
            db_save.rollback()
        finally:
            db_save.close()

    images: List[dict] = list(state.get("diagnosis_images") or [])

    if ticket_id is not None and images:
        safe_ticket_id: int = ticket_id
        print(f"[support_agent] Adjuntando {len(images)} imagen(es) en una sola nota...")
        await attach_images_to_zammad(safe_ticket_id, images)
    elif ticket_id is None:
        print("[support_agent] WARN: ticket_id es None, no se pueden adjuntar imágenes")

    diagnosis_history = state.get("diagnosis_history") or []
    if thread_id:
        save_conversation_memory(thread_id, diagnosis_history)

    print(f"TICKET CREADO #{ticket_number}")

    return {
        "messages": [
            AIMessage(
                content=(
                    f"Ya te cree el ticket.\n"
                    f"Es el #{ticket_number}, sobre {title}."
                )
            )
        ],
        "support_option_step": None,
        "support_option_retries": None,
        "diagnosis_step": None,
        "diagnosis_history": None,
        "diagnosis_images": None,
        "severity": None,
        "servicio": None,
        "quick_fix_step": None,
        "quick_fix_solution": None,
        "email_request_step": None,
        "pending_intent": None,
        "ticket_step": None,
        "email_skipped": None,
        "greeting_step": "waiting_problem",
    }

async def check_ticket_status(state: State):
    messages_state = state.get("messages", [])
    last_message = str(messages_state[-1].content).strip()
    step = state.get("ticket_status_step")

    if step == "ask_id":
        match = re.search(r'\b\d{4,}\b', last_message)

        if not match:
            return {
                "ticket_status_step": "ask_id",
                "messages": [AIMessage(content="Claro, ¿me podrías indicar el número de tu ticket por favor?")]
            }

        ticket_number = match.group(0)
        search_tool = next((t for t in tools if t.name == "zammad_search_tickets"), None)

        if search_tool is None:
            return {
                "ticket_status_step": None,
                "messages": [AIMessage(content="No pude consultar el ticket ahorita, intenta más tarde.")]
            }

        result = await search_tool.ainvoke({"params": {"query": ticket_number}})

        ticket_info = "No encontré información sobre ese ticket."

        if isinstance(result, list) and len(result) > 0:
            text = result[0].get("text", "")

            number_match = re.search(r'Ticket #(\d+)', text)
            title_match = re.search(r'##\s*Ticket #\d+ - (.*)', text)
            state_match = re.search(r'\*\*State\*\*:\s*(.+)', text)

            estados = {
                "new": "Nuevo",
                "open": "Abierto",
                "closed": "Cerrado",
                "pending reminder": "Pendiente",
                "merged": "Fusionado"
            }

            if number_match:
                estado_raw = state_match.group(1).strip().lower() if state_match else ""
                estado_es = estados.get(estado_raw, estado_raw.capitalize() if estado_raw else "N/A")

                ticket_info = (
                    f"Ticket: {number_match.group(1)}\n"
                    f"Titulo: {title_match.group(1).strip() if title_match else 'N/A'}\n"
                    f"Estado: {estado_es}"
                )
            else:
                ticket_info = text[:300] if text else "No pude leer la respuesta."

        return {
            "ticket_status_step": None if "Ticket:" in ticket_info else "ask_id",
            "messages": [AIMessage(
                content=f"Mira, esto encontré:\n\n{ticket_info}"
                if "Ticket:" in ticket_info
                else f"No encontré un ticket con el número {ticket_number}, ¿me confirmas el número correcto?"
            )]
        }

    return {
        "ticket_status_step": "ask_id",
        "messages": [guided_response(
            objetivo="Pedirle al usuario el número del ticket que quiere consultar.",
            contexto="El usuario quiere saber el estado de un ticket."
        )]
    }

async def escalate_human(state: State):
    thread_id = state.get("thread_id") or ""
    conv_id = state.get("chatwoot_conversation_id")
    
    summary = state.get("summary", "")
    historial_lista = state.get("diagnosis_history") or []
    
    if not historial_lista:
        mensajes_state = state.get("messages", [])
        historial_lista = [
            f"{'Usuario' if m.type == 'human' else 'Bot'}: {m.content}"
            for m in mensajes_state
        ][-8:]

    if summary:
        historial_lista = [f"Resumen previo: {summary}"] + historial_lista

    resumen_ejecutivo = await _generar_resumen_agente(historial_lista)

    if conv_id and resumen_ejecutivo:
        await _enviar_nota_privada_chatwoot(conv_id, resumen_ejecutivo)
    else:
        logger.warning(f"No se pudo enviar nota privada: conv_id={conv_id} o resumen vacio")

    if thread_id:
        from ..integrations import save_conversation_memory
        save_conversation_memory(thread_id, historial_lista)

    return {
        "messages": [AIMessage(content="Te estoy conectando con un técnico para que te ayude mejor. Por favor espera un momento.")],
        "intent": "human",
        "greeting_step": None,
        "diagnosis_step": None,
    }

def waiting_agent(state: State):
    messages_list = state.get("messages", [])
    last = str(messages_list[-1].content).lower() if messages_list else ""

    despedida = detect_intent_simple(
        last,
        {
            "gracias": "dice gracias, ok, entendido, listo, perfecto",
            "otro": "cualquier otra cosa"
        }
    )

    if "gracias" in despedida:
        return {"messages": [AIMessage(content="Con gusto, ya están al tanto.")]}

    return {"messages": [AIMessage(content="Ya avisé, en breve te contactan. Si necesitas cancelar la espera dime.")]}


async def _generar_resumen_agente(history: list) -> Optional[str]:
    if not history:
        return None
    
    historial_texto = "\n".join(history)

    try:
        response = await llm_diagnosis.ainvoke([
            SystemMessage(content="""
            Eres un supervisor de soporte tecnico. Tu objetivo es resumir
            el caso para el tecnico que lo va a atender.
            
            Responde en un formato de puntos claves (maximo 3 puntos):
            
            - PROBLEMA: (resumen breve)
            - PRUEBAS: (Que intento el bot o el usuario)
            - ESTADO: (En que punto se quedo la charla)
            """),
            HumanMessage(content=f"Historial de la conversacion:\n{historial_texto}")
        ])
        return str(response.content)
    except Exception as e:
        logger.error(f"Error generando resumen: {e}")
        return None

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True
)
async def _enviar_nota_privada_chatwoot(conv_id: int, mensaje: str):
    chatwoot_url = os.getenv("CHATWOOT_URL")
    account_id = os.getenv("CHATWOOT_ACCOUNT_ID")
    api_token = os.getenv("CHATWOOT_API_TOKEN")

    if not all([chatwoot_url, account_id, api_token]):
        logger.error("Faltan variables de entorno para Chatwoot (URL, ACCOUNT_ID o TOKEN)")
        return

    url = f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conv_id}/messages"
    
    headers = {
        "api_access_token": api_token,
        "Content-Type": "application/json"
    }

    payload = {
        "content": f" RESUMEN DE IA PARA EL AGENTE:\n\n{mensaje}",
        "message_type": "outgoing",
        "private": True
    }

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=headers, timeout=10)
            r.raise_for_status()
            logger.info(f"Nota privada enviada con éxito a conversación {conv_id}")
    except Exception as e:
        logger.error(f"Error enviando nota privada: {e}")
        