from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI

from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage, AIMessage
from langgraph.checkpoint.memory import MemorySaver
from typing import List, Literal, Annotated, cast, Optional
from typing_extensions import TypedDict
from dotenv import load_dotenv
from tools.registry import tools
from tools.functions.generator_password import generator_pw
from sqlalchemy import select, insert, update
import re

from config.db import SessionLocal
from models.users import users
from models.conversations import conversation
from models.messages import messages
from models.tickets import tickets



from services.zammad_services import ZammadService

import smtplib
from email.mime.text import MIMEText
import os

load_dotenv()

memory_saver = MemorySaver()

llm = ChatOpenAI(
    model="gpt-4.1",
    temperature=0,
)





class State(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], add_messages]
    intent: Literal[
        "chat_general",
        "crear_ticket",
        "human",
        "silence",
        "estado_ticket",
        "password_flow"]
    human_escalated: bool
    ticket_step: Literal[
        "ask_user_info",
        "ask_title", 
        "ask_description",
        "ask_email",
        "done"]

    password_step:Literal[
        "ask_status",
        "guide_change",
        "confirm_result",
        "ask_temps",
        "done",
    ]

    ticket_status_step: Optional[Literal["ask_id"]]
    account_status: Optional[str]
    attempts: Optional[int]
    security_risk: Optional[int]
    priority: Optional[int]

    title: Optional[str]
    description: Optional[str]
    email: Optional[str]
    zammad_ticket_id: int
    thread_id: Optional[str]
    flow: Optional[str]
    awaiting_confirmation: bool 
   
def langgraph(mensaje: str, thread_id: str):

    state: State = {
        "messages": [HumanMessage(content=mensaje)],
        "intent": "chat_general",
        "thread_id": thread_id
    }

    result = graph.invoke(
        state,
        config={"configurable": {"thread_id": thread_id}}
    )

    return result["messages"][-1].content



def router(state: State):

    if state.get("human_escalated"):
        return {"intent": "silence"}

    if state.get("password_step") and state.get("password_step") != "done":
        return {"intent": "password_flow"}

    if state.get("ticket_step") and state.get("ticket_step") != "done":
        return {"intent": "crear_ticket"}

    
    if state.get("ticket_status_step") == "ask_id":
        return {"intent": "estado_ticket"}

    last_message = state.get("messages", [])[-1]
    user_text = str(last_message.content).lower() if last_message else ""

    
    if "estado" in user_text and "ticket" in user_text:
        return {
            "intent": "estado_ticket",
            "ticket_status_step": "ask_id"
        }

    prompt = f"""
Clasifica el mensaje:

"{user_text}"

Opciones:
- password_flow
- crear_ticket
- estado_ticket
- human
- chat_general

Responde solo una palabra.
"""

    response = llm.invoke([HumanMessage(content=prompt)])
    intent = str(response.content).strip().lower().split()[0]

    return {"intent": intent}

def chat_general(state: State, config):

    system_prompt = """
Eres un asistente de soporte técnico empresarial.
Responde de forma clara, profesional y directa.
"""

    messages_state = state.get("messages", [])

    if not any(isinstance(m, SystemMessage) for m in messages_state):
        messages_state = [SystemMessage(content=system_prompt)] + messages_state

    response = llm.invoke(messages_state)

    return {"messages": [response]}


