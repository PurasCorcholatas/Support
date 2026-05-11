import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from router.router import user, chat, whatssap_router, router
from graph.main import init_llm_with_tools
from services.cleanup import cleaner_bot_messages_loop
from services.zammad_services import start_zammad_polling
import models.notified_tickets 
import models.bot_messages
from config.db import meta_data, engine

meta_data.create_all(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_llm_with_tools()
    asyncio.create_task(cleaner_bot_messages_loop())
    asyncio.create_task(start_zammad_polling(interval=15))  
    yield


app = FastAPI(lifespan=lifespan)

app.include_router(user, prefix="/api")
app.include_router(chat, prefix="/chat")
app.include_router(whatssap_router)
app.include_router(router)

@app.get("/")
def root():
    return {"status": "ok"}