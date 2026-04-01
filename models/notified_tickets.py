from sqlalchemy import Table, Column, Integer, String, DateTime, MetaData
from datetime import datetime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from config.db import engine, meta_data




notified_tickets = Table(
    "notified_tickets",
    meta_data,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ticket_id", String, unique=True, nullable=False),
    Column("notified_at", DateTime, default=datetime.utcnow)
)


meta_data.create_all(engine)