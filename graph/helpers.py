from config.db import SessionLocal
from models.users import users
from sqlalchemy import select
from .state import State
from typing import Optional, List, Dict
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
import re
import json
from .llms import llm, llm_diagnosis

def _normalize_images(image_b64_list: list) -> list:
    """Normalize image list to dicts with data and mime_type keys."""
    normalized = []
    for img in image_b64_list:
        if isinstance(img, str):
            normalized.append({"data": img, "mime_type": "image/jpeg"})
        elif isinstance(img, dict):
            normalized.append(img)
    return normalized

def guided_response(objetivo: str, contexto: str = "", historial: Optional[list] = None):
    historial_text = "\n".join(historial) if historial else ""

    response = llm.invoke([
        SystemMessage(content="""
Eres Santiago, técnico de soporte de Serviunix. Hablas por WhatsApp como lo haría cualquier persona real.
Sin formalismos, sin sonar a bot, sin listas numeradas, sin signos de exclamación exagerados.

CÓMO HABLAS:
- Corto y directo. Máximo 2 oraciones.
- Como alguien que conoce al usuario, no como un formulario
- Usas expresiones naturales: "listo", "dale", "cuéntame","oye"
- Nunca uses: "onda","che","mira" u otras expresiones regionales
- Si el usuario saluda → respondes el saludo brevemente Y cumples el objetivo en la misma respuesta
- Nunca repites información que el usuario ya dio
- Nunca haces más de una pregunta
- Nunca uses signos de exclamación
- NUNCA uses voseo: ni 'vos', ni 'decime', ni 'querés', ni 'podés'

EJEMPLOS DE TONO:
Malo: "¡Hola! ¿En qué puedo ayudarte hoy?"
Bueno: "Hola Simon como estas, ¿Te encuentras en Bello?"

Malo: "Por favor indícame el nombre de tu empresa."
Bueno: "¿Me podrías dar el nombre de tu empresa?"

Malo: "Entendido. Procederé a verificar tu identidad."
Bueno: "Ok, te voy a hacer unas preguntas rápidas para verificar que eres tú."

Malo: "¡Perfecto! Tu registro ha sido completado exitosamente."
Bueno: "Listo, quedaste registrado en esa sede. ¿En qué te puedo ayudar?"
"""),
        HumanMessage(content=f"""
OBJETIVO: {objetivo}
CONTEXTO: {contexto}
HISTORIAL: {historial_text}

Responde naturalmente cumpliendo el objetivo. Sin saludos largos, sin listas, sin exagerar.
""")
    ])

    return AIMessage(content=response.content)

def detect_intent_simple(user_text: str, options: dict, context: str = "") -> str:
    options_text = "\n".join(f"- {k}: {v}" for k, v in options.items())
    keys = list(options.keys())

    response = llm.invoke([
        SystemMessage(content=f"Responde SOLO con una de estas claves exactas: {', '.join(keys)}. Sin explicaciones."),
        HumanMessage(content=f"""
{context}

El usuario dijo: "{user_text}"

Opciones:
{options_text}

Responde SOLO con la clave exacta.
""")
    ])

    result = str(response.content).strip().lower()
    for key in keys:
        if key in result:
            return key
    return keys[-1]

def _hacer_pregunta_tecnica(problema_texto: str) -> str:
    response = llm_diagnosis.invoke([
        SystemMessage(content="""Eres Santiago, soporte técnico de Serviunix en WhatsApp.
Haz UNA sola pregunta técnica corta para diagnosticar el problema descrito.
Sin signos de exclamación, sin saludos, sin introducción.
Devuelve SOLO la pregunta, sin signos de interrogación al inicio ni al final."""),
        HumanMessage(content=f"Problema: {problema_texto}")
    ])
    return str(response.content).strip().strip("?").strip("¿")

