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


async def quick_fix_flow(state: State):
    thread_id = state.get("thread_id") or ""
    history = state.get("diagnosis_history") or []
    history_text = "\n".join(history)
    quick_step = state.get("quick_fix_step")
    messages_state = state.get("messages", [])
    last_user_message = str(messages_state[-1].content).strip()

    if quick_step == "waiting_result":
        resultado = detect_intent_simple(
            last_user_message.lower(),
            {
                "funciono": "dice que si funciono, que pudo entrar, que ya esta bien, que se resolvio",
                "no_funciono": "dice que no funciono, que sigue igual, que no pudo, que el problema persiste",
                "otro": "otra cosa"
            }
        )

        if "no_funciono" in resultado:
            return {
                "quick_fix_step": None,
                "quick_fix_solution": None,
                "support_option_step": "waiting_choice",
                "support_option_retries": 0,
                "intent": "support_options",
                "messages": [AIMessage(
                    content="Ok, esto ya necesita que lo revise un tecnico.\n"
                            "¿Prefieres que cree un ticket o te paso a alguien del equipo?"
                )]
            }

        if "funciono" in resultado:
            solucion_aplicada = state.get("quick_fix_solution") or ""
            if thread_id and solucion_aplicada:
                db = SessionLocal()
                try:
                    db.execute(
                        insert(user_memory).values(
                            phone_number=thread_id,
                            summary=f"[Solucionado] {solucion_aplicada}",
                            category=(state.get("servicio") or "otro").lower(),
                        )
                    )
                    db.commit()
                except Exception as e:
                    print(f"[quick_fix_flow] Error guardando solucion en memoria: {e}")
                    db.rollback()
                finally:
                    db.close()

            return {
                "quick_fix_step": None,
                "quick_fix_solution": None,
                "diagnosis_step": None,
                "intent": "chat_general",
                "messages": [AIMessage(content="Que bueno que quedo. Cualquier otra cosa me avisas")]
            }

        return {
            "quick_fix_step": "waiting_result",
            "messages": [AIMessage(content="¿Pudiste solucionarlo o sigue igual?")]
        }

    memory_context = ""
    db = SessionLocal()
    try:
        mem_rows = db.execute(
            select(user_memory)
            .where(user_memory.c.phone_number == thread_id)
            .order_by(user_memory.c.created_at.desc())
            .limit(5)
        ).fetchall()
        if mem_rows:
            memory_context = "\n".join([f"-[{r.category}] {r.summary}" for r in mem_rows])
    except Exception as e:
        print(f"[quick_fix_flow] Error cargando memoria: {e}")
    finally:
        db.close()

    response = llm_diagnosis.invoke([
        SystemMessage(content="""
Eres un técnico de soporte senior de Serviunix.

Tu trabajo es evaluar si el problema tiene UNA solución que el usuario puede ejecutar solo.

Puedes resolver (can_fix: true) si el problema se soluciona con pasos simples 
que el usuario puede ejecutar sin conocimientos técnicos y sin que un técnico 
acceda a su equipo remotamente.

PUEDES resolver:
- Borrar caché, cookies, modo incógnito
- Reiniciar equipo, aplicación o servicio simple
- Reconectar impresora, mouse, teclado, unidad de red
- Liberar/renovar IP o flush DNS (con comandos simples)
- Cerrar procesos desde administrador de tareas
- Verificar configuración básica (fecha/hora, spam, permisos de carpeta)
- Reparar perfil de Outlook o reconectar cuenta de correo básica

NO PUEDES resolver (can_fix: false):
- Problemas de servidor, VPN o red corporativa completa
- Configuración avanzada de dominio o Active Directory
- Errores de ERP o aplicaciones empresariales complejas
- Migraciones o instalaciones de software nuevo
- Problemas que requieren acceso remoto al equipo
- Varios usuarios afectados al mismo tiempo

Responde SOLO en JSON sin markdown:
{
  "can_fix": true o false,
  "razon": "una frase breve",
  "pasos": ["paso 1", "paso 2", "paso 3"],
  "solucion_resumen": "ej: borrar caché de Chrome"
}
Los campos pasos y solucion_resumen solo van si can_fix es true. Máximo 3 pasos simples.
"""),
        HumanMessage(content=f"""
Historial del problema:
{history_text}

Memoria de problemas anteriores de este usuario:
{memory_context if memory_context else "Sin historial previo"}
""")
    ])

    try:
        text = re.sub(r"```json|```", "", str(response.content)).strip()
        data = json.loads(text)
        can_fix = data.get("can_fix", False)
        pasos = data.get("pasos", [])
        solucion_resumen = data.get("solucion_resumen", "")
    except Exception as e:
        print(f"[quick_fix_flow] Error parseando JSON: {e}")
        can_fix = False
        pasos = []
        solucion_resumen = ""

    if can_fix and pasos:
        pasos_texto = "\n".join(f"{i+1}. {p}" for i, p in enumerate(pasos))
        return {
            "quick_fix_step": "waiting_result",
            "quick_fix_solution": solucion_resumen,
            "diagnosis_images": state.get("diagnosis_images") or [],
            "messages": [AIMessage(
                content=f"Prueba esto:\n\n{pasos_texto}\n\n¿Funciono o sigue igual?"
            )]
        }

    severity = state.get("severity") or "moderada"
    servicio = state.get("servicio") or "Otro"
    
    resumen_problema = llm_diagnosis.invoke([
        SystemMessage(
            content="Responde SOLO con una frase corta de maximo 6 palabras que describa el problema tecnico. Sin puntos, sin mayusculas al inicio, sin explicaciones"
        ),
        HumanMessage(
            content=f"El problema diagnosticado fue:\n{history_text}"
        )
    ])
    description_butt = str(resumen_problema.content).strip().rstrip(".")

    return {
        "quick_fix_step": None,
        "support_option_step": "waiting_choice",
        "support_option_retries": 0,
        "intent": "support_options",
        "severity": severity,
        "servicio": servicio,
        "diagnosis_images": state.get("diagnosis_images"),
        "messages": [AIMessage(
            content=f"Ok, lo del {description_butt} parece algo que hay que escalar.\n"
                    "¿Prefieres que cree un ticket o te paso con alguien del equipo?"
        )]
    }