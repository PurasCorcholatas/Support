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


async def cleaner_conversation():
    """
    Limpia el estado de conversaciones inactivas en LangGraph.
    Busca checkpoints que no han tenido actividad en las últimas
    INACTIVIDAD_HORAS horas y elimina sus datos de estado.
    """
    try:
        db = SessionLocal()
        limite = datetime.now(timezone.utc) - timedelta(hours=INACTIVIDAD_HORAS)

        resultado = db.execute(text("""
            DELETE FROM checkpoint_writes
            WHERE thread_id IN (
                SELECT DISTINCT thread_id
                FROM checkpoints
                WHERE updated_at < :limite
            )
        """), {"limite": limite})

        writes_eliminados = getattr(resultado, 'rowcount', 0)

        resultado2 = db.execute(text("""
            DELETE FROM checkpoint_blobs
            WHERE thread_id IN (
                SELECT DISTINCT thread_id
                FROM checkpoints
                WHERE updated_at < :limite
            )
        """), {"limite": limite})

        blobs_eliminados = getattr(resultado2, 'rowcount', 0)

        resultado3 = db.execute(text("""
            DELETE FROM checkpoints
            WHERE updated_at < :limite
        """), {"limite": limite})

        checkpoints_eliminados = getattr(resultado3, 'rowcount', 0)

        db.commit()
        db.close()

        logger.info(
            "Limpieza completada — checkpoints: %s, blobs: %s, writes: %s",
            checkpoints_eliminados,
            blobs_eliminados,
            writes_eliminados,
        )

    except Exception:
        logger.exception("Error en limpieza de conversaciones huérfanas")


async def start_clean_periodic():
    """
    Loop que corre indefinidamente y ejecuta la limpieza
    cada INACTIVIDAD_HORAS horas.
    """
    print(f"Limpieza periódica iniciada — intervalo: {INACTIVIDAD_HORAS} horas")
    logger.info(
        "Limpieza periódica iniciada — intervalo: %s horas",
        INACTIVIDAD_HORAS
    )
    while True:
        await asyncio.sleep(INACTIVIDAD_HORAS * 3600)
        await cleaner_conversation()