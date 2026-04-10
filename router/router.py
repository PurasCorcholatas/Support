from config.db import SessionLocal
from fastapi import APIRouter   , Request
from pydantic import BaseModel
from graph.graph import  State, langgraph
import graph.graph as graph_module
from langchain_core.messages import HumanMessage
import os 
import re
import httpx
import base64
import asyncio
import json
from langchain_openai import ChatOpenAI
import requests
from typing import Optional
from sqlalchemy import select, update
from models.users import users
from sqlalchemy import text
import openai

_notified_tickets: set = set()


user = APIRouter()
chat= APIRouter()
whatssap_router = APIRouter()
router = APIRouter()


CHATWOOT_URL = os.getenv("CHATWOOT_URL")
ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID")
API_TOKEN = os.getenv("CHATWOOT_API_TOKEN")
BOT_AGENT_ID = os.getenv("BOT_AGENT_ID")


_bot_messages: set = set()


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
    print(" WEBHOOK RECIBIDO ")
    return {"status": "ok"}


@chat.post("/chat", response_model=ChatResponse)
def chat_endopoint( payload: ChatRequest):
    user_id = payload.session_id
    state: State = {
    "messages": [HumanMessage(content=payload.message)],
    "intent": "chat_general"
}
    
    result = graph_module.graph.invoke(
        state,
        config = {"configurable": {"thread_id": str(user_id)}}
    )    
    

    messages = result.get("messages", [])
    answer = messages[-1].content if messages else "No se puedo generar la respuesta"
    return {"answer": answer}


async def handle_conversation_resolved(phone_number: str):
    db = SessionLocal()
    
    try:
        db.execute(
            update(users)
            .where(users.c.phone_number == phone_number)
            .values(
                bot_active = True,
                greeting_step="confirm_branch"
            )
        )
        db.commit()

        db.execute(text("DELETE FROM checkpoint_writes WHERE thread_id = :tid"), {"tid": phone_number})
        db.execute(text("DELETE FROM checkpoint_blobs WHERE thread_id = :tid"), {"tid": phone_number})
        db.execute(text("DELETE FROM checkpoints WHERE thread_id = :tid"), {"tid": phone_number})
        db.commit()

        print(f"Bot reactivado automaticamente para {phone_number}")
    
    except Exception as e:
        print(f"Error reactivando bot: {e}")
    
    finally:
        db.close()


async def deactivate_bot_for_agent(phone_number:str):
    db = SessionLocal()
    try:
        db.execute(
            update(users)
            .where(users.c.phone_number == phone_number)
            .values(bot_active=False)
        )
        db.commit()
        print(f"Bot desactivado por agente humano para {phone_number}")
    except Exception as e:
        print(f"Error desactivando: {e}")
    finally:
        db.close()



async def analyze_image_from_url(image_url: str) -> Optional[str]:
    try:
        

        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(
            image_url, 
            headers={"api_access_token": str(API_TOKEN)}
            )
            if response.status_code != 200:
                print(f"Error descargando imagen: {response.status_code}")
                return None

            image_data = base64.b64encode(response.content).decode("utf-8")
            content_type = response.headers.get("content-type", "image/jpeg")

        vision_llm = ChatOpenAI(model="gpt-4o", temperature=0)

        vision_response = vision_llm.invoke([
            HumanMessage(content=[
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{content_type};base64,{image_data}"
                    }
                },
                {
                    "type": "text",
                    "text": """Eres un técnico de soporte IT. Analiza esta imagen y describe:
1. Qué error o problema muestra
2. Qué sistema o aplicación está afectada
3. Cualquier código de error visible

Responde en español de forma concisa, como si fuera el usuario describiendo su problema.
Ejemplo: 'Me aparece el error 503 Service Unavailable en el navegador al intentar entrar al sistema de facturación'
Si no es una imagen de problema técnico, describe brevemente qué muestra."""
                }
            ])
        ])

        description = str(vision_response.content)
        print(f"Imagen analizada: {description}")
        return description

    except Exception as e:
        print(f"Error analizando imagen: {e}")
        return None


