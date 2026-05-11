from sqlalchemy import Table, Column, Integer, String, DateTime
from sqlalchemy.sql import func
from config.db import meta_data

bot_messages = Table(
    "bot_messages",
    meta_data,
    Column("id", Integer, primary_key=True, index=True),
    Column("content_preview", String(100), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now())
)
