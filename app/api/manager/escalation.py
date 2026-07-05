"""
Module: Manager – Escalation Routes
Purpose: Exposes endpoints for the manager to view, trigger, and resolve
         team escalation alerts.

Endpoints
─────────
GET   /manager/escalations              → list all active alerts for the team
POST  /manager/escalations/evaluate     → run escalation evaluation now
PATCH /manager/escalations/{id}/resolve → mark an alert as resolved

Access control
──────────────
All endpoints use require_manager → both "manager" and "admin" roles are allowed.
- Admin scope  : evaluates / views alerts for the entire organisation.
- Manager scope: evaluates / views alerts for their own team only.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from supabase import Client

from app.core.dependencies import get_supabase_admin_client
from app.core.rbac import require_manager
from app.repositories.manager_repository import ManagerRepository
from app.services.escalation_service import EscalationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manager", tags=["Manager – Escalations"])


# ─────────────────────────────────────────────────────────────────────────────
# GET /manager/escalations
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/escalations", summary="List active escalation alerts for the team")
async def list_escalations(
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Returns all unresolved escalation alerts for the manager's team.

    Each alert includes:
    - level       : yellow / orange / red
    - reason      
    - rep name and email 
    - created_at  
    """
    repo      = ManagerRepository(supabase)
    org_id    = repo.get_caller_org(current_user["user_id"])
    member_ids = repo.resolve_member_ids(
        user_id=current_user["user_id"],
        role=current_user["role"],
        org_id=org_id,
    )

    service = EscalationService(supabase)
    alerts  = service.get_active_alerts(member_ids)

    logger.info(
        "Manager %s fetched %d escalation alerts",
        current_user["email"],
        len(alerts),
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"success": True, "total": len(alerts), "alerts": alerts},
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /manager/escalations/evaluate
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/escalations/evaluate", summary="Run escalation evaluation for the team")
async def evaluate_escalations(
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Runs the full escalation evaluation for all team members immediately.
    New alerts are saved to the DB and email notifications are sent.

    Escalation levels:
    - yellow : no meetings in 7 days OR latest score < 60  → email rep
    - orange : score drop ≥ 15 pts OR ≥ 2 losses in 30 days → email rep + manager
    - red    : 3 consecutive losses OR SLA breach  → dashboard alert (no email)

    Already-open alerts at the same level are skipped (no duplicates).
    """
    repo      = ManagerRepository(supabase)
    org_id    = repo.get_caller_org(current_user["user_id"])
    member_ids = repo.resolve_member_ids(
        user_id=current_user["user_id"],
        role=current_user["role"],
        org_id=org_id,
    )

    service = EscalationService(supabase)
    result  = service.evaluate_team(
        member_ids=member_ids,
        org_id=org_id,
        manager_email=current_user["email"],
    )

    logger.info(
        "Manager %s triggered evaluation — created: %d, skipped: %d",
        current_user["email"],
        result["created"],
        result["skipped"],
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"success": True, **result},
    )


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /manager/escalations/{escalation_id}/resolve
# ─────────────────────────────────────────────────────────────────────────────

@router.patch(
    "/escalations/{escalation_id}/resolve",
    summary="Mark an escalation alert as resolved",
)
async def resolve_escalation(
    escalation_id: str,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Marks the specified escalation as resolved.
    The alert will no longer appear in the active alerts list.
    """
    service = EscalationService(supabase)
    updated = service.resolve_alert(escalation_id)

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Escalation not found.",
        )

    logger.info(
        "Manager %s resolved escalation %s",
        current_user["email"],
        escalation_id,
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            "message": "Escalation resolved successfully.",
            "escalation": updated,
        },
    )
