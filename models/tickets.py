from sqlalchemy import Table, Column, Integer,Enum, String, ForeignKey, DateTime
from sqlalchemy.sql import func
from config.db import engine, meta_data



tickets = Table(
    "tickets",
    meta_data,
    Column("id", Integer, primary_key=True),
    Column("conversation_id", Integer,ForeignKey("conversation.id"), nullable=False),
    Column("zammad_ticket_id", Integer, nullable=False),
    Column("subject", String(255), nullable=False),  
    Column("status",String(50), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now()),
    extend_existing=True
)

meta_data.create_all(engine)


