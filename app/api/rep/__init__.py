"""Rep API package — endpoints available to sales representatives."""
from fastapi import APIRouter
from app.api.rep.meetings import router as meetings_router
from app.api.rep.deals import router as deals_router

rep_router = APIRouter()
rep_router.include_router(meetings_router)
rep_router.include_router(deals_router)
