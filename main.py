from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI
from router.router import user, chat, whatssap_router
from graph.graph import init_llm_with_tools



app = FastAPI()


app.include_router(user, prefix="/api")
app.include_router(chat, prefix="/chat")
app.include_router(whatssap_router)


@app.get("/")
def root():
    return{"status": "ok"}