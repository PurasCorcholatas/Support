from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage, AIMessage
from langgraph.checkpoint.memory import MemorySaver
from typing import List, Literal, Annotated, cast, Optional
from typing_extensions import TypedDict
from dotenv import load_dotenv
from tools.registry import tools
from tools.functions.generator_password import generator_pw
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


llm_with_tools = llm.bind_tools(tools)


class State(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], add_messages]
    intent: Literal["chat_general", "crear_ticket", "human", "silence", "estado_ticket"]
    human_escalated: bool
    ticket_step: Literal["ask_info","ask_title", "ask_description", "ask_email", "done"]

    title: str
    description: str
    email: str
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
    
    if state.get("ticket_step") and state.get("ticket_step") != "done":
        return {"intent": "crear_ticket"}

    if state.get("flow") =="password":
        return {"intent": "chat_general"}
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
    - chat_general
    - human
    - crear_ticket
    -estado_ticket
    """

    response = llm.invoke([HumanMessage(content=prompt)])

    content = cast(str, response.content)
    intent = content.strip().lower().split()[0].replace(".", "")

    print("Intent detectado:", intent)

    return {"intent": intent}




def chat_general(state: State, config):

    thread_id = config["configurable"]["thread_id"]

    system_prompt = f"""
Eres un asistente de soporte técnico empresarial.

Si el usuario menciona palabras como:
- contraseña
- clave
- password
- resetear
- recuperar acceso
- clave corporativa

Debes usar la herramienta guia_cambio_clave.

Si el usuario solicita generar una contraseña,
usa la herramienta generar_password
y pásale como argumento el thread_id: {thread_id}

Nunca inventes contraseñas manualmente.
Siempre usa la herramienta.
"""

    messages_state = state.get("messages", [])

    if messages_state:
        last_message = str(messages_state[-1].content).strip().lower()
    else:
        last_message = ""

    if state.get("awaiting_confirmation"):
        if last_message in ["si", "sí", "dale", "ok", "generala", "genérala", "esta bien", "está bien"]:
            resultado = generator_pw.invoke({
                "thread_id": thread_id
            })
            return {
                "awaiting_confirmation": False,
                "messages": [
                    AIMessage(content=str(resultado))
                ]
            }
        else:
            return {
                "awaiting_confirmation": False,
                "messages": [
                    AIMessage(content="Entendido, no generaré la contraseña.")
                ]
            }

    if not any(isinstance(m, SystemMessage) for m in messages_state):
        messages_state = [SystemMessage(content=system_prompt)] + messages_state

    response = llm_with_tools.invoke(messages_state)

    return {"messages": [response]}


def create_ticket(state: State):

    thread_id = state.get("thread_id")
    db = SessionLocal()

    messages_state = state.get("messages", [])
    last_user_message = str(messages_state[-1].content).strip()
    step = state.get("ticket_step")

    phone_number = thread_id

    

    stmt = select(users).where(users.c.phone_number == phone_number)
    result = db.execute(stmt).fetchone()

    if result:
        user_id = result.id
        is_new_user = False
    else:
        is_new_user = True
        user_id = None

    

    if not step:

        if is_new_user:
            return {
                "ticket_step": "ask_user_info",
                "messages": [
                    AIMessage(
                        content="Antes de continuar, indícame tu nombre y el nombre de tu empresa.\nEjemplo: Juan Pérez - Tech Solutions"
                        
                    )
                ]
            }
        else:
            return {
                "ticket_step": "ask_description",
                "messages": [
                    AIMessage(content="Descríbeme el problema que estás presentando.")
                ]
            }

    

    if step == "ask_user_info":

        
        if "-" in last_user_message:
            parts = last_user_message.split("-", 1)
            name = parts[0].strip()
            company = parts[1].strip()
        else:
            
            extract_prompt = f"""
            Extrae el nombre y la empresa del siguiente texto.

            Texto:
            {last_user_message}

            Responde solo en este formato:
            nombre|empresa
            """

            response = llm.invoke([HumanMessage(content=extract_prompt)])
            content = str(response.content).strip()

            if "|" in content:
                name, company = content.split("|", 1)
                name = name.strip()
                company = company.strip()
            else:
                name = last_user_message
                company = "No especificada"

        
        stmt = (
            insert(users)
            .values(
                phone_number=phone_number,
                name=name,
                company=company
            )
            .returning(users.c.id)
        )

        user_id = db.execute(stmt).scalar_one()
        db.commit()

        return {
            "ticket_step": "ask_description",
            "messages": [
                AIMessage(
                    content="Perfecto. Ahora descríbeme el problema que estás presentando."
                )
            ]
        }

    

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
            company="",
            message_text=last_user_message
        )
    )
    db.commit()

 

    if step == "ask_description":

        description = last_user_message

        title_prompt = f"""
        Genera un título corto y profesional (máximo 8 palabras)
        para este ticket:

        {description}

        Solo responde el título.
        """

        title = str(
            llm.invoke([HumanMessage(content=title_prompt)]).content
        ).strip().replace('"', '')

        priority_prompt = f"""
        Clasifica la prioridad del problema:

        {description}

        Responde solo: baja, media o alta
        """

        priority_text = str(
            llm.invoke([HumanMessage(content=priority_prompt)]).content
        ).strip().lower()

        priority_map = {
            "baja": 1,
            "media": 2,
            "alta": 3
        }

        priority = priority_map.get(priority_text, 2)

        return {
            "description": description,
            "title": title,
            "priority": priority,
            "ticket_step": "ask_email",
            "messages": [
                AIMessage(
                    content=f"""He generado:
            Título: {title}
            Prioridad: {priority_text.upper()}

        Ahora indícame tu correo electrónico."""
                )
            ]
        }

  
    if step == "ask_email":

        title = state.get("title") or ""
        description = state.get("description") or ""
        priority = state.get("priority") or 2

        stmt = select(users).where(users.c.phone_number == phone_number)
        user = db.execute(stmt).fetchone()

        name = user.name if user else "Sin nombre"
        company = user.company if user else "Sin empresa"

        extra_info = f"""

-------------------------
Información adicional
-------------------------
Nombre: {name}
Empresa: {company}
Teléfono: {phone_number}
"""

        full_body = description + extra_info

        ticket = ZammadService.create_ticket(
            title=title,
            body=full_body,
            customer_email=last_user_message,
            priority_id=priority
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
        db.commit()

        return {
            "ticket_step": "done",
            "intent": "general",
            "messages": [
                AIMessage(
                    content=f"El ticket fue creado correctamente con ID {zammad_id}"
                )
            ]
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


tool_node = ToolNode(tools)

builder = StateGraph(State)

builder.add_node("router", router)
builder.add_node("chat_general", chat_general)
builder.add_node("create_ticket", create_ticket)
builder.add_node("check_status_ticket", check_status_ticket)
builder.add_node("escalate_human", escalate_human)
builder.add_node("tools", tool_node)

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
        "silence": "silence",
    }
)

builder.add_edge("chat_general", END)
builder.add_edge("create_ticket", END)
builder.add_edge("check_status_ticket", END)
builder.add_edge("tools", "chat_general")


builder.add_conditional_edges(
    "chat_general",
    tools_condition,
) 

builder.add_edge("escalate_human", END)
builder.add_edge("silence", END)

graph = builder.compile(checkpointer=memory_saver)