def _parse_ticket_result(result) -> tuple:
    """
    FIXED: unified helper to extract (ticket_number, ticket_id) from any Zammad
    create_ticket response shape, with explicit warnings on failure.
    """
    ticket_number = "N/A"
    ticket_id = None

    if isinstance(result, list) and result:
        text = result[0].get("text", "") if isinstance(result[0], dict) else str(result[0])
        try:
            data = json.loads(text)
            ticket_number = data.get("number", "N/A")
            ticket_id = data.get("id")
        except Exception:
            match_num = re.search(r'"number":\s*"(\d+)"', text)
            match_id = re.search(r'"id":\s*(\d+)', text)
            if match_num:
                ticket_number = match_num.group(1)
            if match_id:
                ticket_id = int(match_id.group(1))

    elif isinstance(result, str):
        try:
            data = json.loads(result)
            ticket_number = data.get("number", "N/A")
            ticket_id = data.get("id")
        except Exception:
            match_num = re.search(r'"number":\s*"(\d+)"', result)
            match_id = re.search(r'"id":\s*(\d+)', result)
            if match_num:
                ticket_number = match_num.group(1)
            if match_id:
                ticket_id = int(match_id.group(1))

    elif isinstance(result, dict):
        ticket_number = result.get("number", "N/A")
        ticket_id = result.get("id")

    return ticket_number, ticket_id

def generate_ticket_summary(history: list, severity: Optional[str]):
    history = history or []
    history_text = "\n".join(history)
    severity = severity or "moderada"  # normalize Optional[str] → str

    response = llm.invoke([
        SystemMessage(content=(
            "Eres un ingeniero de soporte experto. "
            "Responde SOLO en formato indicado. "
            "Sin saludos, sin preguntas, sin texto adicional."
        )),
        HumanMessage(content=f"""
Convierte este diagnóstico en un ticket de soporte.

REGLAS PARA EL TÍTULO:
- Máximo 10 palabras, descriptivo del problema principal.

REGLAS PARA LA DESCRIPCIÓN:
- Primero escribe un párrafo corto (2-3 oraciones) describiendo el problema: qué falla, en qué sistema, desde cuándo y a cuántos afecta.
- Luego, bajo el título "Pasos realizados:", lista las acciones técnicas concretas que ocurrieron durante el diagnóstico, en cualquiera de estos casos:
    1. El usuario mencionó que ejecutó algo, intentó algo, o vio algo técnico por su cuenta.
    2. El técnico le pidió al usuario hacer algo Y el usuario respondió con un resultado concreto.
    3. El usuario mencionó haber intentado algo antes de contactar soporte.
- La acción del paso es lo que se ejecutó o revisó. El resultado es lo que apareció o respondió el usuario.
- No incluyas pasos donde el usuario solo describió el problema sin ejecutar nada.
- No incluyas pasos donde el técnico pidió algo pero el usuario no respondió con un resultado.
- No inventes pasos. Solo lo que está explícito en el historial.
- Si no hubo ninguna acción técnica, omite la sección "Pasos realizados:" por completo.
- Formato de cada paso: "Paso N: [acción realizada] → [resultado o mensaje que apareció]."

DIAGNOSTICO:
{history_text}

SEVERIDAD: {severity}

Devuelve EXACTAMENTE en este formato:

TITULO: [titulo corto máximo 10 palabras]
DESCRIPCION:
[párrafo describiendo el problema]

Pasos realizados:
Paso 1: [acción] → [resultado]
Paso 2: [acción] → [resultado]
""")
    ])

    text = clean_html_entities(str(response.content or ""))
    title = "Incidente reportado por usuario"
    description = text

    if "TITULO" in text and "DESCRIPCION" in text:
        try:
            title = text.split("TITULO")[1].split("DESCRIPCION")[0].strip().lstrip(":").strip()
            description = text.split("DESCRIPCION:")[1].strip()
        except Exception as e:
            print(f"[generate_ticket_summary] Error parseando formato: {e}")

    return title, description