def create_ticket(state: State):

    db = SessionLocal()

    thread_id = state.get("thread_id")
    phone_number = thread_id
    step = state.get("ticket_step")
    messages_state = state.get("messages", [])
    last_user_message = str(messages_state[-1].content).strip() if messages_state else ""

    description = state.get("description")

    

    if not description:

        if step != "ask_description":
            return {
                "ticket_step": "ask_description",
                "messages": [
                    AIMessage(
                        content="Por favor descríbeme el problema que estás presentando."
                    )
                ]
            }

        
        description = last_user_message.strip()

        if not description:
            return {
                "ticket_step": "ask_description",
                "messages": [
                    AIMessage(content="La descripción no puede estar vacía.")
                ]
            }

    

    result = db.execute(
        select(users).where(users.c.phone_number == phone_number)
    ).fetchone()

    usuario_incompleto = (
        result is None or
        not result.name or
        not result.company or
        not result.email
    )

    if usuario_incompleto:

        if step != "ask_user_info":
            return {
                "ticket_step": "ask_user_info",
                "description": description,
                "messages": [
                    AIMessage(
                        content="Antes de continuar, indícame tu nombre completo, empresa y correo corporativo.\nFormato: Nombre - Empresa - correo@empresa.com"
                    )
                ]
            }

       
        parts = last_user_message.split("-")

        if len(parts) < 3:
            return {
                "ticket_step": "ask_user_info",
                "description": description,
                "messages": [
                    AIMessage(
                        content="Formato incorrecto. Usa: Nombre - Empresa - correo@empresa.com"
                    )
                ]
            }

        name = parts[0].strip()
        company = parts[1].strip()
        email = parts[2].strip()

        if not name or not company or not email:
            return {
                "ticket_step": "ask_user_info",
                "description": description,
                "messages": [
                    AIMessage(
                        content="Todos los campos son obligatorios. Usa: Nombre - Empresa - correo@empresa.com"
                    )
                ]
            }

        if result:
            db.execute(
                update(users)
                .where(users.c.phone_number == phone_number)
                .values(name=name, company=company, email=email)
            )
            user_id = result.id
        else:
            stmt_insert = (
                insert(users)
                .values(
                    phone_number=phone_number,
                    name=name,
                    company=company,
                    email=email
                )
                .returning(users.c.id)
            )
            user_id = db.execute(stmt_insert).scalar_one()

        db.commit()

        
        result = db.execute(
            select(users).where(users.c.phone_number == phone_number)
        ).fetchone()

        if not result:
            return {
                "ticket_step": "ask_user_info",
                "description": description,
                "messages": [
                    AIMessage(content="Error guardando datos. Intenta nuevamente.")
                ]
            }

    

    if not description:
        return {
            "ticket_step": "ask_description",
            "messages": [
                AIMessage(content="Necesito la descripción del problema para continuar.")
            ]
        }

    if not result:
        return {
            "ticket_step": "ask_user_info",
            "description": description,
            "messages": [
                AIMessage(content="Necesito tus datos antes de crear el ticket.")
            ]
        }

    user_id = result.id
    name = result.name
    company = result.company
    customer_email = result.email

   

    stmt_conv = select(conversation).where(
        conversation.c.users == user_id,
        conversation.c.status == "open"
    )

    conv_result = db.execute(stmt_conv).fetchone()

    if conv_result:
        conversation_id = conv_result.id
    else:
        stmt_new_conv = (
            insert(conversation)
            .values(users=user_id, status="open")
            .returning(conversation.c.id)
        )
        conversation_id = db.execute(stmt_new_conv).scalar_one()
        db.commit()

    

    subject = description[:60] if description else "Nuevo Ticket"

    body_final = f"""
{description}

-------------------------
Nombre: {name}
Empresa: {company}
Correo: {customer_email}
Teléfono: {phone_number}
"""

    ticket = ZammadService.create_ticket(
        title=subject,
        body=body_final,
        customer_email=customer_email,
        priority_id=2
    )

    zammad_id = ticket["id"]

    db.execute(
        insert(tickets).values(
            conversation_id=conversation_id,
            zammad_ticket_id=zammad_id,
            subject=subject,
            status="new"
        )
    )

    db.commit()

   

    return {
        "ticket_step": None,
        "description": None,
        "messages": [
            AIMessage(
                content=f"Te he creado un ticket con el ID {zammad_id}, a la mayor brevedad se solucionara tu problema. ¿Necesitas algo más?"
            )
        ]
    }
    

def check_status_ticket(state: State):

    db = SessionLocal()
    thread_id = state.get("thread_id")
    step = state.get("ticket_status_step")

    messages_state = state.get("messages", [])
    last_message = str(messages_state[-1].content).strip()

    
    if step == "ask_id":

        if not re.fullmatch(r"\d+", last_message):
            return {
                "ticket_status_step": "ask_id",
                "messages": [
                    AIMessage(content="Por favor indícame únicamente el número del ticket.")
                ]
            }

        ticket_id = int(last_message)

        
        state["ticket_status_step"] = None

    else:
        
        match = re.search(r"\d+", last_message)
        if not match:
            return {
                "ticket_status_step": "ask_id",
                "messages": [
                    AIMessage(content="Indícame el ID del ticket que deseas consultar.")
                ]
            }
        ticket_id = int(match.group())

    try:
        zammad_ticket = ZammadService.get_ticket(ticket_id)

        ESTADOS = {
            1: "Nuevo",
            2: "Abierto",
            3: "Pendiente",
            4: "Cerrado"
        }

        status = ESTADOS.get(zammad_ticket.get("state_id"))

        return {
            "ticket_status_step": None,
            "messages": [
                AIMessage(
                    content=f"El ticket #{ticket_id} se encuentra en estado: {status}"
                )
            ]
        }

    except Exception:
        return {
            "messages": [
                AIMessage(content="No encontré un ticket con ese ID.")
            ]
        }

