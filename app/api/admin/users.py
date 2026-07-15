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


# ─────────────────────────────────────────────────────────────────────────────
#  User Administration
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import HTTPException
from app.models.auth_models import (
    AdminCreateUserRequest,
    AdminUpdateUserRequest,
    AdminResetPasswordRequest,
)

@router.post("/users", status_code=status.HTTP_201_CREATED, summary="Create a new employee user account")
async def create_user(
    payload: AdminCreateUserRequest,
    current_user: dict = Depends(require_admin),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Creates a new employee account (Manager or Sales Rep) in the system.
    Saves the user credentials to Supabase Auth and registers their profile in the database.
    """
    repo   = AdminRepository(supabase)
    org_id = repo.get_org_id_for_admin(current_user["user_id"])

    # 1. Create user in Supabase Auth using service key
    try:
        auth_response = supabase.auth.admin.create_user({
            "email": payload.email,
            "password": payload.password,
            "user_metadata": {
                "role": payload.role,
                "full_name": payload.full_name,
            },
            "email_confirm": True
        })
        
        auth_user = auth_response.user
        if not auth_user or not auth_user.id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Auth system did not return a valid user ID."
            )
    except Exception as exc:
        logger.error("create_user: Auth system error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create user in Auth system: {exc}"
        )

    # 2. Save user metadata to database
    try:
        db_user = repo.create_user_record(
            user_id=str(auth_user.id),
            email=payload.email,
            full_name=payload.full_name,
            role=payload.role,
            org_id=org_id,
            team_id=payload.team_id
        )
    except Exception as exc:
        # Rollback Auth user if DB insert fails
        logger.error("create_user: Database registration failed: %s. Rolling back Auth user.", exc)
        try:
            supabase.auth.admin.delete_user(str(auth_user.id))
        except Exception as rollback_exc:
            logger.error("create_user: Failed to rollback Auth user deletion: %s", rollback_exc)
            
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"User created in auth but database registration failed: {exc}"
        )

    logger.info("Admin %s created user %s (%s)", current_user["email"], db_user["id"], db_user["email"])
    return JSONResponse(status_code=status.HTTP_201_CREATED, content={
        "success": True,
        "user": db_user
    })


@router.patch("/users/{user_id}", summary="Modify employee account details")
async def update_user(
    user_id: str,
    payload: AdminUpdateUserRequest,
    current_user: dict = Depends(require_admin),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Updates the profile details (Full Name, Role, Team, active state) of an employee.
    Use this endpoint to deactivate/freeze accounts by setting 'is_active' to False.
    """
    repo   = AdminRepository(supabase)
    org_id = repo.get_org_id_for_admin(current_user["user_id"])

    # Verify user exists in admin's organization
    db_user = repo.get_user_profile(user_id, org_id)

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True,
            "message": "No changes specified."
        })

    # 1. Update Supabase Auth if metadata/role changes
    auth_updates = {}
    if "full_name" in updates:
        auth_updates["user_metadata"] = {
            **(db_user.get("user_metadata") or {}),
            "full_name": updates["full_name"]
        }
    if "role" in updates:
        if not auth_updates.get("user_metadata"):
            auth_updates["user_metadata"] = {}
        auth_updates["user_metadata"]["role"] = updates["role"]

    if auth_updates:
        try:
            supabase.auth.admin.update_user_by_id(user_id, auth_updates)
        except Exception as exc:
            logger.error("update_user: Auth system update failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to update user in auth system: {exc}"
            )

    # 2. Update Database Users table
    try:
        updated_db_user = repo.update_user_record(user_id, org_id, updates)
    except Exception as exc:
        logger.error("update_user: Database update failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database update failed: {exc}"
        )

    logger.info("Admin %s updated user %s", current_user["email"], user_id)
    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "user": updated_db_user
    })


@router.post("/users/{user_id}/reset-password", summary="Override employee account password")
async def reset_user_password(
    user_id: str,
    payload: AdminResetPasswordRequest,
    current_user: dict = Depends(require_admin),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Administratively resets/overrides the password of any employee.
    """
    repo   = AdminRepository(supabase)
    org_id = repo.get_org_id_for_admin(current_user["user_id"])

    # Verify user exists in admin's organization
    repo.get_user_profile(user_id, org_id)

    try:
        supabase.auth.admin.update_user_by_id(user_id, {
            "password": payload.new_password
        })
    except Exception as exc:
        logger.error("reset_user_password: Auth update failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to reset user password: {exc}"
        )

    logger.info("Admin %s reset password for user %s", current_user["email"], user_id)
    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "message": "User password reset successfully."
    })