def generate_technical_details(history: list, description: str) -> str:
    history_text = "\n".join(history or [])

    response = llm.invoke([
        SystemMessage(content="Responde SOLO en JSON sin markdown ni explicaciones"),
        HumanMessage(content=f"""
Del siguiente historial de soporte extrae datos si están disponibles:

{{
    "sistema_afectado": "aplicación, servicio o equipo afectado, o null",
    "alcance": "1 usuario / varios usuarios / toda la empresa, o null",
    "error_exacto": "código o mensaje de error exacto si se mencionó, o null",
    "cuando_inicio": "cuándo empezó el problema si se mencionó, o null"
}}

Historial:
{history_text}

Descripcion:
{description}
""")
    ])

    try:
        text = re.sub(r"```json|```", "", str(response.content)).strip()
        data = json.loads(text)
    except Exception as e:
        print(f"[generate_technical_details] Error parseando JSON: {e}")
        data = {}

    campos = {
        "sistema_afectado": "Sistema Afectado",
        "alcance": "Alcance",
        "error_exacto": "Error exacto",
        "cuando_inicio": "Inicio del problema",
    }

    lineas = [
        f"{label}: {data[key]}"
        for key, label in campos.items()
        if data.get(key) and str(data[key]).lower() not in ("null", "none", "")
    ]

    return "\n".join(lineas) if lineas else "Sin detalles tecnicos adicionales."

def clean_html_entities(text: str) -> str:
    return (text
            .replace("&quot;", '"')
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&#39;", "'"))

def get_or_request_email(state: State, next_intent: str):
    from .integrations import user_has_email
    thread_id = state.get("thread_id") or ""
    messages_state = state.get("messages", [])
    last_msg = str(messages_state[-1].content).strip().lower() if messages_state else ""

    if state.get("email_skipped"):
        return None
    
    if user_has_email(thread_id):
        return None

    # Si ya estamos en el paso de pedir email, validamos la respuesta
    if state.get("email_request_step") == "ask_email":
        # 1. ¿Es un correo válido?
        email_match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", last_msg)
        if email_match:
            email = email_match.group(0).lower()
            db = SessionLocal()
            try:
                db.execute(
                    update(users)
                    .where(users.c.phone_number == thread_id)
                    .values(email=email)
                )
                db.commit()
                print(f"Email guardado para {thread_id}: {email}")
                return None # Ya tenemos email, continuar al flujo original
            finally:
                db.close()
        
        # 2. ¿Quiere saltar el paso?
        skip_intent = detect_intent_simple(
            last_msg,
            {
                "skip": "dice que no tiene, que no quiere darlo, que no se lo sabe, saltar",
                "otro": "otra cosa"
            }
        )
        if "skip" in skip_intent:
            return {"email_skipped": True, "email_request_step": None, "pending_intent": None}

        # 3. No es email ni quiere saltar -> Re-preguntar
        return {
            "messages": [AIMessage(content="Para continuar necesito un correo electrónico válido. ¿Me lo confirmas por favor? (o dime 'no tengo' para saltar)")]
        }
    
    db = SessionLocal()
    try:
        user = db.execute(
            select(users).where(users.c.phone_number == thread_id)
        ).fetchone()
        name = user.name if user and user.name and user.name not in ("pending", "", None) else "usuario"
    finally:
        db.close()

    accion = "crear el ticket" if next_intent == "crear_ticket" else "contactar al agente"

    return {
        "pending_intent": next_intent,
        "email_request_step": "ask_email",
        "support_option_step": None,
        "diagnosis_step": None,
        "diagnosis_history": state.get("diagnosis_history") or [],
        "diagnosis_images": state.get("diagnosis_images") or [],
        "severity": state.get("severity"),
        "servicio": state.get("servicio"),
        "messages": [AIMessage(
            content=f"Para {accion} necesito tu correo, {name}. ¿Cuál es?"
        )]
    }