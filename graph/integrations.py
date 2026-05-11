import os
import json
import httpx
import re
from typing import Optional
from sqlalchemy import select, insert, update
from config.db import SessionLocal
from models.user_memory import user_memory
from models.users import users
from langchain_core.messages import SystemMessage, HumanMessage
from .helpers import clean_html_entities
from .llms import llm, llm_diagnosis
import asyncio

SERVICIO_VALUES: dict = {}
_servicio_values_lock = asyncio.Lock()

def save_conversation_memory(thread_id: str, diagnosis_history: list):
    if not diagnosis_history:
        return

    history_text = "\n".join(diagnosis_history)

    response = llm.invoke([
        SystemMessage(content="Responde SOLO en JSON. Sin markdown ni explicaciones."),
        HumanMessage(content=f"""
Analiza este diagnostico de soporte y devuelve:

{{
    "summary": "resumen del problema en 1 oracion",
    "category": "una de: correo, vpn, red, acceso, aplicacion, otro"
}}

DIAGNOSTICO:
{history_text}
""")
    ])

    try:
        text = re.sub(r"```json|```", "", str(response.content)).strip()
        data = json.loads(text)
        summary = data.get("summary", "")
        category = data.get("category", "otro")
    except Exception as e:
        print(f"[save_conversation_memory] Error parseando JSON: {e}")
        summary = history_text[:200]
        category = "otro"

    db = SessionLocal()
    try:
        db.execute(
            insert(user_memory).values(
                phone_number=thread_id,
                summary=summary,
                category=category,
            )
        )
        db.commit()
        print(f"Memoria guardada: {summary}")
    except Exception as e:
        print(f"[save_conversation_memory] Error guardando en BD: {e}")
        db.rollback()
    finally:
        db.close()

def user_has_email(thread_id: str) -> bool:
    db = SessionLocal()
    try:
        user = db.execute(
            select(users).where(users.c.phone_number == thread_id)
        ).fetchone()

        if not user:
            return False

        email = (user.email or "").strip().lower()
        print("DEBUG user_has_email RAW:", repr(email))

        invalid_values = {"", "null", "none", "pending", "sin correo", "na", "n/a"}
        if email in invalid_values:
            return False

        return bool(re.match(r"^[^@]+@[^@]+\.[^@]+$", email))
    finally:
        db.close()

def get_user_email(thread_id: str) -> Optional[str]:
    db = SessionLocal()
    try:
        user = db.execute(select(users).where(users.c.phone_number == thread_id)).fetchone()
        if user and user.email and user.email not in ("pending", "", None):
            return user.email
        return None
    finally:
        db.close()

async def update_chatwoot_contact(phone_number: str, name: str = "", email: str = "", company: str = ""):
    base_url = os.environ.get("CHATWOOT_URL", "")
    token = os.environ.get("CHATWOOT_API_TOKEN", "")
    account_id = os.environ.get("CHATWOOT_ACCOUNT_ID", "1")

    if not base_url or not token:
        print("[update_chatwoot_contact] CHATWOOT_URL o CHATWOOT_API_TOKEN no configurados")
        return

    try:
        async with httpx.AsyncClient() as client:
            headers = {"api_access_token": token}

            search = await client.get(
                f"{base_url}/api/v1/accounts/{account_id}/contacts/search",
                params={"q": phone_number},
                headers=headers,
                timeout=10,
            )
            results = search.json().get("payload", [])
            if not results:
                print(f"Chatwoot: contacto no encontrado para {phone_number}")
                return

            contact_id = results[0]["id"]

            payload = {}
            if name:
                payload["name"] = name
            if email:
                payload["email"] = email

            if payload:
                await client.put(
                    f"{base_url}/api/v1/accounts/{account_id}/contacts/{contact_id}",
                    json=payload,
                    headers=headers,
                    timeout=10,
                )
                print(f"Chatwoot contacto actualizado para {phone_number}: {payload}")

            if company:
                company_search = await client.get(
                    f"{base_url}/api/v1/accounts/{account_id}/companies/search",
                    params={"q": company},
                    headers=headers,
                    timeout=10,
                )
                company_results = company_search.json().get("payload", [])

                if company_results:
                    company_id = company_results[0]["id"]
                else:
                    company_create = await client.post(
                        f"{base_url}/api/v1/accounts/{account_id}/companies",
                        json={"name": company},
                        headers=headers,
                        timeout=10,
                    )
                    company_id = company_create.json().get("id")

                await client.put(
                    f"{base_url}/api/v1/accounts/{account_id}/contacts/{contact_id}",
                    json={"additional_attributes": {"company_name": company}},
                    headers=headers,
                    timeout=10,
                )
                print(f"Chatwoot company_name actualizado: {company}")

    except Exception as e:
        print(f"[update_chatwoot_contact] Error: {e}")

