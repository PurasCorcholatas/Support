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


async def router(state: State):
    thread_id = state.get("thread_id") or ""
    messages_list = state.get("messages", [])
    last_message = messages_list[-1] if messages_list else None
    user_text = str(last_message.content).lower() if last_message else ""

    print(f"DEBUG ROUTER: thread_id={thread_id}, user_text='{user_text}'")
    print(f"DEBUG ROUTER: current state diagnosis_history={state.get('diagnosis_history')}")

    # 0. MANEJO DE CORREO (Si estábamos pidiendo uno)
    if state.get("email_request_step") == "ask_email":
        email_match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", user_text)
        if email_match:
            email = email_match.group(0).lower()
            db = SessionLocal()
            try:
                db.execute(update(users).where(users.c.phone_number == thread_id).values(email=email))
                db.commit()
                pending = state.get("pending_intent") or "chat_general"
                return {"email_request_step": None, "intent": pending, "pending_intent": None}
            finally:
                db.close()
        
        skip_intent = detect_intent_simple(user_text, {"skip": "no tiene, no quiere darlo, saltar", "otro": "otra cosa"})
        if "skip" in skip_intent:
            pending = state.get("pending_intent") or "chat_general"
            return {"email_skipped": True, "email_request_step": None, "intent": pending, "pending_intent": None}

    # 1. FLUJOS ACTIVOS (Continuidad)
    hay_flujo_activo = (
        state.get("diagnosis_step") is not None or
        state.get("support_option_step") is not None or
        state.get("quick_fix_step") is not None or
        state.get("email_request_step") is not None or
        state.get("ticket_step") is not None or
        state.get("ticket_status_step") == "ask_id"
    )

    if hay_flujo_activo and messages_list:
        last_global = str(messages_list[-1].content).strip()
        salida_global = detect_intent_simple(
            last_global,
            {
                "resuelto": "ya se resolvió, ya funciona, ya pudo",
                "cancela": "no quiere continuar, cancela",
                "continua": "cualquier otra cosa"
            }
        )
        if "resuelto" in salida_global or "cancela" in salida_global:
            return {"intent": "end", "messages": [AIMessage(content="Entendido. Cualquier otra cosa me avisas.")], "diagnosis_step": None, "support_option_step": None}
        
        current_intent = state.get("intent")
        if current_intent and current_intent not in ("greeting_flow", "chat_general", None):
            return {"intent": current_intent}

    # 2. ESCALACIÓN HUMANA ACTIVA
    if state.get("human_escalated"):
        quiere_salir = detect_intent_simple(user_text, {"salir": "ya se resolvio o quiere cancelar", "espera": "sigue esperando"})
        if "salir" in quiere_salir:
            return {"intent": "chat_general", "human_escalated": False}
        return {"intent": "waiting_agent"}

    # 3. REGISTRO OBLIGATORIO (Para usuarios nuevos o pendientes)
    db = SessionLocal()
    try:
        user_row = db.execute(select(users).where(users.c.phone_number == thread_id)).fetchone()
        if not user_row or not user_row.name or user_row.name == "pending" or not user_row.company or user_row.company == "pending":
            step = user_row.greeting_step if user_row else "start"
            return {"intent": "greeting_flow", "greeting_step": step}
            
    finally:
        db.close()

    # 4. DETECCIÓN DE INTENCIÓN TÉCNICA
    summary = state.get("summary", "")
    router_context = f"Resumen de lo hablado anteriormente: {summary}" if summary else ""

    detected = detect_intent_simple(
        user_text,
        {
            "estado_ticket": "quiere saber el estado de un ticket o menciona un numero",
            "crear_ticket": "tiene un problema, error, falla, necesita soporte o quiere migrar",
            "humano": "quiere hablar con un agente humano",
            "otro": "saludo o charla"
        },
        context=router_context
    )

    if "estado_ticket" in detected:
        return {"intent": "estado_ticket", "ticket_status_step": "ask_id", "greeting_step": None}

    if "crear_ticket" in detected:
        return {
            "intent": "diagnosis_flow",
            "diagnosis_step": 1,
            "diagnosis_history": [f"Usuario: {user_text}"],
            "greeting_step": None
        }

    if "humano" in detected:
        return {"intent": "human", "greeting_step": None}

    # 5. CASO SALUDO PENDIENTE (Solo si no hubo intención técnica clara)
    if user_row and user_row.greeting_step == "confirm_branch":
        return {"intent": "greeting_flow", "greeting_step": "confirm_branch"}

    # 6. DEFAULT (Charla General)
    return {"intent": "chat_general", "greeting_step": None}