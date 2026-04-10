from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import ToolNode
from langchain_anthropic import ChatAnthropic

from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage, AIMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from config.db import LANGGRAPH_DB_URL
from typing import List, Literal, Annotated, Optional
from typing_extensions import TypedDict
from dotenv import load_dotenv

from sqlalchemy import select, insert, update
import re
import json
import httpx
import asyncio
import concurrent.futures

from config.db import SessionLocal
from models.users import users
from models.conversations import conversation
from models.messages import messages
from models.user_memory import user_memory

from services.mcp_client import get_mcp_tools

from security.profile_engine import (
    build_account_profile,
    generate_adaptive_questions
)
from security.question_engine import (
    QUESTION_BANK,
    select_initial_quetions,
    select_additional_question,
    get_question_text
)

from security.scoring_engine import calculate_security_score
from services.zimbra_service import ZimbraService

import smtplib
from email.mime.text import MIMEText
import os

load_dotenv()

from anthropic import Anthropic
from langchain_anthropic import ChatAnthropic
import os

os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY", "")

llm_diagnosis = ChatAnthropic(
    model_name="claude-sonnet-4-6",
    temperature=0,
    timeout=60,
    stop=None,
)

llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
)

llm_with_tools = None
tool_node = None
graph = None
tools = []
checkpointer = None
_db_connection = None

 

PRIORIDAD_MAP = {
    "baja":     "1 baja",
    "moderada": "2 moderada",
    "alta":     "3 alta",
    "critica":  "4 critica",
}


class State(TypedDict, total=False):

    human_escalated: Optional[bool]

    messages: Annotated[List[BaseMessage], add_messages]
    intent: Literal[
        "chat_general",
        "crear_ticket",
        "human",
        "estado_ticket",
        "greeting_flow"]

    greeting_step: Optional[Literal[
        "start",
        "wait_user_reply",
        "confirm_branch",
        "waiting_problem",
        "register_name",
        "register_company",
        "register_email",
        "register_sede",
        "register_company_sede"
    ]]

    branch: Optional[str]
    servicio: Optional[str]
    diagnosis_step: Optional[int]
    diagnosis_history: Optional[List[str]]
    diagnosis_summary: Optional[str]

    support_option_step: Optional[Literal[
        "offer_options",
        "waiting_choice",
        "similar_detected",
        "waiting_similar"
    ]]

    password_step: Optional[Literal[
        "confirm_owner",
        "ask_email",
        "ask_recent_change",
        "ask_send_email",
        "dynamic_question",
        "waiting_confirmation",
        "confirmation_continue",
        "done"
    ]]
    security_risk: Optional[int]
    real_data: Optional[dict]
    security_questions: Optional[List[str]]
    user_answers: Optional[dict]

    thread_id: Optional[str]

    ticket_step: Optional[Literal[
        "ask_user_info",
        "ask_title",
        "ask_description",
        "ask_email",
        "done"
    ]]

    ticket_status_step: Optional[Literal["ask_id"]]

    description: Optional[str]
    current_question_index: Optional[int]
    email: Optional[str]
    validation_summary: Optional[dict]
    severity: Optional[str]

    email_request_step: Optional[Literal["ask_email"]]
    pending_intent: Optional[str]


_locks: dict[str, asyncio.Lock] = {}
_pending: dict[str, list[str]] = {}


def get_lock(thread_id: str) -> asyncio.Lock:
    if thread_id not in _locks:
        _locks[thread_id] = asyncio.Lock()
    return _locks[thread_id]


async def init_llm_with_tools():

    global llm_with_tools
    global tool_node
    global graph
    global tools

    tools = await get_mcp_tools()

    for t in tools:
        if t.name == "zammad_update_ticket":
            # print("=== SCHEMA zammad_update_ticket ===")
            # print(t.args_schema)
            break

    llm_with_tools = llm.bind_tools(tools)
    tool_node = ToolNode(tools)

    await load_servicio_values()
 

    builder = StateGraph(State)

    builder.add_node("router", router)
    builder.add_node("chat_general", chat_general)
    builder.add_node("greeting_flow", greeting_flow)
    builder.add_node("diagnosis_flow", diagnosis_flow)
    builder.add_node("support_options", offer_support_options)
    builder.add_node("support_agent", support_agent)
    builder.add_node("check_ticket_status", check_ticket_status)
    builder.add_node("escalate_human", escalate_human)
    builder.add_node("waiting_agent", waiting_agent)
    builder.add_node("tools", tool_node)

    builder.add_edge(START, "router")

    builder.add_conditional_edges(
        "router",
        lambda state: state["intent"],
        {
            "greeting_flow": "greeting_flow",
            "chat_general": "chat_general",
            "human": "escalate_human",
            "diagnosis_flow": "diagnosis_flow",
            "support_options": "support_options",
            "crear_ticket": "support_agent",
            "estado_ticket": "check_ticket_status",
            "waiting_agent": "waiting_agent",
        }
    )


    builder.add_conditional_edges(
        "support_options",
        lambda state: state.get("intent", "support_options"),
        {
            "crear_ticket": "support_agent",
            "human": "escalate_human",
            "support_options": END,
            "chat_general": END,
            "diagnosis_flow": "diagnosis_flow",
        }
    )

    builder.add_conditional_edges(
        "support_agent",
        lambda state: "tools" if getattr(state["messages"][-1], "tool_calls", None) else END,
        {
            "tools": "tools",
            END: END
        }
    )

    builder.add_edge("tools", END)
    builder.add_edge("diagnosis_flow", END)
    builder.add_edge("greeting_flow", END)
    builder.add_edge("waiting_agent", END)
    builder.add_edge("check_ticket_status", END)
    builder.add_edge("escalate_human", END)
    builder.add_edge("chat_general", END)

    from psycopg_pool import AsyncConnectionPool
    checkpointer = None
    _db_connection = None
    

    async with AsyncPostgresSaver.from_conn_string(LANGGRAPH_DB_URL) as tmp_checkpointer:
        await tmp_checkpointer.setup()

    _db_connection = AsyncConnectionPool(
        conninfo=LANGGRAPH_DB_URL,
        max_size=10,
        open=False
    )
    await _db_connection.open()
    checkpointer = AsyncPostgresSaver(_db_connection) #type: ignore
    graph = builder.compile(checkpointer=checkpointer)


