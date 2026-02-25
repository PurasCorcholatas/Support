from sqlalchemy import Table, Column, Integer,Enum,  ForeignKey, DateTime, INT
from sqlalchemy.sql import func
from config.db import engine, meta_data



conversation = Table(
    "conversation",
    meta_data,
    Column("id", Integer, primary_key=True),
    Column("users", INT,ForeignKey("users.id"), nullable=False),
    Column("status",
        Enum("open", "closed", name="status"),
        default="open"
    ),
    
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now()),
    
    extend_existing=True,
    
)

meta_data.create_all(engine)

