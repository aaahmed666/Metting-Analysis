from fastapi import APIRouter
from app.api.manager.team import router as team_router

manager_router = APIRouter()
manager_router.include_router(team_router)
