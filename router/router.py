from config.db import engine, get_db
from fastapi import APIRouter , Depends , Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from graph.graph import graph, State, langgraph
from langchain_core.messages import HumanMessage
import os
import requests

user = APIRouter()
chat= APIRouter()
whatssap_router = APIRouter()



CHATWOOT_URL = os.getenv("CHATWOOT_URL")
ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID")
API_TOKEN = os.getenv("CHAT_API_TOKEN")




class ChatRequest(BaseModel):
    message: str
    session_id: str
    
    
class ChatResponse(BaseModel):
    answer:str


@user.get("/")
def root():
    return{"status": "ok"}

@chat.post("/webhook/chatwoot")
async def chat_whatsapp_webhook(request: Request):
    print("🔥🔥 WEBHOOK RECIBIDO 🔥🔥")
    return {"status": "ok"}


@chat.post("/chat", response_model=ChatResponse)
def chat_endopoint( payload: ChatRequest):
    user_id = payload.session_id
    state: State = {
    "messages": [HumanMessage(content=payload.message)],
    "intent": "general"
}
    
    result = graph.invoke(
        state,
        config = {"configurable": {"thread_id": str(user_id)}}
    )    
    

    messages = result.get("messages", [])
    answer = messages[-1].content if messages else "No se puedo generar la respuesta"
    return {"answer": answer}

from fastapi import Request


@whatssap_router.post("/whatsapp/webhook")
async def chat_webhook(request: Request):

    data = await request.json()

    if data["event"] != "message_created":
        return {"status": "ignored"}

    if data["message"]["message_type"] != "incoming":
        return {"status": "ignored"}

    message = data["message"]["content"]
    conversation_id = data["conversation"]["id"]

    respuesta = langgraph(message, str(conversation_id))

    url = f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/messages"

    headers = {
        "api_access_token": API_TOKEN,
        "Content-Type": "application/json"
    }

    payload = {
        "content": respuesta,
        "message_type": "outgoing"
    }

    requests.post(url, json=payload, headers=headers)

    return {"status": "ok"}