def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


async def langgraph(mensaje: str, thread_id: str):
    if graph is None:
        raise Exception("Graph no inicializado")

    _pending.setdefault(thread_id, []).append(mensaje)

    await asyncio.sleep(5)

    current_batch = _pending.get(thread_id, [])
    if not current_batch or current_batch[-1] != mensaje:
        return None

    mensajes_batch = _pending.pop(thread_id, [mensaje])
    mensaje_combinado = " | ".join(mensajes_batch) if len(mensajes_batch) > 1 else mensajes_batch[0]

    async with get_lock(thread_id):
        db = SessionLocal()
        try:
            stmt_check = select(users).where(users.c.phone_number == thread_id)
            user_check = db.execute(stmt_check).fetchone()

            if user_check and user_check.bot_active is False:
                db.execute(
                    update(users)
                    .where(users.c.phone_number == thread_id)
                    .values(bot_active=True)
                )
                db.commit()

                state: State = {
                    "messages": [HumanMessage(content=mensaje_combinado)],
                    "intent": "greeting_flow",
                    "greeting_step": "confirm_branch",
                    "thread_id": thread_id,
                }
            else:
                state: State = {
                    "messages": [HumanMessage(content=mensaje_combinado)],
                    "thread_id": thread_id,
                }

            result = await graph.ainvoke(
                state,
                config={
                    "configurable": {"thread_id": thread_id},
                    "recursion_limit": 200
                }
            )

            all_ai = [m for m in result["messages"] if isinstance(m, AIMessage)]

            if not all_ai:
                return None

            last_ai = all_ai[-1]
            if isinstance(last_ai.content, list):
                final_text = " ".join(
                    block.get("text", "") for block in last_ai.content if isinstance(block, dict)
                ).strip()
            else:
                final_text = str(last_ai.content).strip()

            if not final_text:
                return None

            final_messages = [final_text]

            stmt_user = select(users).where(users.c.phone_number == thread_id)
            user = db.execute(stmt_user).fetchone()

            if user:
                stmt_conv = select(conversation).where(
                    conversation.c.users == user.id,
                    conversation.c.status == "open"
                )
                conv = db.execute(stmt_conv).fetchone()

                if conv:
                    conversation_id = conv.id
                    db.execute(
                        insert(messages).values(
                            conversation_id=conversation_id,
                            sender="user",
                            message_text=mensaje_combinado,
                            company=user.company
                        )
                    )
                    db.execute(
                        insert(messages).values(
                            conversation_id=conversation_id,
                            sender="bot",
                            message_text=final_messages[-1],
                            company=user.company
                        )
                    )

                db.commit()

            _locks.pop(thread_id, None)
            return final_messages

        finally:
            db.close()


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

EJEMPLOS DE TONO:
Malo: "¡Hola! ¿En qué puedo ayudarte hoy?"
Bueno: "Hola Simon como estas, ¿Te encuentras en Bello"

Malo: "Por favor indícame el nombre de tu empresa."
Bueno: "¿Me podrias dar el nombre de tu empresa?"

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

    response = llm.invoke([
        SystemMessage(content="Responde SOLO con una de las claves indicadas. Sin explicaciones."),
        HumanMessage(content=f"""
{context}

El usuario dijo: "{user_text}"

Opciones:
{options_text}

Responde SOLO con la clave exacta.
""")
    ])
    return str(response.content).strip().lower()


def extract_sede(user_message):
    response = llm.invoke([
        SystemMessage(content="""
Extrae únicamente el nombre de la sede mencionada por el usuario.
Devuelve SOLO el nombre de la sede, máximo 3 palabras.
Si no hay una sede clara, responde: ninguna
"""),
        HumanMessage(content=user_message)
    ])

    sede = str(response.content).lower()
    sede = re.sub(r"[^a-záéíóúñ\s]", "", sede).strip()

    if sede == "ninguna" or len(sede) < 3:
        return ""

    return sede


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
        text = str(response.content).strip()
        text = re.sub(r"```json|```", "", text).strip()
        data = json.loads(text)
        summary = data.get("summary", "")
        category = data.get("category", "otro")
    except:
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
    finally:
        db.close()


def check_similar_problem(thread_id: str, current_problem: str):
    db = SessionLocal()
    try:
        result = db.execute(
            select(user_memory)
            .where(user_memory.c.phone_number == thread_id)
            .order_by(user_memory.c.created_at.desc())
            .limit(5)
        ).fetchall()

        if not result:
            return None

        memory_text = "\n".join([
            f"- [{row.category}] {row.summary}" for row in result
        ])

        response = llm.invoke([
            SystemMessage(content="Responde SOLO en JSON. Sin markdown ni explicaciones."),
            HumanMessage(content=f"""
El usuario tiene este historial de problemas anteriores:
{memory_text}

Ahora dice: "{current_problem}"

¿El problema actual es similar a alguno anterior?

{{
    "similar": true o false,
    "summary": "resumen del problema anterior si hay match, si no null"
}}
""")
        ])

        text = str(response.content).strip()
        text = re.sub(r"```json|```", "", text).strip()
        data = json.loads(text)

        if data.get("similar"):
            return data
        return None

    except Exception as e:
        print("Error memoria:", e)
        return None
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


def get_valid_user_email(thread_id: str):
    db = SessionLocal()
    try:
        user = db.execute(
            select(users).where(users.c.phone_number == thread_id)
        ).fetchone()

        if not user:
            return None

        email = (user.email or "").strip().lower()

        if re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            return email

        return None
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
    base_url = os.environ.get("CHATWOOT_URL")
    token = os.environ.get("CHATWOOT_API_TOKEN", "")
    account_id = os.environ.get("CHATWOOT_ACCOUNT_ID", "1")

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
                # Buscar si la empresa ya existe
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

                if company:
                    await client.put(
                        f"{base_url}/api/v1/accounts/{account_id}/contacts/{contact_id}",
                        json={
                            "additional_attributes": {
                                "company_name": company
                            }
                        },
                        headers=headers,
                        timeout=10,
                    )
                    print(f"Chatwoot company_name actualizado: {company}")
    except Exception as e:
        print(f"Error actualizando Chatwoot: {e}")
        
        
        
