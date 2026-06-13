import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from app.core.logging_config import setup_logging
from app.routers import chat, upload

load_dotenv(override=True)
setup_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server starting — checking Playwright browser installation...")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    logger.info("Playwright ready. Server is up.")
    yield
    logger.info("Server shutting down.")


app = FastAPI(
    title="RAG Data Assistant",
    description="Chat with your documents using AI",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(upload.router)
app.include_router(chat.router)

if os.getenv("WHATSAPP_ENABLED", "false").lower() == "true":
    from app.routers import whatsapp
    app.include_router(whatsapp.router)
    logger.info("WhatsApp webhook active at /whatsapp/webhook")


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