def handle_password_issue(state: State):

    messages_state = state.get("messages", [])
    last_message = str(messages_state[-1].content).strip().lower()
    step = state.get("password_step")

    
    if re.search(r"\b(mi\s)?(clave|contraseña|password)\s+es\b", last_message):
        return {
            "messages": [
                AIMessage(content="Por seguridad, nunca compartas tu contraseña. El equipo de soporte jamás te la pedirá.")
            ]
        }

    

    if not step:

        if "no puedo acceder" in last_message or "no puedo entrar" in last_message:
            return {
                "flow": "access_issue",
                "password_step": "ask_recent_change",
                "messages": [
                    AIMessage(content="¿Has cambiado la contraseña en los últimos 3 meses? (si/no)")
                ]
            }

        if "correo" in last_message and "bloque" in last_message:
            return {
                "flow": "correo_bloqueado",
                "password_step": "ask_attempts",
                "messages": [
                    AIMessage(content="¿Cuántos intentos fallidos realizaste antes de que se bloqueara?")
                ]
            }

        
        return {
            "password_step": "ask_recent_change",
            "flow": "access_issue",
            "messages": [
                AIMessage(content="¿Has cambiado la contraseña en los últimos 3 meses? (si/no)")
            ]
        }

   

    if step == "ask_recent_change":

        if last_message in ["si", "sí", "ok", "yes"]:

            
            return {
                "password_step": "done",
                "intent": "crear_ticket",
                "description": "Usuario no puede acceder a su cuenta después de cambio reciente de contraseña. Posible bloqueo.",
                "priority": 3
            }

        else:

            
            return {
                "password_step": "guide_change",
                "messages": [
                    AIMessage(content="""
Te guiaré para cambiar tu contraseña correctamente:

1. Mínimo 12 caracteres
2. Al menos 1 mayúscula
3. Incluir números
4. Incluir caracteres especiales
5. No usar tu nombre ni empresa

Avísame cuando la hayas cambiado.
""")
                ]
            }

    if step == "guide_change":
        return {
            "password_step": "confirm_result",
            "messages": [
                AIMessage(content="¿Ahora puedes acceder correctamente? (si/no)")
            ]
        }

    if step == "confirm_result":

        if last_message in ["si", "sí", "ok", "listo", "ya"]:
            return {
                "password_step": None,
                "messages": [
                    AIMessage(content="Perfecto, me alegra que se haya solucionado. ¿Necesitas algo más?")
                ]
            }

        else:
            return {
                "password_step": "done",
                "intent": "crear_ticket",
                "description": "Usuario no puede acceder a su cuenta después de intentar cambio de contraseña.",
                "priority": 2
            }

    

    if step == "ask_attempts":

        match = re.search(r"\d+", last_message)

        if match:
            attempts = int(match.group())
        else:
            attempts = 1

        if attempts > 3:
            
            return {
                "attempts": attempts,
                "security_risk": 2,
                "priority": 3,
                "password_step": "done",
                "intent": "crear_ticket",
                "description": f"Cuenta de correo bloqueada después de {attempts} intentos fallidos."
            }
        else:
            
            return {
                "password_step": "guide_change",
                "messages": [
                    AIMessage(content="Parece un bloqueo temporal. Intentemos cambiar la contraseña primero.")
                ]
            }

    return {"password_step": None}

def escalate_human(state: State):

    db = SessionLocal()

    thread_id = state.get("thread_id")
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

    
    chatwoot_base_url = os.environ.get("https://app.chatwoot.com")
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
    msg["Subject"] = f" Escalación Soporte - {nombre} ({empresa})"
    msg["From"] = smtp_email
    msg["To"] = destino

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_email, smtp_password)
            server.send_message(msg)
    except Exception as e:
        print("Error enviando correo:", e)

    return {
        "messages": [{
            "role": "assistant",
            "content": "He notificado a un asesor humano. En breve continuará contigo."
        }],
        "human_escalated": True
    }


def silence(state: State):
    return {"messages": []}




builder = StateGraph(State)

builder.add_node("router", router)
builder.add_node("chat_general", chat_general)
builder.add_node("create_ticket", create_ticket)
builder.add_node("check_status_ticket", check_status_ticket)
builder.add_node("escalate_human", escalate_human)
builder.add_node("handle_password_issue", handle_password_issue)


builder.add_node("silence", silence)

builder.add_edge(START, "router")

builder.add_conditional_edges(
    "router",
    lambda state: state["intent"],
    {
        "chat_general": "chat_general",
        "crear_ticket": "create_ticket",
        "estado_ticket": "check_status_ticket",
        "human": "escalate_human",
        "password_flow": "handle_password_issue",
        "silence": "silence",
    }
)

builder.add_conditional_edges(
    "handle_password_issue",
    lambda state: state.get("intent", "chat_general"),
    {
        "crear_ticket": "create_ticket",
        "chat_general": END,
        "password_flow": END,
    }
)
builder.add_edge("create_ticket", END)
builder.add_edge("check_status_ticket", END)





builder.add_edge("escalate_human", END)
builder.add_edge("silence", END)

graph = builder.compile(checkpointer=memory_saver)