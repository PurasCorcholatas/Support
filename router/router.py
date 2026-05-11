from config.db import SessionLocal
from fastapi import APIRouter, Request, HTTPException, Security, Depends
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from config.logger import logger

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: str = Security(API_KEY_HEADER)):
    expected_api_key = os.getenv("BOT_API_KEY")
    if not expected_api_key:
        logger.warning("BOT_API_KEY no está configurado. La ruta está desprotegida.")
        return api_key
    if api_key != expected_api_key:
        raise HTTPException(status_code=403, detail="Acceso denegado: API Key inválida")
    return api_key
from graph.main import  State, langgraph
import graph.main as graph_module
from langchain_core.messages import HumanMessage
import os 
import re
import httpx
import base64
import asyncio
import json
import hmac
import hashlib
from langchain_openai import ChatOpenAI

from typing import Optional
from sqlalchemy import select, update, insert, delete
from models.users import users
from models.bot_messages import bot_messages
from sqlalchemy import text
import openai
import cv2
import tempfile

_notified_tickets: set = set()


user = APIRouter()
chat= APIRouter()
whatssap_router = APIRouter()
router = APIRouter()


CHATWOOT_URL = os.getenv("CHATWOOT_URL")
ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID")
API_TOKEN = os.getenv("CHATWOOT_API_TOKEN")
BOT_AGENT_ID = os.getenv("BOT_AGENT_ID")
BOT_AGENT_IDS = set(os.getenv("BOT_AGENT_IDS", str(os.getenv("BOT_AGENT_ID", ""))).split(","))


# _bot_messages cache is now in PostgreSQL


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
    logger.info(" WEBHOOK RECIBIDO ")
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

        logger.info(f"Bot reactivado automaticamente para {phone_number}")
    
    except Exception as e:
        logger.error(f"Error reactivando bot: {e}")
    
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
        logger.info(f"Bot desactivado por agente humano para {phone_number}")
    except Exception as e:
        logger.error(f"Error desactivando: {e}")
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
                logger.error(f"Error descargando imagen: {response.status_code}")
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
        logger.info(f"Imagen analizada: {description}")
        return description

    except Exception as e:
        logger.error(f"Error analizando imagen: {e}")
        return None


async def download_image_as_base64(image_url: str) -> dict | None:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(
                image_url,
                headers={"api_access_token": str(API_TOKEN)}
            )
            if response.status_code != 200:
                return None
            content_type = response.headers.get("content-type", "image_jpeg").split(";")[0].strip()
            b64 = base64.b64encode(response.content).decode("utf-8")
            return {"data": b64, "mime_type": content_type}
    
    except Exception as e:
        logger.error(f"Error descargando imagen: {e}")
        return None

async def analyze_audio_from_url(audio_url:str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(
                audio_url,
                headers={"api_access_token": str(API_TOKEN)}
                
            )
            if response.status_code !=200:
                logger.error(f"Error descargando audio: {response.status_code}")
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
        logger.info(f"Audio transcrito: {transcript}")
        return transcript if transcript else None
    
    except Exception as e:
        logger.error(f"Error transcribiendo audio: {e}")
        return None



async def analyze_video_from_url(video_url: str) -> tuple[Optional[str], list[str]]:
    """
    Descarga un video, extrae fotogramas clave y los analiza usando GPT-4o.
    Retorna (descripcion, lista_de_frames_b64).
    """
    try:
        # 1. Descargar video
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            response = await client.get(video_url, headers={"api_access_token": str(API_TOKEN)})
            if response.status_code != 200:
                logger.error(f"Error descargando video: {response.status_code}")
                return None
            video_bytes = response.content

        # 2. Guardar temporalmente para que OpenCV lo pueda leer
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name

        # 3. Extraer fotogramas clave (3-4 frames)
        cap = cv2.VideoCapture(tmp_path)
        frames_b64 = []
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        if total_frames > 0:
            # Tomar frames en puntos estratégicos
            indices = [0, total_frames // 3, (2 * total_frames) // 3, total_frames - 1]
            for idx in indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if ret:
                    # Redimensionar para ahorrar tokens y tiempo (máximo 800px de ancho)
                    h, w = frame.shape[:2]
                    if w > 800:
                        new_w = 800
                        new_h = int(h * (800 / w))
                        frame = cv2.resize(frame, (new_w, new_h))
                    
                    _, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    frames_b64.append(base64.b64encode(buffer).decode("utf-8"))
        
        cap.release()
        
        # Limpieza del archivo temporal
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

        if not frames_b64:
            logger.warning("No se pudieron extraer fotogramas del video")
            return None

        # 4. Enviar secuencia a GPT-4o
        vision_llm = ChatOpenAI(model="gpt-4o", temperature=0)
        
        prompt_content = [
            {
                "type": "text",
                "text": "Analiza esta secuencia de fotogramas de un video enviado por un usuario de soporte técnico. "
                        "Describe qué problema técnico se observa, qué aplicación o hardware falla y cualquier "
                        "detalle relevante (luces parpadeando, mensajes en pantalla, etc.). "
                        "Responde de forma concisa en español como si fueras el usuario describiendo su problema."
            }
        ]
        
        for b64 in frames_b64:
            prompt_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })

        vision_response = await vision_llm.ainvoke([HumanMessage(content=prompt_content)])
        description = str(vision_response.content)
        logger.info(f"Video analizado: {description}")
        return description, frames_b64

    except Exception as e:
        logger.error(f"Error analizando video: {e}")
        return None, []



