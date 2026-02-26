from sqlalchemy import Table, Column, Integer,Enum, String, ForeignKey, DateTime
from sqlalchemy.sql import func
from config.db import engine, meta_data



human_escalations = Table(
    "human_escalations",
    meta_data,
    Column("id", Integer, primary_key=True),
    Column("conversation_id", Integer,ForeignKey("conversation.id"), nullable=False),
    Column("reason", String(255), nullable=False),  
    Column("escalated_at", DateTime, nullable=False, server_default=func.now()),
    Column("resolved_by", String(50), nullable=False),
    Column("status",
        Enum("pending", "assigned","closed", name="status"),
        default="pending"
    ),
    extend_existing=True
)

meta_data.create_all(engine)


