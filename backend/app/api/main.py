from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import settings

app = FastAPI(title="rrdemo orchestrator")
app.include_router(router)
# Mounted last so /api/* takes precedence.
app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
