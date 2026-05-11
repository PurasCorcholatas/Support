from sqlalchemy import Table, Column, Integer, String, DateTime, BigInteger, Text
from sqlalchemy import func 
from config.db import engine, meta_data


user_memory = Table(
    "user_memory",
    meta_data,
    Column("id", Integer, primary_key=True),
    Column("phone_number", BigInteger, nullable=False),
    Column("summary", Text, nullable=False),
    Column("category", String(50), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
)



