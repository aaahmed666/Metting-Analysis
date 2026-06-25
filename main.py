"""
Module: Application Entrypoint
Purpose: Defines and exposes the FastAPI application instance (`app`) that the
         ASGI server (uvicorn) imports and serves.
"""
from fastapi import FastAPI
import os
import threading
import webbrowser
from api.uploads import router as uploads_router
from config.setting import get_settings
from fastapi.middleware.cors import CORSMiddleware

settings = get_settings()

from app.api.auth import router as auth_router
from app.api.manager import manager_router
from app.api.meetings import router as meetings_router

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router,   prefix="/api/v1")
app.include_router(manager_router, prefix="/api/v1")
app.include_router(meetings_router, prefix="/api/v1")
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


@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "version": settings.APP_VERSION, "docs": "/docs"}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "healthy"}
