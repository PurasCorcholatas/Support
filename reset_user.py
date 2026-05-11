
import os
import sys
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

# Configuración de base de datos
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg2://postgres:simon@localhost:5432/support_ia")
engine = create_engine(DATABASE_URL)

def reset_user(phone_number):
    phone_number = phone_number.replace("+", "").strip()
    
    queries = [
        # 1. Borrar mensajes
        f"DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversation WHERE users IN (SELECT id FROM users WHERE phone_number = '{phone_number}'));",
        # 2. Borrar tickets
        f"DELETE FROM tickets WHERE conversation_id IN (SELECT id FROM conversation WHERE users IN (SELECT id FROM users WHERE phone_number = '{phone_number}'));",
        # 3. Borrar conversaciones
        f"DELETE FROM conversation WHERE users IN (SELECT id FROM users WHERE phone_number = '{phone_number}');",
        # 4. Borrar memoria
        f"DELETE FROM user_memory WHERE phone_number = '{phone_number}';",
        # 5. Borrar checkpoints de LangGraph (Estado del flujo)
        f"DELETE FROM checkpoints WHERE thread_id = '{phone_number}';",
        f"DELETE FROM checkpoint_blobs WHERE thread_id = '{phone_number}';",
        f"DELETE FROM checkpoint_writes WHERE thread_id = '{phone_number}';",
        # 6. Borrar usuario
        f"DELETE FROM users WHERE phone_number = '{phone_number}';"
    ]

    with engine.connect() as conn:
        trans = conn.begin()
        try:
            for query in queries:
                conn.execute(text(query))
            trans.commit()
            print(f"--- REINICIO EXITOSO ---")
            print(f"Usuario {phone_number} ha sido eliminado de todas las tablas.")
            print(f"Estado de LangGraph (Checkpoints) limpiado.")
            print(f"El proximo mensaje de este numero iniciara el flujo desde cero.")
        except Exception as e:
            if trans.is_active:
                trans.rollback()
            print(f"Error durante el reinicio: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        phone = sys.argv[1]
    else:
        phone = "573007901240" # Por defecto tu numero
    
    reset_user(phone)
