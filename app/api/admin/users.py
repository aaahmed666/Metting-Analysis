from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from supabase import Client

from app.core.dependencies import get_supabase_admin_client
from app.core.rbac import require_admin
from app.repositories.admin_repository import AdminRepository
from app.services.report_service import ReportService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin – Employee Reports"])


@router.get("/users")
async def list_users(
    current_user: dict = Depends(require_admin),
    supabase: Client = Depends(get_supabase_admin_client),
):
    repo   = AdminRepository(supabase)
    org_id = repo.get_org_id_for_admin(current_user["user_id"])
    users  = repo.list_org_users(org_id)

    logger.info(
        "Admin %s listed %d users in org %s",
        current_user["email"], len(users), org_id,
    )

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "total":   len(users),
        "users":   users,
    })


@router.get("/users/{user_id}")
async def get_user(
    user_id: str,
    current_user: dict = Depends(require_admin),
    supabase: Client = Depends(get_supabase_admin_client),
):
    repo   = AdminRepository(supabase)
    org_id = repo.get_org_id_for_admin(current_user["user_id"])

    service = ReportService(supabase)
    summary = service.get_user_summary(user_id, org_id)

    logger.info(
        "Admin %s viewed profile of user %s",
        current_user["email"], user_id,
    )

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        **summary,
    })


@router.get("/users/{user_id}/report")
async def get_user_report(
    user_id: str,
    current_user: dict = Depends(require_admin),
    supabase: Client = Depends(get_supabase_admin_client),
):
    repo   = AdminRepository(supabase)
    org_id = repo.get_org_id_for_admin(current_user["user_id"])

    service = ReportService(supabase)

    logger.info(
        "Admin %s requested AI report for user %s",
        current_user["email"], user_id,
    )

    result = service.generate_employee_report(user_id=user_id, org_id=org_id)

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        **result,
    })
