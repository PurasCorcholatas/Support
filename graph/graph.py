from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage, AIMessage
from langgraph.checkpoint.memory import MemorySaver
from typing import List, Literal, Annotated, cast, Optional
from typing_extensions import TypedDict
from dotenv import load_dotenv

from sqlalchemy import select, insert
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
    intent: Literal["general", "crear_ticket", "human", "silence", "estado_ticket"]
    human_escalated: bool
    ticket_step: Literal["ask_title", "ask_description", "ask_email", "done"]

    title: str
    description: str
    email: str
    zammad_ticket_id: int
    thread_id: Optional[str]



def langgraph(mensaje: str, thread_id: str):

    state: State = {
        "messages": [HumanMessage(content=mensaje)],
        "intent": "general",
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

    if state.get("ticket_step") and state.get("ticket_step") != "done":
        return {"intent": "crear_ticket"}

    history = state.get("messages", [])[-3:]

    conversation_text = "\n".join(
        str(m.content) for m in history
    )

    prompt = f"""
    Eres un clasificador determinista.
    Clasifica la intención del ÚLTIMO mensaje del usuario.

    Contexto:
    {conversation_text}

    Responde solo con una palabra:
    - general
    - human
    - crear_ticket
    -estado_ticket
    """

    response = llm.invoke([HumanMessage(content=prompt)])

    content = cast(str, response.content)
    intent = content.strip().lower().split()[0].replace(".", "")

    print("Intent detectado:", intent)

    return {"intent": intent}




def chat_general(state: State):

    system_prompt = "Eres un asistente útil."

    messages_state = state.get("messages", [])

    if not any(isinstance(m, SystemMessage) for m in messages_state):
        messages_state = [SystemMessage(content=system_prompt)] + messages_state

    response = llm.invoke(messages_state)

    return {"messages": [response]}




def create_ticket(state: State):

    thread_id =state.get("thread_id")

    db = SessionLocal()

    messages_state = state.get("messages", [])
    last_user_message = str(messages_state[-1].content).strip()
    step = state.get("ticket_step")

    phone_number = thread_id

    
    stmt = select(users).where(users.c.phone_number == phone_number)
    result = db.execute(stmt).fetchone()

    if result:
        user_id = result.id
        company = result.company
    else:
            stmt = (
        insert(users)
        .values(
            phone_number=phone_number,
            name="Sin nombre",
            company="Sin empresa"
        )
        .returning(users.c.id)
    )

    user_id = db.execute(stmt).scalar_one()
    db.commit()

    
    stmt = select(conversation).where(
        conversation.c.users == user_id,
        conversation.c.status == "open"
    )

    result = db.execute(stmt).fetchone()

    if result:
        conversation_id = result.id
    else:
            stmt = (
        insert(conversation)
        .values(
            users=user_id,
            status="open"
        )
        .returning(conversation.c.id)
    )

    conversation_id = db.execute(stmt).scalar_one()
    db.commit()

    
    db.execute(
        insert(messages).values(
            conversation_id=conversation_id,
            sender="user",
            company="Mi empresa",
            message_text=last_user_message
        )
    )
    db.commit()

    
    if not step:
        return {
            "ticket_step": "ask_title",
            "messages": [{
                "role": "assistant",
                "content": "Indícame el título del problema."
            }]
        }

    if step == "ask_title":
        return {
            "title": last_user_message,
            "ticket_step": "ask_description",
            "messages": [{
                "role": "assistant",
                "content": "Ahora descríbeme el problema."
            }]
        }

    if step == "ask_description":
        return {
            "description": last_user_message,
            "ticket_step": "ask_email",
            "messages": [{
                "role": "assistant",
                "content": "Indícame tu correo electrónico."
            }]
        }

    if step == "ask_email":

        title = state.get("title")
        description = state.get("description")

        assert title is not None
        assert description is not None

        ticket = ZammadService.create_ticket(
            title=title,
            body=description,
            customer_email=last_user_message
        )

        zammad_id = ticket["id"]

       
        db.execute(
            insert(tickets).values(
                conversation_id=conversation_id,
                zammad_ticket_id=zammad_id,
                subject=title,
                status="new"
            )
        )

        
        db.execute(
            insert(messages).values(
                conversation_id=conversation_id,
                sender="bot",
                company=company,
                message_text=f"Ticket creado correctamente con ID {zammad_id}"
            )
        )

        db.commit()

        return {
            "email": last_user_message,
            "ticket_step": "done",
            "intent": "general",
            "zammad_ticket_id": zammad_id,
            "messages": [{
                "role": "assistant",
                "content": f"Ticket creado correctamente con ID {zammad_id}"
            }]
        }


def check_status_ticket(state:State):

    thread_id = state.get("thread_id")
    db = SessionLocal()

    messages_state = state.get("messages", [])
    
    if not messages_state:
        return{
            "messages": [AIMessage(
                content= "No hay mensajes para procesar"
            )]
        }
    
    
    last_message: str = str (messages_state[-1].content)
    
    stmt_user = select(users).where(users.c.phone_number == thread_id)
    user = db.execute(stmt_user).fetchone()
    
    
    if not user:
        return {
            "messages": [AIMessage(
                content="No encontré usuario asociado a esta conversación."
            )]
        }
    
    match = re.search(r"\d+", last_message)
    
    if match:
        ticket_id = int(match.group())
    
    else:
        stmt_last_ticket = (
            select(tickets)
            .join(conversation, tickets.c.conversation_id == conversation.c.id)
            .where(conversation.c.users == user.id)
            .order_by(tickets.c.id.desc())
        )
        
        last_ticket = db.execute(stmt_last_ticket).fetchone()
        
        
    if not last_ticket:
        return {
            "messages":[AIMessage(
                content= "No encontre tickets asosciados a tu cuenta"
            )]
        }
        
    ticket_id = last_ticket.zammad_ticket_id
    
    

    try:
        zammad_ticket = ZammadService.get_ticket(ticket_id)

        state_id = zammad_ticket.get("state_id")

        ESTADOS = {
            1: "Nuevo",
            2: "Abierto",
            3: "Pendiente",
            4: "Cerrado"
        }

        status = ESTADOS.get(state_id, f"Desconocido ({state_id})")

        return {
            "messages": [AIMessage(
                content=f"El ticket #{ticket_id} actualmente se encuentra en estado: {status}"
            )]
        }

    except Exception as e:
        print("Error consultando Zammad:", e)

        return {
            "messages": [AIMessage(
                content="Hubo un error consultando el estado"
            )]
        }


def escalate_human(state: State):

    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ["SMTP_PORT"])
    smtp_email = os.environ["SMTP_EMAIL"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    destino = os.environ["DESTINO_SOPORTE"]

    msg = MIMEText("Se solicitó atención humana.")
    msg["Subject"] = "Escalación a humano"
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
builder.add_node("silence", silence)

builder.add_edge(START, "router")

builder.add_conditional_edges(
    "router",
    lambda state: state["intent"],
    {
        "general": "chat_general",
        "crear_ticket": "create_ticket",
        "estado_ticket": "check_status_ticket",
        "human": "escalate_human",
        "silence": "silence",
    }
)

builder.add_edge("chat_general", END)
builder.add_edge("create_ticket", END)
builder.add_edge("check_status_ticket", END)
builder.add_edge("escalate_human", END)
builder.add_edge("silence", END)

graph = builder.compile(checkpointer=memory_saver)