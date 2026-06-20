"""
Module: Application Entrypoint
Purpose: Defines and exposes the FastAPI application instance (`app`) that the
         ASGI server (uvicorn) imports and serves.
"""
from fastapi import FastAPI

from api.uploads import router as uploads_router
from core.logging import configure_logging

configure_logging()

app = FastAPI(title="Backend API")

app.include_router(uploads_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "backend-api"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}