def detected_incident_severity(history_text: str):
    response = llm.invoke([
        SystemMessage(content="Eres un experto en soporte IT."),
        HumanMessage(content=f"""
Analiza el incidente y responde SOLO en JSON sin markdown:
{{
    "prioridad": "baja / moderada / alta / critica",
    "servicio": "una de: Analisis de Datos / Backups / Computador - Impresiora / Consultoria / Correo Electronico / Datacenter Serviunix / ERP / Maquinas Virtuales / Nextcloud - Samba - Alfreso / Otras Aplicaciones / Proxy - Firewall / Redes"
}}

Criterios de prioridad:
- critica: servidores caidos, red corporativa caida, ERP sin funcionar, datacenter, afecta toda la empresa
- alta: VPN, correo sin funcionar, aplicaciones criticas, afecta varios usuarios
- moderada: errores en aplicaciones, problemas de acceso, afecta un area
- baja: consultas, configuracion, problemas menores, un solo usuario

INCIDENTE:
{history_text}
""")
    ])

    try:
        text = re.sub(r"```json|```", "", str(response.content)).strip()
        data = json.loads(text)
        prioridad = data.get("prioridad", "moderada").lower()
        servicio = data.get("servicio", "Otras Aplicaciones")
    except Exception as e:
        print(f"[detected_incident_severity] Error parseando JSON: {e}")
        prioridad = "moderada"
        servicio = "Otras Aplicaciones"

    return prioridad, servicio

async def attach_images_to_zammad(ticket_id: Optional[int], images: list[dict]):
    if ticket_id is None or not images:
        print("[attach_images_to_zammad] ticket_id es None o no hay imágenes")
        return
    zammad_url = os.environ.get("ZAMMAD_URL", "")
    zammad_token = os.environ.get("ZAMMAD_HTTP_TOKEN", "")

    if not zammad_url or not zammad_token:
        print("[attach_images_to_zammad] ZAMMAD_URL o ZAMMAD_HTTP_TOKEN no configurados")
        return

    try:
        attachments = []
        for i, img in enumerate(images):
            raw_data = img["data"]
            if "," in raw_data and raw_data.startswith("data:"):
                raw_data = raw_data.split(",", 1)[1]
            
            attachments.append({
                "filename": f"evidencia_{i+1}.jpg",
                "data": raw_data,
                "mime-type": img.get("mime_type", "image/jpeg"),
            })

        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{zammad_url}/api/v1/ticket_articles",
                json={
                    "ticket_id": ticket_id,
                    "body": "Evidencia visual adjunta (imágenes/vídeo)",
                    "type": "note",
                    "internal": True,
                    "attachments": attachments
                },
                headers={
                    "Authorization": f"Token token={zammad_token}",
                    "Content-Type": "application/json",
                },
                timeout=30
            )
            r.raise_for_status()
            print(f"{len(images)} imágenes adjuntadas al ticket {ticket_id} en una sola nota")

    except Exception as e:
        print(f"[attach_images_to_zammad] Error: {e}")

async def load_servicio_values():
    global SERVICIO_VALUES
    async with _servicio_values_lock:
        zammad_url = os.environ.get("ZAMMAD_URL", "")
        zammad_token = os.environ.get("ZAMMAD_HTTP_TOKEN", "")

        if not zammad_url or not zammad_token:
            print("[load_servicio_values] ZAMMAD_URL o ZAMMAD_HTTP_TOKEN no configurados")
            return

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{zammad_url}/api/v1/object_manager_attributes",
                    headers={
                        "Authorization": f"Token token={zammad_token}",
                        "Content-Type": "application/json",
                    },
                    timeout=10,
                )
                r.raise_for_status()
                attrs = r.json()
                for attr in attrs:
                    if attr.get("name") == "servicio" and attr.get("object") == "Ticket":
                        options = attr["data_option"]["options"]
                        SERVICIO_VALUES = {item["name"]: item["value"] for item in options}
                        print(f"[load_servicio_values] {len(SERVICIO_VALUES)} servicios cargados")
                        return
            print("[load_servicio_values] Campo 'servicio' no encontrado en Zammad")
        except Exception as e:
            print(f"[load_servicio_values] Error: {e}")

def get_servicio_value(nombre: Optional[str]) -> str:
    if not nombre:
        return "otras_aplicaciones"
    if nombre in SERVICIO_VALUES:
        return SERVICIO_VALUES[nombre]

    nombre_lower = nombre.lower().strip()
    for key, value in SERVICIO_VALUES.items():
        if key.lower().strip() == nombre_lower:
            return value

    return "otras_aplicaciones"