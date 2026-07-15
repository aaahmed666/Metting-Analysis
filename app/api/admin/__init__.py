"""
Admin API package.
Aggregates all admin-scoped routers into a single admin_router
that is registered in main.py.
"""
from fastapi import APIRouter
from app.api.admin.users import router as users_router

admin_router = APIRouter()
admin_router.include_router(users_router)
