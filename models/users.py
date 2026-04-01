from sqlalchemy import Table, Column, Integer, String, DateTime, INT, Text,Boolean
from sqlalchemy.sql import func
from config.db import engine, meta_data



users = Table(
    "users",
    meta_data,
    Column("id", Integer, primary_key=True),
    Column("phone_number", INT, nullable=False),
    Column("name", String(20), nullable=False),
    Column("company", String(40)),
    Column("email", String(200), nullable=False, unique=True),
    Column("sede", Text, nullable=False),
    Column("bot_active", Boolean, nullable=False, server_default="true"),
    Column("greeting_step",String, nullable=True ),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    extend_existing=True
    
)

meta_data.create_all(engine)