def get_or_request_email(state: State, next_intent: str):
    thread_id = state.get("thread_id") or ""

    print("DEBUG get_or_request_email thread_id:", thread_id)
    print("DEBUG get_or_request_email has_email:", user_has_email(thread_id))

    if user_has_email(thread_id):
        return None

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
        "messages": [AIMessage(
            content=f"Para {accion} necesito tu correo, {name}. ¿Cuál es?"
        )]
    }


async def greeting_flow(state: State):
    db = SessionLocal()
    try:
        thread_id = state.get("thread_id")
        step = state.get("greeting_step", "start")
        messages_state = state.get("messages", [])
        last_user_message = str(messages_state[-1].content).strip()

        stmt_user = select(users).where(users.c.phone_number == thread_id)
        user = db.execute(stmt_user).fetchone()

        if step == "start":
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
            return {
                "greeting_step": "register_name",
                "messages": [
                    AIMessage(content="Hola, Bienvenido al soporte técnico de Serviunix. ¿Con quién tengo el gusto?")
                ]
            }

        if step == "register_name":
            intent = detect_intent_simple(
                last_user_message,
                {
                    "es_nombre": "contiene o es el nombre de una persona real, aunque tenga palabras como 'con', 'soy', 'me llamo', 'mi nombre es' antes del nombre",
                    "otro": "es un saludo sin nombre, pregunta, queja, o texto no contiene ningun nombre propio"
                }
            )

            if "otro" in intent:
                response = llm.invoke([
                    SystemMessage(content="""
Eres Santiago, soporte técnico de Serviunix por WhatsApp.
Responde brevemente y de forma natural a lo que dice el usuario,
pero al final pregunta su nombre de forma natural.
Máximo 2 oraciones, sin signos de exclamación.
"""),
                    HumanMessage(content=f"El usuario dijo: {last_user_message}")
                ])
                return {
                    "greeting_step": "register_name",
                    "messages": [AIMessage(content=response.content)]
                }

            extract = llm.invoke([
                SystemMessage(content="Extrae SOLO el nombre propio de la persona. Devuelve únicamente el nombre, sin más texto."),
                HumanMessage(content=f"El usuario dijo: {last_user_message}")
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
            await update_chatwoot_contact(phone_number=thread_id or "", name=name or "")

            return {
                "greeting_step": "register_company_sede",
                "messages": [AIMessage(
                    content=f"Mucho gusto, {name}. ¿En qué empresa trabajas y desde qué sede te comunicas?"
                )]
            }

        if step == "register_company_sede":
            extract_response = llm.invoke([
                SystemMessage(content="Responde SOLO en JSON sin markdown. Sin explicaciones."),
                HumanMessage(content=f"""
El usuario dijo esto cuando se le preguntó empresa y sede: "{last_user_message}"

Extrae:
{{
    "empresa": "nombre de la empresa o null si no se menciona",
    "sede": "nombre de la sede o null si no se menciona",
    "es_respuesta_valida": true o false
}}

es_respuesta_valida es true si el mensaje contiene al menos el nombre de una empresa reconocible.
""")
            ])

            empresa = None
            sede = None
            es_valida = False

            try:
                text = str(extract_response.content).strip()
                text = re.sub(r"```json|```", "", text).strip()
                data = json.loads(text)
                empresa = data.get("empresa")
                sede = data.get("sede")
                es_valida = data.get("es_respuesta_valida", False)
            except:
                pass

            if not es_valida or not empresa:
                response = llm.invoke([
                    SystemMessage(content="""
Eres Santiago, soporte técnico de Serviunix por WhatsApp.
Responde brevemente a lo que dice el usuario y luego pregúntale
el nombre de su empresa y sede en la misma pregunta.
Máximo 2 oraciones. Sin signos de exclamación.
"""),
                    HumanMessage(content=f"El usuario dijo: {last_user_message}")
                ])
                return {
                    "greeting_step": "register_company_sede",
                    "messages": [AIMessage(content=response.content)]
                }

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
            print(f"DEBUG empresa antes de chatwoot: {repr(empresa)}")
            await update_chatwoot_contact(phone_number=thread_id or "", company=empresa or "")

            name = user.name if user and user.name and user.name != "pending" else ""

            if sede:
                msg = f"Perfecto{', ' + name if name else ''}. Quedaste registrado desde {sede} en {empresa}. ¿En qué te puedo ayudar?"
            else:
                msg = f"Perfecto{', ' + name if name else ''}. Registrado en {empresa}. ¿En qué te puedo ayudar?"

            return {
                "greeting_step": "waiting_problem",
                "messages": [AIMessage(content=msg)]
            }

        if step == "confirm_branch":
            user = db.execute(stmt_user).fetchone()
            sede = user.sede if user and user.sede else None
            name = user.name if user and user.name and user.name != "pending" else "usuario"

            detected = detect_intent_simple(
                last_user_message,
                {
                    "confirma": "dice que sí, que sigue en esa sede",
                    "niega": "dice que no está en esa sede",
                    "otra_sede": "menciona una sede diferente",
                    "otro": "habla de otra cosa"
                },
                context=f"El bot preguntó si el usuario sigue en la sede {sede}"
            )

            if "confirma" in detected:
                db.execute(
                    update(users)
                    .where(users.c.phone_number == thread_id)
                    .values(greeting_step=None)
                )
                db.commit()
                return {
                    "greeting_step": "waiting_problem",
                    "messages": [AIMessage(content=f"Perfecto. ¿En qué te puedo ayudar, {name}?")]
                }

            if "niega" in detected or "otra_sede" in detected:
                sede_detected = extract_sede(last_user_message)
                if sede_detected:
                    db.execute(
                        update(users)
                        .where(users.c.phone_number == thread_id)
                        .values(sede=sede_detected, greeting_step=None)
                    )
                    db.commit()
                    return {
                        "greeting_step": "waiting_problem",
                        "messages": [AIMessage(content=f"Listo, te actualizo a {sede_detected}. ¿En qué te puedo ayudar?")]
                    }
                return {
                    "greeting_step": "confirm_branch",
                    "messages": [AIMessage(content=f"¿Desde qué sede te comunicas hoy, {name}?")]
                }

            return {
                "greeting_step": "confirm_branch",
                "messages": [guided_response(
                    objetivo=f"Responder al usuario y preguntarle si sigue en la sede {sede}.",
                    contexto=f"Usuario: {name}",
                    historial=[last_user_message]
                )]
            }

        return {}
    finally:
        db.close()


def diagnosis_flow(state: State):

    if llm_with_tools is None:
        raise Exception("Graph no inicializado")

    step = state.get("diagnosis_step") or 0
    history = state.get("diagnosis_history") or []
    messages_state = state.get("messages", [])
    last_msg_obj = messages_state[-1]

    if isinstance(last_msg_obj.content, list):
        image_context = llm_diagnosis.invoke([
            SystemMessage(content=(
                "Eres un técnico de soporte. Describe en texto plano el error que ves en la imagen. "
                "Incluye: aplicación, código de error, mensaje exacto, sistema operativo o VM afectada, "
                "y cualquier otro detalle visible. Máximo 4 oraciones."
            )),
            last_msg_obj
        ])
        last_user_message = f"[Imagen del error]: {str(image_context.content).strip()}"
    else:
        last_user_message = str(last_msg_obj.content)

    updated_history = history + [f"Usuario: {last_user_message}"]
    updated_history_text = "\n".join(updated_history)

    if step >= 2:
        last_bot_message = ""
        for entry in reversed(history):
            if entry.startswith("bot:"):
                last_bot_message = entry[4:].strip()
                break

        is_clarification = llm_diagnosis.invoke([
            SystemMessage(content="Responde SOLO con 'si' o 'no'. Sin explicaciones"),
            HumanMessage(content=f"""
El tecnico de soporte le hizo esta pregunta al usuario:
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
El usuario no entendió algo técnico que le preguntaste.

REGLAS ESTRICTAS:

1. PRIMERO detecta el nivel técnico del usuario por cómo escribió en el historial:
   - TÉCNICO: usa palabras como servidor, ping, SSH, IP, logs, firewall, DNS, terminal
   - USUARIO FINAL: escribe casual, dice "no me abre", "no funciona", no usa términos técnicos

2. Si pregunta por logs, terminal, SSH, comandos o configuración de servidor:
   - Si es TÉCNICO → explícale cómo hacerlo en 1 oración y repite la pregunta
   - Si es USUARIO FINAL → dile "no te preocupes por eso, yo lo escalo" y pregunta algo observable

3. Si pregunta por algo del navegador (caché, cookies):
   - Explícalo en 1 oración simple y repite la pregunta

4. Si no entiende un término técnico:
   - Tradúcelo a palabras cotidianas en 1 oración y repite la pregunta

TRADUCCIONES A LENGUAJE SIMPLE:
- "hacer ping" → "¿otros compañeros tienen el mismo problema?"
- "revisar logs" → "¿el error empezó de repente o después de algún cambio?"
- "cliente SMB" → "¿desde otro computador de la oficina puedes abrir esa carpeta?"
- "servicio caído" → "¿otras cosas de la empresa también fallan o solo esto?"
- "resolución DNS" → "¿puedes abrir otras páginas o sistemas de la empresa?"

ESTILO:
- Máximo 2 oraciones
- Sin comandos de terminal a usuarios finales
- Natural, directo, sin formalismos
"""),
                HumanMessage(content=f"""
Historial completo:
{chr(10).join(history)}

La pregunta de diagnóstico que hiciste fue:
"{last_bot_message}"

El usuario preguntó o no entendió:
"{last_user_message}"

Responde según su nivel técnico y luego repite la pregunta adaptada.
""")
            ])
            return {
                "diagnosis_step": step,
                "diagnosis_history": history,
                "messages": [AIMessage(content=str(clarification_response.content))]
            }

    if step >= 3:
        enough = llm_diagnosis.invoke([
    SystemMessage(content="Responde SOLO con 'si' o 'no'. Sin explicaciones."),
    HumanMessage(content=f"""
Eres un ingeniero de soporte senior. Revisa este historial.

HISTORIAL:
{updated_history_text}

¿Tienes suficiente para abrir un ticket útil?

Considera que ES suficiente si tienes:
- El error exacto o síntoma concreto (incluye imágenes descritas)
- El sistema afectado con detalle
- Cuándo empezó o qué contexto hay (aunque sea "de repente sin cambios")
- Si afecta a uno o varios usuarios

NO es necesario saber qué intentó el usuario si ya está claro que no ha intentado nada 
o si el problema es de infraestructura que el usuario no puede resolver solo.

Si tienes 3 de esas 4 cosas, responde 'si'.
""")
])

        tiene_suficiente = "si" in str(enough.content).strip().lower()

        if tiene_suficiente or step >= 8:
            severity, servicio = detected_incident_severity(updated_history_text)
            resumen_problema = llm_diagnosis.invoke([
                SystemMessage(
                    content="Responde SOLO con una frase corta de maximo 6 palabras que describa el problema tecnico. Sin puntos, sin mayusculas al inicio, sin explicaciones"
                ),
                HumanMessage(
                    content=f"El problema diagnosticado fue:\n{updated_history_text}"
                )
            ])
            description_butt = str(resumen_problema.content).strip().rstrip(".")

            return {
                "diagnosis_step": None,
                "diagnosis_history": updated_history,
                "support_option_step": "waiting_choice",
                "intent": "support_options",
                "severity": severity,
                "servicio": servicio,
                "messages": [
                    AIMessage(
                        content=(
                            f"Ok, lo del {description_butt} parece algo que hay que escalar.\n"
                            "¿Prefieres que cree un ticket o te paso a alguien del equipo?"
                        )
                    )
                ]
            }

    if step == 0 and not history:
        return {
            "diagnosis_step": 1,
            "diagnosis_history": [],
            "messages": [AIMessage(content="Cuéntame qué está pasando y te ayudo a crear el ticket.")]
        }

    response = llm_diagnosis.invoke([
    SystemMessage(content="""
Eres Santiago, técnico de soporte de Serviunix por WhatsApp.

Tu única tarea: escribir UNA pregunta corta al usuario. Nada más.

FORMATO DE SALIDA OBLIGATORIO:
- Solo la pregunta. Sin introducción, sin análisis, sin separadores.
- Máximo 1 oración.
- Sin signos de exclamación.

EJEMPLOS DE LO QUE DEBES DEVOLVER:
"¿Otras VMs también fallan o solo esa?"
"¿Esto empezó después de algún cambio o de repente?"
"¿Solo a ti te pasa o también a compañeros?"

EJEMPLOS DE LO QUE JAMÁS DEBES DEVOLVER:
"Por el historial, este usuario es técnico..." ← PROHIBIDO
"---" ← PROHIBIDO  
"Según lo que describes..." ← PROHIBIDO
Cualquier texto antes o después de la pregunta ← PROHIBIDO

Si ya tienes el error de una imagen, no preguntes qué error tiene.
Pregunta por lo que aún no sabes: cuándo empezó, si afecta a otros, qué ya intentaron.
"""),
    HumanMessage(content=f"""
HISTORIAL:
{updated_history_text}

Devuelve SOLO la siguiente pregunta. Una oración. Sin nada más.
""")
])


    question = str(getattr(response, "content", response)).strip()

    return {
        "diagnosis_step": step + 1,
        "diagnosis_history": updated_history + [f"bot: {question}"],
        "messages": [AIMessage(content=question)]
    }



def offer_support_options(state: State):

    messages_state = state.get("messages", [])
    last = str(messages_state[-1].content).lower()

    if state.get("support_option_step") == "similar_detected":
        similar = state.get("similar_problem", "un problema similar")
        return {
            "support_option_step": "waiting_similar",
            "messages": [AIMessage(
                content=(
                    f"Oye, veo que antes tuviste este problema: {similar}\n\n"
                    f"¿Es el mismo? Responde sí o no."
                )
            )]
        }

    if state.get("support_option_step") == "waiting_similar":
        detected = detect_intent_simple(
            last,
            {
                "confirma": "dice que sí, es el mismo problema, no es algo nuevo",
                "niega": "dice que no es lo mismo, que es un problema nuevo o diferente, o que es algo nuevo",
                "otro": "otra cosa"
            }
        )

        if "confirma" in detected:
            return {
                "support_option_step": "waiting_choice",
                "messages": [
                    AIMessage(
                        content=(
                            "Dale, es el mismo.\n"
                            "¿Prefieres que cree un ticket o te paso a alguien del equipo?"
                        )
                    )
                ]
            }

        if "niega" in detected:
            return {
                "support_option_step": None,
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
            "messages": [
                AIMessage(
                    content=(
                        "Esto ya parece un tema del sistema.\n"
                        "¿Prefieres que cree un ticket o te paso a alguien del equipo?"
                    )
                )
            ]
        }

    detected = detect_intent_simple(
        last,
        {
            "ticket": "quiere crear un ticket",
            "agente": "quiere hablar con un agente humano",
            "otro": "otra cosa"
        }
    )

    if "ticket" in detected:
        email_state = get_or_request_email(state, "crear_ticket")
        if email_state:
            return email_state
        return {
            "intent": "crear_ticket",
            "support_option_step": None,
            "diagnosis_step": None
        }

    if "agente" in detected:
        email_state = get_or_request_email(state, "human")
        if email_state:
            return email_state
        return {
            "intent": "human",
            "support_option_step": None,
            "diagnosis_step": None
        }

    return {
        "support_option_step": "waiting_choice",
        "messages": [guided_response(
            objetivo="Pedirle al usuario que elija entre crear un ticket (1) o hablar con alguien (2).",
            contexto="El usuario no eligió una opción válida.",
            historial=[last]
        )]
    }


def detected_incident_severity(history_text):
    response = llm.invoke([
        SystemMessage(
            content="Eres un experto en soporte IT."
        ),
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
    except:
        prioridad = "moderada"
        servicio = "Otras Aplicaciones"

    return prioridad, servicio


async def support_agent(state):
    print("ENTRANDO A SUPPORT AGENT")
    print(f"history: {state.get('diagnosis_history')}")
    print(f"severity: {state.get('severity')}")
    print(f"servicio: {state.get('servicio')}")

    if llm_with_tools is None:
        raise Exception("LLM no inicializado")

    history = state.get("diagnosis_history") or []
    severity = state.get("severity", "moderada")
    thread_id = state.get("thread_id") or ""
    servicio_nombre = state.get("servicio", "Otras Aplicaciones")
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

    if create_tool is None:
        return {
            "intent": "human",
            "messages": [AIMessage(content="No pude crear, te conecto con alguien.")]
        }

    db = SessionLocal()
    try:
        stmt_user = select(users).where(users.c.phone_number == thread_id)
        user = db.execute(stmt_user).fetchone()

        # Validar email correctamente
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
            print(f"[support_agent] WARN: usuario {thread_id} sin email válido, ticket se creará con thread_id como customer")

        user_name = (user.name if user else None) or ""

        if user:
            user_info = (
                "Informacion del usuario\n\n"
                f"Nombre: {user.name or 'No registrado'}\n"
                f"Empresa: {user.company if user.company and user.company != 'pending' else 'No registrada'}\n"
                f"Telefono: {thread_id}\n"
                f"Correo: {customer or 'No registrado'}\n"
                f"Sede: {user.sede or 'No registrada'}\n\n"
                "Diagnostico del problema\n\n"
                f"{description}"
            )
        else:
            user_info = description

    finally:
        db.close()

    if create_user_tool and customer:
        try:
            name_parts = user_name.strip().split(" ", 1)
            firstname = name_parts[0] if name_parts else "Usuario"
            lastname = name_parts[1] if len(name_parts) > 1 else "Soporte"
            await create_user_tool.ainvoke({
                "params": {
                    "email": customer,
                    "firstname": firstname,
                    "lastname": lastname,
                }
            })
            print(f"Usuario asegurado en Zammad: {customer}")
        except Exception as e:
            print(f"Usuario ya existe o error al crear en Zammad: {e}")

    result = await create_tool.ainvoke({
        "params": {
            "title": title,
            "group": "Users",
            "customer": customer if customer else thread_id,
            "article_body": user_info,
            "priority": PRIORIDAD_MAP.get(severity, "2 normal"),
        }
    })

    print("Resultado create_ticket", result)

    ticket_number = "N/A"
    ticket_id = None

    if isinstance(result, list) and len(result) > 0:
        text = result[0].get("text", "")
        try:
            data = json.loads(text)
            ticket_number = data.get("number", "N/A")
            ticket_id = data.get("id")
        except:
            match_num = re.search(r'"number":\s*"(\d+)"', text)
            match_id = re.search(r'"id":\s*(\d+)', text)
            if match_num:
                ticket_number = match_num.group(1)
            if match_id:
                ticket_id = int(match_id.group(1))
    elif isinstance(result, str):
        match_num = re.search(r'"number":\s*"(\d+)"', result)
        match_id = re.search(r'"id":\s*(\d+)', result)
        if match_num:
            ticket_number = match_num.group(1)
        if match_id:
            ticket_id = int(match_id.group(1))
    elif isinstance(result, dict):
        ticket_number = result.get("number", "N/A")
        ticket_id = result.get("id")

    if ticket_id and servicio_value:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.put(
                    f"{os.environ['ZAMMAD_URL']}/api/v1/tickets/{ticket_id}",
                    json={"servicio": servicio_value},
                    headers={
                        "Authorization": f"Token token={os.environ['ZAMMAD_HTTP_TOKEN']}",
                        "Content-Type": "application/json",
                    },
                    timeout=30,
                )
                print(f"PUT status: {r.status_code}")
                print(f"PUT response: {r.text}")
                r.raise_for_status()
                print(f"Campo servicio actualizado via API {servicio_value} en ticket {ticket_id}")
        except Exception as e:
            print(f"Error actualizando campo servicio API: {e}")

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
        "diagnosis_step": None,
        "diagnosis_history": None,
        "severity": None,
        "servicio": None,
    }
    
def generate_ticket_summary(history, severity):
    history = history or []
    history_text = "\n".join(history)

    response = llm.invoke([
        SystemMessage(content=(
            "Eres un ingeniero de soporte experto. "
            "Responde SOLO con el formato indicado. "
            "Sin saludos, sin preguntas, sin texto adicional."
        )),
        HumanMessage(content=f"""
Convierte este diagnóstico en un ticket de soporte.

REGLAS:
- La descripción debe ser un párrafo continuo, natural, como un resumen técnico.
- Incluye TODOS los datos concretos que aparezcan en el historial: horas, fechas, errores exactos, sistemas afectados, pasos ya intentados, cuántos usuarios afectados.
- Si el usuario mencionó una hora o fecha, escríbela textualmente en la descripción.
- No uses listas ni bullets. Solo párrafo.
- La descripción debe tener mínimo 3 oraciones.

DIAGNOSTICO:
{history_text}

SEVERIDAD: {severity}

Devuelve EXACTAMENTE en este formato:

TITULO: [titulo corto máximo 10 palabras]
DESCRIPCION: [párrafo detallado con todos los datos del historial]
""")
    ])
   
    
    text = clean_html_entities(str(response.content or ""))
    title = "Incidente reportado por usuario"
    description = text

    if "TITULO:" in text and "DESCRIPCION:" in text:
        try:
            title = text.split("TITULO:")[1].split("DESCRIPCION:")[0].strip()
            description = text.split("DESCRIPCION:")[1].strip()
        except:
            pass

    return title, description


def should_use_tool(state):
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return "end"


async def check_ticket_status(state: State):

    messages_state = state.get("messages", [])
    last_message = str(messages_state[-1].content).strip()
    step = state.get("ticket_status_step")

    if step == "ask_id":
        cleaned = last_message.strip().lstrip('#').strip()
        match = re.fullmatch(r'\d+', cleaned)

        if not match:
            return {
                "ticket_status_step": "ask_id",
                "messages": [AIMessage(content="No veo el número del ticket claro, ¿me lo puedes repetir? Solo el número, sin letras ni símbolos.")]
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

async def router(state: State):
    print(f"=== ROUTER intent={state.get('intent')} support_option_step={state.get('support_option_step')} diagnosis_step={state.get('diagnosis_step')} email_request_step={state.get('email_request_step')} ===")

    thread_id = state.get("thread_id") or ""
    messages_list = state.get("messages", [])

    # 1. Si el agente humano ya tomó el caso
    if state.get("human_escalated"):
        last = str(messages_list[-1].content).lower() if messages_list else ""
        quiere_salir = detect_intent_simple(
            last,
            {
                "salir": "dice que ya se resolvio, quiere continuar solo, o quiere cancelar la espera",
                "espera": "sigue esperando al agente o cualquier otra cosa"
            }
        )
        if "salir" in quiere_salir:
            return {"intent": "chat_general", "human_escalated": False}
        return {"intent": "waiting_agent"}

    # 2. Flujo de solicitud de email pendiente
    if state.get("email_request_step") == "ask_email":
        last_email = str(messages_list[-1].content).strip() if messages_list else ""
        if re.match(r"^[^@]+@[^@]+\.[^@]+$", last_email):
            db2 = SessionLocal()
            try:
                db2.execute(
                    update(users)
                    .where(users.c.phone_number == thread_id)
                    .values(email=last_email)
                )
                db2.commit()
                await update_chatwoot_contact(phone_number=thread_id, email=last_email)
            finally:
                db2.close()

            pending = state.get("pending_intent", "diagnosis_flow")
            return {
                "email_request_step": None,
                "pending_intent": None,
                "support_option_step": None,
                "diagnosis_step": None,
                "diagnosis_history": state.get("diagnosis_history"),
                "severity": state.get("severity"),
                "intent": pending if pending in ("human", "crear_ticket") else "diagnosis_flow",
            }
        return {
            "email_request_step": "ask_email",
            "messages": [AIMessage(content="Ese correo no parece válido, ¿me lo confirmas?")]
        }

    
    _flujo_activo = (
        bool(state.get("password_step")) or
        (state.get("diagnosis_step") is not None) or
        (state.get("ticket_status_step") == "ask_id") or
        bool(state.get("support_option_step"))
    )

    if _flujo_activo:
        # Si estamos esperando respuesta de opciones, ir directo sin escape hatch
        if state.get("support_option_step"):
            return {"intent": "support_options"}

        _last = str(messages_list[-1].content).lower() if messages_list else ""
        _quiere_salir = detect_intent_simple(
            _last,
            {
                "estado_ticket": "quiere saber el estado de un ticket existente, menciona ticket y estado o seguimiento",
                "agente": "quiere hablar con un agente humano ahora",
                "password": "tiene problema con contraseña o acceso",
                "cancelar": "quiere cancelar, cambiar de tema, empezar de nuevo o salir del flujo actual",
                "continuar": "sigue en el tema, responde lo que se le pregunto, habla del problema actual"
            },
            context="El usuario estaba en medio de un flujo de soporte (diagnostico, opciones, o contraseña)."
        )

        if "estado_ticket" in _quiere_salir:
            return {
                "intent": "estado_ticket",
                "ticket_status_step": "ask_id",
                "diagnosis_step": None,
                "diagnosis_history": None,
                "support_option_step": None,
                "password_step": None,
            }

        if "agente" in _quiere_salir:
            email_state = get_or_request_email(state, "human")
            if email_state:
                return {**email_state, "diagnosis_step": None, "diagnosis_history": None, "support_option_step": None, "password_step": None}
            return {
                "intent": "human",
                "diagnosis_step": None,
                "diagnosis_history": None,
                "support_option_step": None,
                "password_step": None
            }

        if "cancelar" in _quiere_salir:
            return {
                "intent": "chat_general",
                "diagnosis_step": None,
                "diagnosis_history": None,
                "support_option_step": None,
                "password_step": None,
                "ticket_status_step": None,
            }

        if state.get("diagnosis_step") is not None:
            return {"intent": "diagnosis_flow"}

        if state.get("ticket_status_step") == "ask_id":
            return {"intent": "estado_ticket"}

        if state.get("password_step"):
            return {"intent": "chat_general"}

    # 4. Greeting activo (excepto waiting_problem)
    if state.get("greeting_step") and state.get("greeting_step") != "waiting_problem":
        return {"intent": "greeting_flow"}

    # 5. Diagnosis activo — ANTES del bloque waiting_problem
    if state.get("diagnosis_step") is not None:
        return {"intent": "diagnosis_flow"}

    # 6. Support options activo
    if state.get("support_option_step") == "waiting_choice":
        last_message = messages_list[-1] if messages_list else None
        user_text = str(last_message.content).lower() if last_message else ""

        detected = detect_intent_simple(
            user_text,
            {
                "ticket": "dice 1, o quiere crear un ticket",
                "agente": "dice 2, o quiere hablar con un agente humano",
                "otro": "otra cosa"
            },
            context="El bot pregunto: ¿Que prefieres: 1. Ticket o 2. Hablar con alguien?"
        )
        if "ticket" in detected:
            email_state = get_or_request_email(state, "crear_ticket")
            if email_state:
                return email_state
            return {
                "intent": "crear_ticket",
                "support_option_step": None,
                "diagnosis_step": None,
                "diagnosis_history": state.get("diagnosis_history"),
                "severity": state.get("severity"),
                "servicio": state.get("servicio")
            }
        if "agente" in detected:
            email_state = get_or_request_email(state, "human")
            if email_state:
                return email_state
            return {"intent": "human", "support_option_step": None, "diagnosis_step": None}

        return {"intent": "support_options"}

    if state.get("support_option_step"):
        return {"intent": "support_options"}

    # 7. Ticket step activo
    if state.get("ticket_step"):
        return {"intent": "crear_ticket"}

    # 8. Ticket status activo
    if state.get("ticket_status_step") == "ask_id":
        return {"intent": "estado_ticket"}

    # 9. greeting_step == waiting_problem → detectar intent del mensaje nuevo
    if state.get("greeting_step") == "waiting_problem":
        db = SessionLocal()
        try:
            stmt = select(users).where(users.c.phone_number == thread_id)
            user_row = db.execute(stmt).fetchone()
            if user_row:
                if not user_row.sede:
                    return {"intent": "greeting_flow", "greeting_step": "register_sede"}
                if not user_row.company or user_row.company == "pending":
                    return {"intent": "greeting_flow", "greeting_step": "register_company"}
                if not user_row.name:
                    return {"intent": "greeting_flow", "greeting_step": "register_name"}
        finally:
            db.close()

        last = str(messages_list[-1].content) if messages_list else ""

        detected_general = detect_intent_simple(
            last,
            {
                "estado_ticket": "quiere saber el estado de un ticket, menciona explicitamente la palabra ticket y estado o seguimiento",
                "password": "tiene problema con contraseña, clave o acceso al correo",
                "humano": "quiere hablar con un agente humano",
                "crear_ticket": "quiere crear un ticket o reportar un problema",
                "problema": "describe un problema tecnico activo: errores, caidas, fallas, equipos que no encienden, servidores, redes, migracion, configuracion",
                "resuelto": "dice que su problema ya se resolvio, que ya funciona, que ya esta bien",
                "charla": "saludo, pregunta personal, conversacion casual"
            }
        )

        if "estado_ticket" in detected_general:
            return {"intent": "estado_ticket", "ticket_status_step": "ask_id"}

        if "resuelto" in detected_general:
            return {"intent": "chat_general"}

        if "charla" in detected_general:
            return {"intent": "chat_general"}

        if "humano" in detected_general:
            email_state = get_or_request_email(state, "human")
            if email_state:
                return email_state
            return {"intent": "human"}

        # Para cualquier problema o solicitud de ticket, iniciar diagnosis con el mensaje ya incluido
        return {
            "intent": "diagnosis_flow",
            "diagnosis_step": 1,
            "diagnosis_history": [f"Usuario: {last}"],
        }

    # 10. Primer mensaje — usuario conocido o nuevo
    if not state.get("greeting_step") and len(messages_list) == 1:
        db = SessionLocal()
        try:
            stmt = select(users).where(users.c.phone_number == thread_id)
            user_row = db.execute(stmt).fetchone()

            if user_row and user_row.name and user_row.name != "pending" \
                    and user_row.company and user_row.company != "pending":

                saved_step = user_row.greeting_step or None

                if saved_step and saved_step not in ("waiting_problem", None):
                    return {"intent": "greeting_flow", "greeting_step": saved_step}

                if user_row.sede:
                    return {"intent": "greeting_flow", "greeting_step": "confirm_branch"}

                return {"intent": "greeting_flow", "greeting_step": "register_company_sede"}

            return {
                "intent": "greeting_flow",
                "greeting_step": user_row.greeting_step if user_row and user_row.greeting_step else "start"
            }

        finally:
            db.close()

    # 11. Fallback general
    last_message = messages_list[-1] if messages_list else None
    user_text = str(last_message.content).lower() if last_message else ""

    detected = detect_intent_simple(
        user_text,
        {
            "estado_ticket": "quiere saber el estado de un ticket, menciona explicitamente la palabra ticket y estado o seguimiento",
            "crear_ticket": "quiere crear un ticket o reportar un problema",
            "password": "tiene problema con contraseña, clave o acceso al correo",
            "humano": "quiere hablar con un agente humano",
            "otro": "cualquier otra cosa"
        }
    )

    if "estado_ticket" in detected:
        return {"intent": "estado_ticket", "ticket_status_step": "ask_id"}

    if "crear_ticket" in detected:
        return {
            "intent": "diagnosis_flow",
            "diagnosis_step": 0,
            "diagnosis_history": []
        }

    if "humano" in detected:
        return {"intent": "human"}

    return {"intent": "chat_general"}


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


def escalate_human(state: State):

    db = SessionLocal()
    try:
        thread_id = state.get("thread_id") or ""
        messages_state = state.get("messages", [])

        smtp_host = os.environ["SMTP_HOST"]
        smtp_port = int(os.environ["SMTP_PORT"])
        smtp_email = os.environ["SMTP_EMAIL"]
        smtp_password = os.environ["SMTP_PASSWORD"]
        destino = os.environ["DESTINO_SOPORTE"]

        stmt_user = select(users).where(users.c.phone_number == thread_id)
        user = db.execute(stmt_user).fetchone()

        if user:
            nombre = user.name
            empresa = user.company
            correo = user.email
        else:
            nombre = "No registrado"
            empresa = "No registrada"
            correo = "No disponible"

        historial = ""
        for msg in messages_state:
            role = "Usuario" if msg.type == "human" else "Bot"
            historial += f"{role}: {msg.content}\n"

        chatwoot_base_url = os.environ.get("CHATWOOT_URL")
        chatwoot_link = f"{chatwoot_base_url}/app/accounts/1/conversations/{thread_id}"

        cuerpo = f"""
ESCALACIÓN A SOPORTE HUMANO

INFORMACIÓN DEL CLIENTE
Nombre: {nombre}
Empresa: {empresa}
Correo: {correo}
Teléfono / ID Conversación: {thread_id}

CONTEXTO COMPLETO DE LA CONVERSACIÓN
{historial}

ACCESO DIRECTO A CHATWOOT
{chatwoot_link}

Este caso requiere intervención manual.
"""

        msg = MIMEText(cuerpo)
        msg["Subject"] = f"Escalación Soporte - {nombre} ({empresa})"
        msg["From"] = smtp_email
        msg["To"] = destino

        try:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(smtp_email, smtp_password)
                server.send_message(msg)
        except Exception as e:
            print("Error enviando correo:", e)

        diagnosis_history = state.get("diagnosis_history") or []
        if thread_id:
            save_conversation_memory(thread_id, diagnosis_history)

        db.execute(
            update(users)
            .where(users.c.phone_number == thread_id)
            .values(bot_active=False)
        )
        db.commit()

        return {
            "messages": [
                AIMessage(content="Listo, ya avisé al equipo. En breve te escribe un técnico.")
            ]
        }

    finally:
        db.close()


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
        return {
            "messages": [AIMessage(content="Con gusto, ya están al tanto.")]
        }

    return {
        "messages": [AIMessage(content="Ya avisé, en breve te contactan. Si necesitas cancelar la espera dime.")]
    }


def normalize(text: str):
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9áéíóúñ]', '', text)
    return text


def reset_password_flow():
    return {
        "description": None,
        "ticket_step": None,
        "password_step": None,
        "security_questions": None,
        "user_answers": None,
        "current_question_index": None,
        "security_risk": None,
        "real_data": None
    }
    

async def load_servicio_values():
    global SERVICIO_VALUES
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{os.environ['ZAMMAD_URL']}/api/v1/object_manager_attributes",
                headers={
                    "Authorization": f"Token token={os.environ['ZAMMAD_HTTP_TOKEN']}",
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
                    # print(f"Servicios cargados desde Zammad: {SERVICIO_VALUES}")
                    return
        print("Campo 'servicio' no encontrado en Zammad, usando dict vacio")
    except Exception as e:
        print(f"Error cargando servicios desde Zammad: {e}")
        
def clean_html_entities(text: str) -> str:
    return (text
        .replace("&quot;", '"')
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
    )
    
    
def get_servicio_value(nombre: str) -> str:
    
    if nombre in SERVICIO_VALUES:
        return SERVICIO_VALUES[nombre]
    
    
    nombre_lower = nombre.lower().strip()
    for key, value in SERVICIO_VALUES.items():
        if key.lower().strip() == nombre_lower:
            return value
    
    return "otras_aplicaciones"


