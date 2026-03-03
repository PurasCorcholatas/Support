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
    
    
    conversation_status: Optional[Literal[
        "bot_active",
        "human_active",
        "closed"
    ]]
    
    password_step:Literal[
        "confirm_owner",
        "ask_email",
        "ask_device",
        "ask_location",
        "ask_last_access",
        "evaluate",
        "done"
    ]
    
    security_risk: Optional[int]
    
    description: Optional[str]
    thread_id: Optional[str]
    
    ticket_step: Literal[
        "ask_user_info",
        "ask_title", 
        "ask_description",
        "ask_email",
        "done"]

    

    ticket_status_step: Optional[Literal["ask_id"]]
    
    description: Optional[str]
    thread_id: Optional[str]
    device: Optional[str]
    location: Optional[str]
    validation_summary: Optional[dict]

   
def langgraph(mensaje: str, thread_id: str):

    db = SessionLocal()

    state: State = {
        "messages": [HumanMessage(content=mensaje)],
        "intent": "chat_general",
        "thread_id": thread_id,
        "conversation_status": "bot_active"
    }

    result = graph.invoke(
        state,
        config={"configurable": {"thread_id": thread_id}}
    )

    final_message = result["messages"][-1].content

    
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
                    message_text=mensaje,
                    company=user.company
                )
            )

            db.execute(
                insert(messages).values(
                    conversation_id=conversation_id,
                    sender="bot",
                    message_text=final_message,
                    company=user.company
                )
            )

        db.commit()
        
    return final_message


def router(state: State):

    if state.get("conversation_status") == "human_active":
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

    return {"intent": intent,
            "conversation_status": "bot_active"
    }

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

    

    validation = state.get("validation_summary")

    if validation:

        resumen = f"""
VALIDACIÓN REALIZADA POR BOT

Tipo de solicitud: {validation.get("tipo")}
Dispositivo habitual: {validation.get("dispositivo")}
Ubicación habitual: {validation.get("ubicacion")}
Último acceso: {validation.get("ultimo_acceso")}
Nivel de riesgo calculado: {validation.get("riesgo")}
"""

        ZammadService.add_note(
            ticket_id=zammad_id,
            body=resumen,
            internal=True
        )

    

    return {
        "ticket_step": None,
        "description": None,
        "messages": [
            AIMessage(
                content=f"Te he creado un ticket con el ID {zammad_id}. A la mayor brevedad se solucionará tu problema. ¿Necesitas algo más?"
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
    risk = state.get("security_risk", 0)
    
       
    if re.search(r"\b(mi\s)?(clave|contraseña|password)\s+es\b", last_message):
        return {
            "messages": [
                AIMessage(content="Por seguridad, nunca compartas tu contraseña. El equipo de soporte jamás te la pedirá.")
            ]
        }

    

    if not step:
        return {
            "password_step": "confirm_owner",
            "security_risk": 0,
            "messages": [
                AIMessage(content="¿Esta solicitud corresponde unicamente a tu cuenta corporativa? (si/no) ")
                
            ]
        }

    if step == "confirm_owner":
        if last_message not in ["si", "si", "yes"]:
            return{
                "password_step": "done",
                "messages": [
                    AIMessage(content="Solo puedo ayudarte con solicitudes de tu propia cuenta")
                ]
            }

        return {
            "password_step": "ask_email",
            "messages":[
                AIMessage(content="Indicame tu coreo corporativo")
            ]
        }


    if step == "ask_email":
        
        risk = int(state.get("security_risk") or 0)
        
        if not re.match(r"^[^@]+@[^@]+\.[^@]+$", last_message):
            return {
                "password_step": "ask_email",
                "security_risk": risk + 1,
                "messages": [
                    AIMessage("Formato de correo invalido")
                ]
            }
            

        return {
            "password_step": "ask_device",
            "messages":[
                AIMessage(content="¿Desde que dispositivo de accesdes normalmente) (navegador,movil) ")
            ]
        }

    risk = int(state.get("security_risk") or 0)

    if step == "ask_device":
        if len(last_message) < 3:
            risk += 1
        
        return {
            "password_step": "ask_location",
            "security_risk": risk,
            "device": last_message,
            "messages": [
                AIMessage(content = "¿Sueles ingresar desde oficina o remoto?")
            ]
        }

    if step == "ask_location":
        if last_message not in ["oficina", "remoto"]:
            risk +=1
        return{
            "password_step": "ask_last_access",
            "security_risk": risk,
            "device": state.get("device"),
            "location": last_message,
            "messages": [
                AIMessage(content=("¿Recuerdas cuando fue tu ultimo acceso?"))
            ]
        }


    if step == "ask_last_access":
        
        if len(last_message) < 3:
            risk += 1
            
        if risk >= 2:
            return{
                "password_step": "done",
                "security_risk": risk,
                "intent": "human"
            }
        
        return {
            "password_step": "done",
            "security_risk": risk,
            "intent": "crear_ticket",
            "description": "Solicitud de cambio de contraseña validada por bot",
            "device": state.get("device"),
            "location": state.get("location"),
            "validation_summary": {
                "tipo": "Cambio de contraseña",
                "dispositivo": state.get("device"),
                "ubicacion": state.get("location"),
                "ultimo_acceso": last_message,
                "riesgo": risk
            }
        }



    

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
    "messages": [
        AIMessage(content="Te estoy conectando con un técnico. En breve continuará contigo.")
    ],
    "conversation_status": "human_active"
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
        "human": "escalate_human",
        "chat_general": END,
        "password_flow": END,
    }
)

builder.add_edge("create_ticket", END)
builder.add_edge("check_status_ticket", END)





builder.add_edge("escalate_human", END)
builder.add_edge("silence", END)

graph = builder.compile(checkpointer=memory_saver)