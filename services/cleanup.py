import asyncio
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import text
from config.db import SessionLocal

logger = logging.getLogger(__name__)

INACTIVIDAD_HORAS = 8

LIMPIAR_CAMPOS = [
    "greeting_step",
    "diagnosis_step", 
    "diagnosis_history",
    "support_option_step",
    "password_step",
    "security_risk",
    "real_data",
    "security_questions",
    "user_answers",
    "current_question_index",
    "ticket_step",
    "ticket_status_step",
]


async def cleaner_bot_messages_loop():
    """Limpia los mensajes del bot de la base de datos cada 5 minutos."""
    logger.info("Limpieza de bot_messages iniciada — intervalo: 5 minutos")
    while True:
        try:
            db = SessionLocal()
            limite = datetime.now(timezone.utc) - timedelta(minutes=5)
            resultado = db.execute(text("DELETE FROM bot_messages WHERE created_at < :limite"), {"limite": limite})
            db.commit()
            db.close()
            eliminados = getattr(resultado, 'rowcount', 0)
            if eliminados > 0:
                logger.info("Limpieza bot_messages: %s eliminados", eliminados)
        except Exception:
            logger.exception("Error en limpieza de bot_messages")
        await asyncio.sleep(300)