"""
Module: Application Entrypoint
Purpose: Defines and exposes the FastAPI application instance (`app`) that the
         ASGI server (uvicorn) imports and serves.
"""
import os
import threading
import webbrowser

from fastapi import FastAPI

from api.uploads import router as uploads_router
from config.setting import get_settings
from core.logging import configure_logging

configure_logging()

app = FastAPI(title="Backend API")

app.include_router(uploads_router)


@app.on_event("startup")
async def _open_swagger() -> None:
    settings = get_settings()
    if settings.ENVIRONMENT != "development":
        return
    if os.environ.get("OPEN_DOCS", "true").lower() == "false":
        return
    if os.environ.get("RUN_MAIN") not in (None, "true"):
        return
    threading.Timer(
        1.0, lambda: webbrowser.open("http://127.0.0.1:8000/docs")
    ).start()


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "backend-api"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}
