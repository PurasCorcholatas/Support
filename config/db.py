from sqlalchemy import create_engine, MetaData
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "postgresql+psycopg2://postgres:simon@localhost:5432/support_ia"
LANGGRAPH_DB_URL = "postgresql://postgres:simon@localhost:5432/support_ia"

engine = create_engine(
    DATABASE_URL,
    echo=False
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

meta_data = MetaData()


def get_db():
    db = SessionLocal()
    return db