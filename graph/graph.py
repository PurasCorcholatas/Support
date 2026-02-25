from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage

from langgraph.checkpoint.memory import MemorySaver
from typing import List, Literal, Annotated, cast
from typing_extensions import TypedDict
from dotenv import load_dotenv

from services.zammad_services import ZammadService

import smtplib
from email.mime.text import MIMEText
import os


load_dotenv()

memory_saver = MemorySaver()

llm = ChatOpenAI(
    model="gpt-4.1",
    temperature=0
)


class State(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], add_messages]
    intent: Literal["general", "crear_ticket"]
    human_escalated: bool
    
    
    title:str
    description:str
    email:str
    zammad_ticket_id:int
    
def langgraph(mensaje: str, thread_id: str):

    state: State = {
        "messages": [HumanMessage(content=mensaje)],
        "intent": "general"
    }

    result = graph.invoke(
        state,
        config={"configurable": {"thread_id": thread_id}}
    )

    return result["messages"][-1].content
    
def router(state: State):

    if state.get("intent") == 'crear_ticket':
        return {"intent": "crear_ticket"}
    
    
    if state.get("human_escalated"):
        return {"intent": "silencio"}

    

    history = state.get("messages", [])[-3:]

    
    conversation = "\n".join(
        str(m.content) for m in history
    )

    prompt = f"""
    Eres un clasificador determinista.
    Clasifica la intención del ÚLTIMO mensaje del usuario considerando el contexto.

    Contexto:
    {conversation}

    Responde solo con una palabra:
    - general
    -si pide humano > human
    - Si el usuario quiere crear un ticket > crear_ticket
    """

    response = llm.invoke([HumanMessage(content=prompt)])

    
    content = cast(str, response.content)

    raw_intent = content.strip().lower()
    intent = raw_intent.split()[0].replace(".", "")

    
    print("intent:", intent)

    return {"intent": intent}
    
    
def chat_general(state: State):
    system_prompt = """
        Eres un asistente 
        """

    messages = state.get("messages", [])

    if not any(isinstance(m, SystemMessage) for m in messages):
        messages = [SystemMessage(content=system_prompt)] + messages

    response = llm.invoke(messages)
    
    return {"messages": [response]}

def create_ticket(state: State):

    messages = state.get("messages", [])

    last_user_message = ""
    if messages:
        last_user_message = str(messages[-1].content).strip()

    title = state.get("title")
    description = state.get("description")
    email = state.get("email")

    
    if not title:
        return {
            "title": last_user_message,
            "messages": [{
                "role": "assistant",
                "content": "Ahora descríbeme el problema."
            }]
        }

    
    if not description:
        return {
            "description": last_user_message,
            "messages": [{
                "role": "assistant",
                "content": "Indícame tu correo electrónico."
            }]
        }

    
    if not email:
        return {
            "email": last_user_message,
            "messages": [{
                "role": "assistant",
                "content": "Perfecto, estoy creando tu ticket..."
            }]
        }

    
    assert title is not None
    assert description is not None
    assert email is not None

    ticket = ZammadService.create_ticket(
        title=title,
        body=description,
        customer_email=email
    )

    return {
        "zammad_ticket_id": ticket["id"],
        "messages": [{
            "role": "assistant",
            "content": f"Ticket creado correctamente con ID {ticket['id']} "
        }]
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


def silence(state:State):
    return{"messages":[]}


builder = StateGraph(State)


builder.add_node("chat_general", chat_general)
builder.add_node("create_ticket", create_ticket)
builder.add_node("escalate_human", escalate_human)
builder.add_node("router", router)
builder.add_node("silence",silence)

builder.add_edge(START, "router")


builder.add_conditional_edges(
    "router",
    lambda state:state["intent"],
    {
        "general": "chat_general",
        "crear_ticket": "create_ticket",
        "silence": "silence",
        "human": "escalate_human",
        
    }
)




builder.add_edge("chat_general", END)
builder.add_edge("create_ticket", END)
builder.add_edge("silence",END)


graph = builder.compile(checkpointer=memory_saver)