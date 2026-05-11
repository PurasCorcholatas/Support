
import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg2://postgres:simon@localhost:5432/support_ia")
engine = create_engine(DATABASE_URL)

phone_number = '573007901240'

queries = [
    # 1. Borrar mensajes
    f"""
    DELETE FROM messages
    WHERE conversation_id IN (
        SELECT id FROM conversation 
        WHERE users IN (
            SELECT id FROM users WHERE phone_number = '{phone_number}'
        )
    );
    """,
    # 2. Borrar tickets
    f"""
    DELETE FROM tickets
    WHERE conversation_id IN (
        SELECT id FROM conversation 
        WHERE users IN (
            SELECT id FROM users WHERE phone_number = '{phone_number}'
        )
    );
    """,
    # 3. Borrar conversaciones
    f"""
    DELETE FROM conversation
    WHERE users IN (
        SELECT id FROM users WHERE phone_number = '{phone_number}'
    );
    """,
    # 4. Borrar memoria
    f"""
    DELETE FROM user_memory
    WHERE phone_number = '{phone_number}';
    """,
    # 5. Borrar checkpoints de LangGraph
    f"""
    DELETE FROM checkpoints
    WHERE thread_id = '{phone_number}';
    """,
    f"""
    DELETE FROM checkpoint_blobs
    WHERE thread_id = '{phone_number}';
    """,
    f"""
    DELETE FROM checkpoint_writes
    WHERE thread_id = '{phone_number}';
    """,
    # 6. Borrar usuario
    f"""
    DELETE FROM users
    WHERE phone_number = '{phone_number}';
    """
]

with engine.connect() as conn:
    trans = conn.begin()
    try:
        for query in queries:
            conn.execute(text(query))
        trans.commit()
        print(f"User {phone_number} and all traces deleted successfully.")
    except Exception as e:
        if trans.is_active:
            trans.rollback()
        print(f"Error during cleanup: {e}")
