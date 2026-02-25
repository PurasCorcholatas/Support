from sqlalchemy import Table, Column, Integer,Text, String, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from config.db import engine, meta_data



agent_states = Table(
    "agent_states",
    meta_data,
    Column("id", Integer, primary_key=True),
    Column("conversation_id", Integer,ForeignKey("conversations.id"), nullable=False),
    Column("current_step", String(100), nullable=False),
    Column("collected_data", JSONB),    
    Column("updated_at", DateTime, nullable=False, server_default=func.now()),
    extend_existing=True
)

meta_data.create_all(engine)


