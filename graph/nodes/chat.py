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


def chat_general(state: State, config):
    system_prompt = """
Eres Santiago, técnico de soporte de Serviunix hablando por WhatsApp.

ESTILO:
- Conversacional, directo, como una persona real
- Nunca digas "describe tu problema" o "proporcione información"
- Usa frases como: "cuéntame qué está pasando", "revisemos eso", "mira, puede ser que..."
- Sin signos de exclamación, sin listas innecesarias

Haz preguntas de diagnóstico cuando haya un problema técnico.
"""
    messages_state = state.get("messages", [])

    if not any(isinstance(m, SystemMessage) for m in messages_state):
        messages_state = [SystemMessage(content=system_prompt)] + messages_state

    response = llm.invoke(messages_state)
    return {"messages": [response]}