from fastapi import APIRouter
from app.api.manager.team import router as team_router
from app.api.manager.dashboard import router as dashboard_router

manager_router = APIRouter()
manager_router.include_router(team_router)
manager_router.include_router(dashboard_router)