@whatssap_router.post("/whatsapp/webhook")
async def chat_webhook(request: Request):
    webhook_secret = os.getenv("CHATWOOT_WEBHOOK_SECRET")
    if webhook_secret:
        signature = request.headers.get("x-chatwoot-signature")
        if not signature:
            logger.warning("Webhook rechazado: Falta la cabecera x-chatwoot-signature")
            raise HTTPException(status_code=401, detail="Acceso denegado: Falta firma")
            
        payload = await request.body()
        expected_signature = hmac.new(
            webhook_secret.encode('utf-8'),
            payload,
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(signature, expected_signature):
            logger.warning("Webhook rechazado: Firma criptográfica inválida")
            raise HTTPException(status_code=401, detail="Acceso denegado: Firma inválida")

    data = await request.json()

    logger.info(f"EVENT: {data.get('event')}")
    logger.info(f"MESSAGE_TYPE: {data.get('message_type')}")
    logger.info(f"SENDER_TYPE: {data.get('sender', {}).get('type')}")
    logger.info(f"PRIVATE: {data.get('private')}")
    content = data.get("content") or ""
    logger.info(f"CONTENT: {content[:50]}")
    logger.info(f"FULL SENDER: {json.dumps(data.get('sender', {}))}")

    if data.get("event") == "conversation_updated":
        conv_status = (
            data.get("conversation", {}).get("status")
            or data.get("status")
        )
        logger.debug(f"DEBUG conversation_updated conv_status={conv_status}")

        if conv_status == "resolved":
            phone_number = (
                data.get("conversation", {})
                    .get("meta", {})
                    .get("sender", {})
                    .get("phone_number", "")
                or data.get("meta", {})
                    .get("sender", {})
                    .get("phone_number", "")
            )
            phone_number = phone_number.replace("+", "").strip()
            if phone_number:
                await handle_conversation_resolved(phone_number)
                logger.info(f"Bot reactivado para {phone_number}")
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
                    logger.info(f"Bot reactivado para {phone_number}")

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

        logger.debug(f"DEBUG sender_id={sender_id} BOT_AGENT_ID={BOT_AGENT_ID}")

        if str(sender_id) in BOT_AGENT_IDS:
            logger.warning("Mensaje del bot de notificaciones, ignorado")
            return {"status": "bot_notificacion_ignored"}

        db = SessionLocal()
        try:
            row = db.execute(select(bot_messages).where(bot_messages.c.content_preview == content_preview)).fetchone()
            if row:
                db.execute(delete(bot_messages).where(bot_messages.c.content_preview == content_preview))
                db.commit()
                logger.warning("Mensaje del bot, ignorado")
                return {"status": "bot_message_ignored"}
        finally:
            db.close()

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
            logger.info(f"Agente humano escribió → bot desactivado para {phone_number}")
        return {"status": "agent_message_detected"}

    if data.get("message_type") != "incoming":
        return {"status": "not incoming"}

    sender_type = (data.get("message_type") or "").lower()
    if sender_type != "incoming":
        return {"status": "not incoming"}

    message = data.get("content")
    conversation_id = data.get("conversation", {}).get("id")
    attachments = data.get("attachments", [])

    image_b64_list = []
    if not message and attachments:

        image_attachment = next(
            (a for a in attachments if a.get("file_type") == "image"), None
        )

        if image_attachment:
            image_url = image_attachment.get("data_url")
            message = await analyze_image_from_url(image_url)
            if not message:
                return {"status": "no se pudo analizar la imagen"}
            img_data = await download_image_as_base64(image_url)
            if img_data:
                image_b64_list.append(img_data)

        if not message:
            audio_attachment = next(
                (a for a in attachments if a.get("file_type") in ("audio", "voice_note")), None
            )
            if audio_attachment:
                audio_url = audio_attachment.get("data_url")
                message = await analyze_audio_from_url(audio_url)
                if not message:
                    return {"status": "no se pudo transcribir el audio"}

        if not message:
            video_attachment = next(
                (a for a in attachments if a.get("file_type") in ("video", "video_note")), None
            )
            if video_attachment:
                video_url = video_attachment.get("data_url")
                message, frames = await analyze_video_from_url(video_url)
                if not message:
                    return {"status": "no se pudo analizar el video"}
                
                # Adjuntamos los frames del video como imágenes para el ticket
                for f_b64 in frames:
                    image_b64_list.append({"data": f_b64, "mime_type": "image/jpeg"})

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
        logger.info("Agente humano escribio -> bot desactivado")
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
        logger.warning(f"Bot inactivo para {phone_number}, agente humano manejando")
        return {"status": "bot_inactive"}

    logger.info(f"PHONE NUMBER: {phone_number}")
    logger.info(f"CONVERSATION ID: {conversation_id}")

    respuestas = await langgraph(message, phone_number, image_b64_list=image_b64_list, chatwoot_conversation_id=conversation_id)  # type: ignore
    if respuestas is None:
        return

    url = f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/messages"
    headers = {
        "api_access_token": API_TOKEN,
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=15) as http_client:
        for texto in respuestas:
            db_bm = SessionLocal()
            try:
                db_bm.execute(insert(bot_messages).values(content_preview=texto[:100]))
                db_bm.commit()
            except Exception as e:
                logger.error(f"Error guardando bot_message: {e}")
            finally:
                db_bm.close()
                
            payload = {
                "content": texto,
                "message_type": "outgoing",
            }
            response = await http_client.post(url, json=payload, headers=headers)
            logger.info(f"STATUS CHATWOOT {response.status_code}")

    return {"status": "ok"}
 


@router.post("/bot/reactivar/{phone_number}")
async def reactivar_bot(phone_number: str, api_key: str = Depends(verify_api_key)):
    db = SessionLocal()
    try:
        result = db.execute(
            select(users).where(users.c.phone_number == phone_number)
        ).fetchone()
        
        if not result:
            return{"error": "Usuario no encontrado"}
        
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
       
       
