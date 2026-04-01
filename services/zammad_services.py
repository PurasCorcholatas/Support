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
    url = f"{ZAMMAD_URL}/api/v1/tickets"
    print(f"[POLLING] Consultando: {url}")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Token token={ZAMMAD_TOKEN}"},
            params={
                "state_id": 4,
                "limit": 50,
                "expand": "true"
            }
        )

    if resp.status_code != 200:
        print(f"[POLLING] Error consultando Zammad: {resp.status_code} {resp.text[:200]}")
        return []

    tickets = resp.json()
    if not isinstance(tickets, list):
        print(f"[POLLING] Respuesta inesperada: {type(tickets)}")
        return []

    since = datetime.now(timezone.utc) - timedelta(minutes=2)
    recently_closed = []

    for ticket in tickets:
        close_at_raw = ticket.get("close_at") or ticket.get("updated_at")
        if not close_at_raw:
            continue
        try:
            close_at = datetime.fromisoformat(close_at_raw.replace("Z", "+00:00"))
            if close_at >= since:
                recently_closed.append(ticket)
                print(f"[POLLING] ✅ Ticket reciente encontrado: #{ticket.get('number')} cerrado a las {close_at}")
        except Exception as e:
            print(f"[POLLING] Error parseando fecha #{ticket.get('number')}: {e}")
            continue

    print(f"[POLLING] {len(recently_closed)} tickets cerrados en los últimos 2 minutos")
    return recently_closed


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

    mark_as_notified(ticket_number)

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

        mensaje = (
            f"Tu incidencia ha sido resuelta ✅\n\n"
            f"Ticket: #{ticket_number}\n"
            f"Asunto: {ticket_title}\n"
            f"Atendido por: {agent_name}"
        )

        if close_note:
            mensaje += f"\n\nNota del técnico: {close_note}"

        msg_resp = await client.post(
            f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": mensaje, "message_type": "outgoing", "private": False}
        )

        if msg_resp.status_code in (200, 201):
            print(f"[POLLING] ✅ Notificado — Ticket #{ticket_number} → {phone_number}")
        else:
            print(f"[POLLING] Error enviando mensaje: {msg_resp.status_code} {msg_resp.text[:200]}")


async def start_zammad_polling(interval: int = 60):
    print(f"[POLLING] Iniciando polling cada {interval}s")
    await asyncio.sleep(10)
    while True:
        try:
            tickets = await get_recently_closed_tickets()
            print(f"[POLLING] Revisando... {len(tickets)} tickets cerrados encontrados")
            for ticket in tickets:
                await notify_closed_ticket(ticket)
        except Exception as e:
            print(f"[POLLING] Error en ciclo: {e}")
        await asyncio.sleep(interval)