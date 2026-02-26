from sqlalchemy import Table, Column, Integer,Text, String, ForeignKey, DateTime, INT
from sqlalchemy.sql import func
from config.db import engine, meta_data



messages = Table(
    "messages",
    meta_data,
    Column("id", Integer, primary_key=True),
    Column("conversation_id", Integer,ForeignKey("conversation.id"), nullable=False),
    Column("sender", String(20), nullable=False),
    Column("message_text", Text, nullable=False),
    Column("company", String(255), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    extend_existing=True
)

meta_data.create_all(engine)
