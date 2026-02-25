from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage

from langgraph.checkpoint.memory import MemorySaver
from typing import List, Literal, Annotated, cast
from typing_extensions import TypedDict
from dotenv import load_dotenv

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
    - crear_ticket
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
    return
 

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



builder = StateGraph(State)


builder.add_node("chat_general", chat_general)
builder.add_node("create_ticket", create_ticket)
builder.add_node("escalate_human", escalate_human)
builder.add_node("router", router)

builder.add_edge(START, "chat_general")


# builder.add_conditional_edges(
#     "router",
#     lambda state:state["intent"],
#     {
#         "human": "escalate_human",
#         "general": "chat_general",
#         "create_ticket": "creare_ticket"
        
#     }
# )




builder.add_edge("chat_general", END)




graph = builder.compile(checkpointer=memory_saver)