"""
Module: Application Entrypoint
Purpose: Defines and exposes the FastAPI application instance (`app`) that the
         ASGI server (uvicorn) imports and serves.
"""
from fastapi import FastAPI

from api.uploads import router as uploads_router

app = FastAPI(title="Backend API")

app.include_router(uploads_router)


@app.get("/")
async def root():
    return {"status": "ok", "service": "backend-api"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