async def analyze_audio_from_url(audio_url:str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(
                audio_url,
                headers={"api_access_token": str(API_TOKEN)}
                
            )
            if response.status_code !=200:
                print(f"Error descargando audio: {response.status_code}")
                return None
                
            audio_bytes = response.content
            content_type = response.headers.get("content-type", "audio/ogg")
            
        ext_map = {
            "audio/ogg":"ogg",
            "audio/mpeg": "mp3",
            "audio/mp4": "mp4",
            "audio/wav": "wav",
            "audio/webm": "webm"
        }
        
        ext = ext_map.get(content_type.split(";")[0].strip(), "ogg")
        client_oai = openai.AsyncOpenAI()
        
        
        transcription = await client_oai.audio.transcriptions.create(
            model = "whisper-1",
            file=(f"audio.{ext}", audio_bytes, content_type),
        )

        transcript = transcription.text.strip()
        print(f"Audio transcrito: {transcript}")
        return transcript if transcript else None
    
    except Exception as e:
        print(f"Error transcribiendo audio: {e}")
        return None



@whatssap_router.post("/whatsapp/webhook")
async def chat_webhook(request: Request):
    data = await request.json()
    
    
    print("EVENT:", data.get("event"))
    print("MESSAGE_TYPE:", data.get("message_type"))
    print("SENDER_TYPE:", data.get("sender", {}).get("type"))
    print("PRIVATE:", data.get("private"))
    content = data.get("content") or ""
    print("CONTENT:", content[:50])
    print("FULL SENDER:", json.dumps(data.get("sender", {})))
    # print("FULL CONVERSATION META:", json.dumps(data.get("conversation", {}).get("meta", {})))
    
    
 
    if data.get("event") == "conversation_updated":
        if data.get("status") == "resolved":
            phone_number = (
                data.get("meta", {})
                .get("sender", {})
                .get("phone_number", "")
                .replace("+", "")
                .strip()
            )
            if phone_number:
                await handle_conversation_resolved(phone_number)
                print(f"Bot reactivado para {phone_number}")
        return {"status": "ok"}
 
    if data.get("event") != "message_created":
        return {"status": "ignored"}
 
    if data.get("private") is True:
        content = data.get("content", "").strip().lower()
        if content == "/activar":
            phone_number = (
                data.get("conversation", {})
                    .get("meta", {})
                    .get("sender", {})
                    .get("phone_number")
            )
            if phone_number:
                phone_number = phone_number.replace("+", "").strip()
                db = SessionLocal()
                try:
                    db.execute(
                        update(users)
                        .where(users.c.phone_number == phone_number)
                        .values(
                            bot_active=True,
                            greeting_step="confirm_branch"
                        )
                    )
                    db.commit()
 
                    db.execute(text("DELETE FROM checkpoint_writes WHERE thread_id = :tid"), {"tid": phone_number})
                    db.execute(text("DELETE FROM checkpoint_blobs WHERE thread_id = :tid"), {"tid": phone_number})
                    db.execute(text("DELETE FROM checkpoints WHERE thread_id = :tid"), {"tid": phone_number})
                    db.commit()
                    print(f"Bot reactivado para {phone_number}")
 
                finally:
                    db.close()
        return {"status": "private note ignore"}
 
    sender = data.get("sender", {})
    sender_id = sender.get("id")
    sender_email = sender.get("email")
    content_preview = (data.get("content") or "")[:100]

    if (data.get("message_type") == "outgoing"
        and data.get("private") is not True
        and sender_id is not None
        and sender_email is not None):
        
        print(f"DEBUG sender_id={sender_id} BOT_AGENT_ID={BOT_AGENT_ID}")
        
        if str(sender_id) == str(BOT_AGENT_ID):
            print("Mensaje del bot de notificaciones, ignorado")
            return {"status": "bot_notificacion_ignored"}
        
        
        
        if content_preview in _bot_messages:
            _bot_messages.discard(content_preview)
            print("Mensaje del bot, ignorado")
            return {"status": "bot_message_ignored"}
        
        phone_number = (
            data.get("conversation", {})
                .get("meta", {})
                .get("sender", {})
                .get("phone_number")
            or str(data.get("conversation", {}).get("id", ""))
        )
        phone_number = phone_number.replace("+", "").strip()
        
        if phone_number:
            await deactivate_bot_for_agent(phone_number)
            print(f"Agente humano escribió → bot desactivado para {phone_number}")
        return {"status": "agent_message_detected"}
 
    if data.get("message_type") != "incoming":
        return {"status": "not incoming"}
 
    sender_type = data.get("sender", {}).get("type", "")
    if sender_type in ("agent_bot", "bot", "user"):
        return {"status": "bot_message, ignored"}
 
    message = data.get("content")
    conversation_id = data.get("conversation", {}).get("id")
    attachments = data.get("attachments", [])
 
    if not message and attachments:
        
        image_attachment = next(
            (a for a in attachments if a.get("file_type") == "image"), None
        )
        if image_attachment:
            image_url = image_attachment.get("data_url")
            message = await analyze_image_from_url(image_url)
            if not message:
                return {"status": "no se pudo analizar la imagen"}

        
        if not message:
            audio_attachment = next(
                (a for a in attachments if a.get("file_type") in ("audio", "voice_note")), None
            )
            if audio_attachment:
                audio_url = audio_attachment.get("data_url")
                message = await analyze_audio_from_url(audio_url)
                if not message:
                    return {"status": "no se pudo transcribir el audio"}
 
    if not message or not conversation_id:
        return {"status": "missing data"}
 
 
    phone_number = (
        data.get("conversation", {})
            .get("meta", {})
            .get("sender", {})
            .get("phone_number")
        or str(conversation_id)
    )
    phone_number = phone_number.replace("+", "").strip()
    
    sender_type = data.get("sender", {}).get("type", "")
    if sender_type == "agent":
        await deactivate_bot_for_agent(phone_number)
        print("Agente humano escribio -> bot desactivado")
        return {"status": "agent_took_over"}
    
    
    db = SessionLocal()
    try:
        result = db.execute(
            select(users).where(users.c.phone_number == phone_number)
        ).fetchone()
        bot_active = result.bot_active if result else True
    finally:
        db.close()
        
    
    if not bot_active:
        print(f"Bot inactivo para {phone_number}, agente humano manejando")
        return {"status": "bot_inactive"}
    
    print("PHONE NUMBER:", phone_number)
    print("CONVERSATION ID:", conversation_id)
 
    respuestas = await langgraph(message, phone_number) #type: ignore
    if respuestas is None:
        return

    url = f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/messages"
    headers = {
        "api_access_token": API_TOKEN,
        "Content-Type": "application/json"
        
    }
    
    for texto in respuestas:
        _bot_messages.add(texto[:100])
        payload = {
            "content": texto,
            "message_type": "outgoing",
            
        }
        response = requests.post(url, json=payload, headers=headers)
        
        print("STATUS CHATWOOT", response.status_code)
        
    return {"status": "ok"}
 


@router.post("/bot/reactivar/{phone_number}")
async def reactivar_bot(phone_number: str):
    db = SessionLocal()
    try:
        result = db.execute(
            select(users).where(users.c.phone_number == phone_number)
        ).fetchone()
        
        if not result:
            return{"erro": "Usuario no encontrado"}
        
        db.execute(
            update(users)
            .where(users.c.phone_number == phone_number)
            .values(
                bot_active=True,
                greeting_step= "confirm_branch")
        )
        db.commit()
        
        return {
            "status": "ok", 
            "mensaje": f"Bot reactivado para {phone_number}"
            
        }
    except Exception as e:
        return {"error": str(e)}
    
    finally:
        db.close()
       
       
