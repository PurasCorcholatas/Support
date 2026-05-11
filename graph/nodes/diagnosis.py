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


async def diagnosis_flow(state: State):

    step = state.get("diagnosis_step") or 0
    history = state.get("diagnosis_history") or []
    messages_state = state.get("messages", [])
    last_msg_obj = messages_state[-1]

    if isinstance(last_msg_obj.content, list):
        image_context = llm_diagnosis.invoke([
            SystemMessage(content=(
                "Eres un ingeniero senior de soporte técnico en Serviunix. Tu tarea es analizar esta imagen de error. "
                "1. Identifica el sistema o aplicación afectada. "
                "2. Extrae CÓDIGOS DE ERROR exactos y el mensaje de texto visible. "
                "3. Basado en tu conocimiento técnico, explica brevemente qué significa ese error. "
                "4. Sugiere UN paso de solución inmediata que el usuario pueda intentar. "
                "Responde en un tono técnico pero claro para WhatsApp. Máximo 5 oraciones."
            )),
            last_msg_obj
        ])
        last_user_message = f"[Imagen del error]: {str(image_context.content).strip()}"
    else:
        last_user_message = str(last_msg_obj.content)

    if not history or history[-1] != f"Usuario: {last_user_message}":
        updated_history = history + [f"Usuario: {last_user_message}"]
    else:
        updated_history = history

    if len(updated_history) > 8:
        tail = updated_history[-7:]
        historico_para_llm = [updated_history[0]] + tail
    else:
        historico_para_llm = updated_history

    historico_truncado = [
        entry[:500] + "..." if len(entry) > 500 else entry
        for entry in historico_para_llm
    ]
    updated_history_text = "\n".join(historico_truncado)

    if step >= 1:
        last_bot_message = ""
        for entry in reversed(history):
            if entry.startswith("bot:"):
                last_bot_message = entry[4:].strip()
                break

        if last_bot_message:
            is_clarification = llm_diagnosis.invoke([
                SystemMessage(content="Responde SOLO con 'si' o 'no'. Sin explicaciones."),
                HumanMessage(content=f"""
El tecnico le hizo esta pregunta al usuario:
"{last_bot_message}"

El usuario respondio:
"{last_user_message}"

¿La respuesta del usuario es una pregunta de aclaración sobre términos técnicos o sobre cómo hacer algo que el técnico mencionó?
(por ejemplo: "¿qué son los logs?", "¿cómo borro la caché?", "¿dónde veo eso?", "¿qué es eso?", "no entiendo", "¿qué es ping?", "¿cómo hago eso?")

Responde SOLO: si / no
""")
            ])

            if "si" in str(is_clarification.content).strip().lower():
                clarification_response = llm_diagnosis.invoke([
                    SystemMessage(content="""
Eres Santiago, técnico de soporte de Serviunix hablando por WhatsApp.
El usuario no entendió algo técnico que le pediste.

REGLAS:
- Revisa el historial. Si ya explicaste cómo hacerlo antes, NO lo repitas, solo simplifica con otras palabras.
- Si es la primera vez que preguntan, explica en 1 oración simple cómo hacerlo.
- Al final pregunta qué resultado o mensaje le apareció.
- Máximo 2 oraciones. Sin signos de exclamación.
"""),
                    HumanMessage(content=f"""
Historial completo:
{chr(10).join(history)}

La pregunta de diagnóstico que hiciste fue:
"{last_bot_message}"

El usuario no entendió:
"{last_user_message}"

Responde sin repetir lo que ya explicaste antes.
""")
                ])
                return {
                    "diagnosis_step": step,
                    "diagnosis_history": updated_history,
                    "messages": [AIMessage(content=str(clarification_response.content))]
                }

    summary = state.get("summary", "")
    summary_context = f"\nResumen de la conversación anterior: {summary}\n" if summary else ""

    try:
        entities_response = llm_diagnosis.invoke([
            SystemMessage(content="Responde SOLO en JSON sin markdown ni explicaciones."),
            HumanMessage(content=f"""
{summary_context}
Del historial extrae las entidades ya conocidas sobre el problema:
{{
    "alcance": "un usuario / varios usuarios / toda la empresa / null",
    "objeto_afectado": "lo que se quiere migrar/arreglar/configurar, o null",
    "tiempo": "cuándo empezó o cuándo quiere hacerlo, o null",
    "accion": "lo que quiere hacer (migrar, configurar, arreglar, instalar...) o null",
    "sistema_origen": "plataforma o sistema de origen si se mencionó (ej: Zimbra, Gmail, Exchange) o null",
    "sistema_destino": "plataforma o sistema destino si se mencionó (ej: Microsoft 365, Google Workspace) o null",
    "sistema_afectado": "sistema operativo, aplicación o servicio mencionado si no es migración, o null",
    "correo_usuario": "dirección de correo mencionada por el usuario si la dio, o null",
    "cantidad_cuentas": "número de cuentas/usuarios si se mencionó, o null",
    "licencia": "si se mencionó si tiene o no licencia activa, o null",
    "error_exacto": "mensaje de error exacto si el usuario lo mencionó, o null"
}}

HISTORIAL:
{updated_history_text}
""")
        ])
        known = json.loads(re.sub(r"```json|```", "", str(entities_response.content)).strip())
    except Exception as e:
        print(f"[diagnosis_flow] Error extrayendo entidades: {e}")
        known = {}

    contexto_conocido = "\n".join([
        f"- {k}: {v}" for k, v in known.items() if v and str(v).lower() != "null"
    ])

    es_migracion_o_consultoria = (
        known.get("sistema_origen") and known.get("sistema_destino")
    ) or (
        known.get("accion") in ("migrar", "configuración", "configurar", "instalar", "consultoría") and
        known.get("objeto_afectado")
    )

    umbral_steps = 2 if es_migracion_o_consultoria else 5

    if step >= umbral_steps:
        prompt_suficiencia = """
Eres un ingeniero de soporte senior. Revisa este historial.

HISTORIAL:
{history}

ENTIDADES CONOCIDAS:
{entidades}

¿Tienes suficiente para abrir un ticket útil?

ES suficiente si:
- Para migraciones o consultoría: tienes el sistema origen, el destino y el objeto a migrar
- Para problemas técnicos: tienes el error/síntoma, el sistema afectado, y cuándo empezó
- El técnico asignado puede contactar al usuario para pedir detalles adicionales

NO es necesario saber cuántas cuentas hay, si tiene licencias activas, ni detalles técnicos de implementación.
Si el objetivo o problema está claro, responde 'si'.

Responde SOLO: si / no
""".format(history=updated_history_text, entidades=contexto_conocido or "ninguna aún")

        enough = llm_diagnosis.invoke([
            SystemMessage(content="Responde SOLO con 'si' o 'no'. Sin explicaciones."),
            HumanMessage(content=prompt_suficiencia)
        ])

        tiene_suficiente = "si" in str(enough.content).strip().lower()

        if tiene_suficiente or step >= 8:
            severity, servicio = detected_incident_severity(updated_history_text)
            return {
                "diagnosis_step": None,
                "diagnosis_history": updated_history,
                "intent": "quick_fix",
                "severity": severity,
                "servicio": servicio,
            }

    if step == 0 and not history:
        # Si el usuario ya describió algo en el mensaje actual, usarlo directamente
        primera_pregunta = _hacer_pregunta_tecnica(last_user_message)
        return {
            "diagnosis_step": 1,
            "diagnosis_history": [f"Usuario: {last_user_message}", f"bot: {primera_pregunta}"],
            "messages": [AIMessage(content=primera_pregunta)]
        }

    
    for intento in range(3):
        try:
            response = llm_diagnosis.invoke([
                SystemMessage(content="""
Eres Santiago, técnico de soporte senior de Serviunix hablando por WhatsApp.
Tu única tarea: hacer UNA pregunta técnica que consiga el dato más crítico que falta para diagnosticar el problema.

REGLAS:
- Solo la pregunta. Sin introducción, sin análisis, sin separadores.
- Máximo 1 oración natural, como WhatsApp.
- Sin signos de exclamación.
- NUNCA preguntes algo que ya esté en los DATOS CONFIRMADOS.
- Para migraciones: si ya tienes origen y destino, no preguntes por cuentas ni licencias.
- Si el alcance ya dice "un usuario", nunca preguntes cuántos usuarios están afectados.

PROHIBIDO:
"Por el historial..." / "Según lo que describes..." / "---" / cualquier texto antes o después de la pregunta.
"""),
                HumanMessage(content=f"""
HISTORIAL:
{updated_history_text}

DATOS YA CONFIRMADOS — PROHIBIDO PREGUNTAR SOBRE ESTOS:
{contexto_conocido if contexto_conocido else "ninguno aún"}

Devuelve SOLO la siguiente pregunta técnica. Una oración. Sin nada más.
""")
            ])
            break
        except Exception as e:
            error_str = str(e).lower()
            if "500" in error_str or "internal server error" in error_str:
                if intento < 2:
                    await asyncio.sleep(2 ** intento)  # FIXED: non-blocking sleep
                    continue
            raise
    else:
        response = AIMessage(content="¿Puedes describir con más detalle qué está pasando exactamente?")

    question = str(getattr(response, "content", response)).strip()

    final_content = question
    if 'image_context' in locals() and image_context:
        analysis = str(image_context.content).strip()
        final_content = f"Mira, analicé la imagen:\n\n{analysis}\n\n{question}"

    return {
        "diagnosis_step": step + 1,
        "diagnosis_history": updated_history + [f"bot: {question}"],
        "diagnosis_images": state.get("diagnosis_images") or [],
        "messages": [AIMessage(content=final_content)]
    }