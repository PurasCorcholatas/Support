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


async def greeting_flow(state: State):
    db = SessionLocal()
    try:
        thread_id = state.get("thread_id")
        step = state.get("greeting_step", "start")
        messages_state = state.get("messages", [])
        last_user_message = str(messages_state[-1].content).strip()
        
        print(f"DEBUG GREETING: thread_id={thread_id}, step={step}, last_msg='{last_user_message}'")
        print(f"DEBUG GREETING: state diagnosis_history={state.get('diagnosis_history')}")

        stmt_user = select(users).where(users.c.phone_number == thread_id)
        user = db.execute(stmt_user).fetchone()

        # Helper para detectar problemas en cualquier paso
        def _check_for_problem(text):
            return detect_intent_simple(
                text,
                {
                    "problema": "describe un problema tecnico, error, falla, solicitud de ayuda o migracion",
                    "otro": "saludo, nombre, empresa o mensaje casual"
                }
            )

        if step == "start":
            tiene_problema = _check_for_problem(last_user_message)
            print(f"DEBUG GREETING: detect_intent_simple result={tiene_problema}")

            if not user:
                db.execute(
                    insert(users).values(
                        phone_number=thread_id,
                        name="pending",
                        company="pending",
                        greeting_step="register_name"
                    )
                )
            else:
                db.execute(
                    update(users)
                    .where(users.c.phone_number == thread_id)
                    .values(greeting_step="register_name")
                )
            db.commit()

            if "problema" in tiene_problema:
                print(f"DEBUG GREETING: Problema detectado en 'start'. Guardando en history.")
                return {
                    "greeting_step": "register_name",
                    "diagnosis_history": [f"Usuario: {last_user_message}"],
                    "messages": [
                        AIMessage(content=(
                            "Hola, bienvenido al soporte técnico de Serviunix. Antes de atenderte necesito algunos datos. ¿Con quién tengo el gusto?"
                        ))
                    ]
                }

            return {
                "greeting_step": "register_name",
                "messages": [
                    AIMessage(content="Hola, bienvenido al soporte técnico de Serviunix. ¿Con quién tengo el gusto?")
                ]
            }

        if step == "confirm_branch":
            tiene_problema = detect_intent_simple(
                last_user_message,
                {
                    "problema": "describe un problema tecnico, error, falla, viene a reportar algo",
                    "agradecimiento": "dice gracias, ok, listo, perfecto, entendido, vale",
                    "otro": "saludo general, pregunta casual, no hay problema claro"
                }
            )

            if "problema" in tiene_problema:
                db.execute(
                    update(users)
                    .where(users.c.phone_number == thread_id)
                    .values(greeting_step="waiting_problem")
                )
                db.commit()
                return {
                    "greeting_step": "waiting_problem",
                    "intent": "diagnosis_flow",
                    "diagnosis_step": 1,
                    "diagnosis_history": [f"Usuario: {last_user_message}"],
                    "diagnosis_images": [],
                    "messages": [
                        guided_response(
                            objetivo="Hacer UNA pregunta tecnica de diagnostico sobre el problema que describio.",
                            contexto=f"El usuario dijo: {last_user_message}",
                        )
                    ]
                }
                
            if "agradecimiento" in tiene_problema:
                db.execute(
                    update(users)
                    .where(users.c.phone_number == thread_id)
                    .values(greeting_step=None)
                )
                db.commit()
                return {
                    "greeting_step": None,
                    "messages": [AIMessage(content="Con gusto. Si necesitas ayuda en otra cosa, aquí estoy.")]
                }

            db.execute(
                update(users)
                .where(users.c.phone_number == thread_id)
                .values(greeting_step="waiting_problem")
            )
            db.commit()
            return {
                "greeting_step": "waiting_problem",
                "messages": [
                    AIMessage(content="Hola de nuevo. ¿En qué te puedo ayudar?")
                ]
            }

        if step == "register_name":
            intent = detect_intent_simple(
                last_user_message,
                {
                    "es_nombre": "contiene un nombre propio de persona",
                    "otro": "no contiene un nombre"
                }
            )

            if "otro" in intent:
                response = llm.invoke([
                    SystemMessage(content="Responde breve y vuelve a pedir el nombre. Sin signos de exclamación."),
                    HumanMessage(content=last_user_message)
                ])
                return {
                    "greeting_step": "register_name",
                    "messages": [AIMessage(content=response.content)]
                }

            extract = llm.invoke([
                SystemMessage(content="Extrae solo el nombre propio de la persona. Devuelve únicamente el nombre, sin más texto."),
                HumanMessage(content=last_user_message)
            ])

            name = str(extract.content).strip().title()

            if user:
                db.execute(
                    update(users)
                    .where(users.c.phone_number == thread_id)
                    .values(name=name, greeting_step="register_company_sede")
                )
            else:
                db.execute(
                    insert(users).values(
                        phone_number=thread_id,
                        name=name,
                        company="pending",
                        greeting_step="register_company_sede"
                    )
                )

            db.commit()
            await update_chatwoot_contact(phone_number=thread_id or "", name=name)

            # También revisamos si en el mensaje del nombre incluyó el problema (poco común pero posible)
            tiene_problema = _check_for_problem(last_user_message)
            new_history = state.get("diagnosis_history") or []
            if "problema" in tiene_problema and not new_history:
                new_history = [f"Usuario: {last_user_message}"]

            return {
                "greeting_step": "register_company_sede",
                "diagnosis_history": new_history if new_history else None,
                "messages": [
                    AIMessage(content=f"Mucho gusto, {name}. ¿En qué empresa trabajas y en qué sede o sucursal estás?")
                ]
            }

        if step == "register_company_sede":
            extract_response = llm.invoke([
                SystemMessage(content="""Extrae empresa y sede/surcusal del mensaje. Responde SOLO en JSON sin markdown:
{"empresa": "nombre de la empresa o null", "sede": "nombre de la sede o sucursal o null", "es_respuesta_valida": true o false}

es_respuesta_valida debe ser true solo si el mensaje menciona claramente una empresa."""),
                HumanMessage(content=last_user_message)
            ])

            empresa, sede, es_valida = None, None, False

            try:
                data = json.loads(str(extract_response.content))
                empresa = data.get("empresa")
                sede = data.get("sede")
                es_valida = data.get("es_respuesta_valida", False)
            except Exception as e:
                print(f"[greeting_flow register_company_sede] Error parseando JSON: {e}")

            if not es_valida or not empresa:
                return{
                    "greeting_step": "register_company_sede",
                    "messages": [AIMessage(content="¿En que empresa trabajas y en sede te encuentras?")]
                }
                
            if not sede:
                sede = "Sin sede"
            
            db.execute(
                update(users)
                .where(users.c.phone_number == thread_id)
                .values(
                    company=empresa,
                    sede=sede,
                    greeting_step="waiting_problem"
                )
            )
            
            db.commit()

            await update_chatwoot_contact(phone_number=thread_id or "", company=empresa)

            # Revisamos si hay historia previa
            problema_previo = state.get("diagnosis_history") or []
            
            # También revisamos si en este mensaje incluyó el problema
            tiene_problema_ahora = _check_for_problem(last_user_message)
            if "problema" in tiene_problema_ahora and not problema_previo:
                 problema_previo = [f"Usuario: {last_user_message}"]

            if problema_previo:
                problema_texto = problema_previo[0].replace("Usuario: ", "")
                
                primera_pregunta = llm_diagnosis.invoke([
                    SystemMessage(content="""Eres Santiago, soporte tecnico de Serviunix en WhatsApp.
                    Haz UNA sola pregunta tecnica corta para diagnosticar el problema descrito
                    Sin signos de exclamacion, sin saludos, sin introduccion.
                    Devuelve SOLO la pregunta, sin signos de interrogacion al inicio ni al final.
                    """),
                        HumanMessage(content=f"Problema: {problema_texto}")
                ])
                pregunta = str(primera_pregunta.content).strip().strip("?").strip("¿")

                return {
                    "greeting_step": "waiting_problem",
                    "intent": "diagnosis_flow",
                    "diagnosis_step": 1,
                    "diagnosis_history": problema_previo + [f"bot: {pregunta}"],
                    "messages": [
                        AIMessage(content=f"Listo, quedaste registrado en {empresa}. ¿{pregunta}?")
                    ]
                }
            
            return {
                "greeting_step": "waiting_problem",
                "messages": [
                    AIMessage(content=f"Listo, quedaste registrado en {empresa}. ¿En que te puedo ayudar?")
                ]
            }
        
        if step == "waiting_problem":
            tiene_problema = detect_intent_simple(
                last_user_message,
                {
                    "problema": "describe un problema tecnico, error, falla, solicitud de ayuda",
                    "otro": "saludo, mensaje casual, sin problema claro"
                }
            )
            if "problema" in tiene_problema:
                return {
                    "greeting_step": "waiting_problem",
                    "intent": "diagnosis_flow",
                    "diagnosis_step": 1,
                    "diagnosis_history": [f"Usuario: {last_user_message}"],
                    "diagnosis_images": [],
                }
            return {
                "greeting_step": "waiting_problem",
                "messages": [AIMessage(content="¿En qué te puedo ayudar?")]
            }

        return {}

    finally:
        db.close()