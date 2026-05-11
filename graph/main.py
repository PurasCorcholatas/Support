
from .llms import llm, llm_diagnosis
from .nodes.greeting import greeting_flow
from .nodes.diagnosis import diagnosis_flow
from .nodes.quick_fix import quick_fix_flow
from .nodes.support import offer_support_options, support_agent, check_ticket_status, escalate_human, waiting_agent
from .nodes.chat import chat_general
from .nodes.router import router
from .helpers import _normalize_images, guided_response, detect_intent_simple, _hacer_pregunta_tecnica, _parse_ticket_result, generate_ticket_summary, generate_technical_details, clean_html_entities
from .integrations import (
    save_conversation_memory, user_has_email, get_user_email,
    update_chatwoot_contact, detected_incident_severity,
    attach_images_to_zammad, load_servicio_values, get_servicio_value
)

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import ToolNode
from langchain_anthropic import ChatAnthropic
from psycopg_pool import AsyncConnectionPool
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
import time
from config.db import SessionLocal
from models.users import users
from models.conversations import conversation
from models.messages import messages
from models.user_memory import user_memory
from models.tickets import tickets

from services.mcp_client import get_mcp_tools

import aiosmtplib
from email.mime.text import MIMEText
import os

load_dotenv()

os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY", "")


# LLMs



llm_with_tools = None
tool_node = None
graph = None

checkpointer = None
_db_connection = None
_initialized = False

SERVICIO_VALUES: dict = {}
_servicio_values_lock = asyncio.Lock()

PRIORIDAD_MAP = {
    "baja":     "1 baja",
    "moderada": "2 moderada",
    "alta":     "3 alta",
    "critica":  "4 critica",
}



# State

from .state import State



_locks: dict[str, asyncio.Lock] = {}
_pending: dict[str, list[str]] = {}
_pending_lock = asyncio.Lock()
_locks_meta = asyncio.Lock()


async def get_lock(thread_id: str) -> asyncio.Lock:
    async with _locks_meta:
        if thread_id not in _locks:
            _locks[thread_id] = asyncio.Lock()
        return _locks[thread_id]



async def init_llm_with_tools():
    global llm_with_tools, tool_node, graph, checkpointer, _db_connection, _initialized

    from .llms import tools
    tools.clear()
    tools.extend(await get_mcp_tools())

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
    builder.add_node("quick_fix", quick_fix_flow)
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
            "quick_fix": "quick_fix",
            "end": END
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

    builder.add_conditional_edges(
        "diagnosis_flow",
        lambda state: (
            "support_options"
            if state.get("intent") == "support_options"
            else "quick_fix"
            if state.get("intent") == "quick_fix"
            else END
        ),
        {
            "support_options": "support_options",
            "quick_fix": "quick_fix",
            END: END
        }
    )

    builder.add_conditional_edges(
        "quick_fix",
        lambda state: state.get("intent", END),
        {
            "support_options": END,
            "chat_general": END,
            "quick_fix": END,
            END: END
        }
    )

    builder.add_edge("tools", END)
    builder.add_edge("greeting_flow", END)
    builder.add_edge("waiting_agent", END)
    builder.add_edge("check_ticket_status", END)
    builder.add_edge("escalate_human", END)
    builder.add_edge("chat_general", END)

    

    async with AsyncPostgresSaver.from_conn_string(LANGGRAPH_DB_URL) as tmp_checkpointer:
        await tmp_checkpointer.setup()

    _db_connection = AsyncConnectionPool(
        conninfo=LANGGRAPH_DB_URL,
        max_size=10,
        open=False
    )
    await _db_connection.open()
    checkpointer = AsyncPostgresSaver(_db_connection)  # type: ignore
    graph = builder.compile(checkpointer=checkpointer)
    _initialized = True



async def langgraph(mensaje: str, thread_id: str, image_b64_list: list = None, chatwoot_conversation_id: int = None):
    if image_b64_list is None:
        image_b64_list = []
    if not _initialized or graph is None:
        raise Exception("Graph no inicializado. Llama a init_llm_with_tools() primero.")

    async with _pending_lock:
        _pending.setdefault(thread_id, []).append(mensaje)

    await asyncio.sleep(5)

    async with _pending_lock:
        current_batch = _pending.get(thread_id, [])
        if not current_batch or current_batch[-1] != mensaje:
            return None
        mensajes_batch = _pending.pop(thread_id, [mensaje])

    mensaje_combinado = " | ".join(mensajes_batch) if len(mensajes_batch) > 1 else mensajes_batch[0]

    lock = await get_lock(thread_id)
    result_to_return = None

    async with lock:
        db = SessionLocal()
        try:
            stmt_check = select(users).where(users.c.phone_number == thread_id)
            user_check = db.execute(stmt_check).fetchone()

            normalized_images = _normalize_images(image_b64_list)

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
                    "chatwoot_conversation_id": chatwoot_conversation_id,
                }
            else:
                state: State = {
                    "messages": [HumanMessage(content=mensaje_combinado)],
                    "thread_id": thread_id,
                    "chatwoot_conversation_id": chatwoot_conversation_id,
                }

            if normalized_images:
                state["diagnosis_images"] = normalized_images

            try:
                from langgraph.errors import GraphRecursionError
                result = await graph.ainvoke(
                    state,
                    config={
                        "configurable": {"thread_id": thread_id},
                        "recursion_limit": 50
                    }
                )
            except GraphRecursionError:
                print(f"[WARN] GraphRecursionError para thread_id={thread_id}")
                result_to_return = ["Tuve un problema procesando tu mensaje. ¿Puedes intentarlo de nuevo o describir el problema de otra forma?"]
            except Exception as graph_err:
                print(f"[ERROR] graph.ainvoke falló para {thread_id}: {graph_err}")
                result_to_return = ["Ocurrió un error interno. Por favor intenta de nuevo en un momento."]

            if result_to_return is None:
                all_ai = [m for m in result["messages"] if isinstance(m, AIMessage)]

                if not all_ai:
                    result_to_return = None
                else:
                    last_ai = all_ai[-1]
                    if isinstance(last_ai.content, list):
                        final_text = " ".join(
                            block.get("text", "") for block in last_ai.content if isinstance(block, dict)
                        ).strip()
                    else:
                        final_text = str(last_ai.content).strip()

                    if not final_text:
                        result_to_return = None
                    else:
                        final_messages = [final_text]

                        stmt_user = select(users).where(users.c.phone_number == thread_id)
                        user = db.execute(stmt_user).fetchone()

                        if user:
                            stmt_conv = select(conversation).where(
                                conversation.c.users == user.id,
                                conversation.c.status == "open"
                            )
                            conv = db.execute(stmt_conv).fetchone()

                            if not conv:
                                result_conv = db.execute(
                                    insert(conversation).values(
                                        users=user.id,
                                        status="open"
                                    ).returning(conversation.c.id)
                                )
                                conversation_id = result_conv.scalar()
                                db.commit()
                            else:
                                conversation_id = conv.id

                            db.execute(
                                insert(messages).values(
                                    conversation_id=conversation_id,
                                    sender="user",
                                    message_text=mensaje_combinado,
                                    company=user.company or "Sin empresa"
                                )
                            )
                            db.commit()

                            db.execute(
                                insert(messages).values(
                                    conversation_id=conversation_id,
                                    sender="bot",
                                    message_text=final_text,
                                    company=user.company or "Sin empresa"
                                )
                            )
                            db.commit()

                        result_to_return = final_messages

        finally:
            db.close()

    async with _locks_meta:
        _locks.pop(thread_id, None)

    return result_to_return

