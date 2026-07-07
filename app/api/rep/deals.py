from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from supabase import Client

from app.core.dependencies import get_current_user, get_supabase_admin_client
from app.models.deal_models import DealStageUpdateRequest
from app.repositories.deal_repository import DealRepository, DealRepositoryError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rep/deals", tags=["Rep – Deals"])


async def _require_sales_rep(current_user: dict = Depends(get_current_user)) -> dict:
    role = current_user.get("role", "")
    if role not in {"sales_rep", "manager", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only sales representatives can access deal endpoints.",
        )
    return current_user


@router.get("")
async def list_deals(
    current_user: dict = Depends(_require_sales_rep),
    supabase: Client = Depends(get_supabase_admin_client),
):
    user_id = current_user["user_id"]
    repo = DealRepository(supabase)
    try:
        deals = repo.list_user_deals(user_id)
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True,
            "total": len(deals),
            "deals": deals,
        })
    except DealRepositoryError as exc:
        logger.error("list_deals: failed for user=%s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve deals.",
        )


@router.patch("/{deal_id}/stage")
async def update_stage(
    deal_id: str,
    body: DealStageUpdateRequest,
    current_user: dict = Depends(_require_sales_rep),
    supabase: Client = Depends(get_supabase_admin_client),
):
    user_id = current_user["user_id"]
    repo = DealRepository(supabase)
    try:
        updated = repo.update_deal_stage(deal_id, user_id, body.stage)
    except DealRepositoryError as exc:
        logger.error("update_stage: failed for deal_id=%s user=%s: %s", deal_id, user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update deal stage.",
        )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deal not found or you do not have access to it.",
        )

    logger.info(
        "User %s updated deal %s stage to '%s'",
        user_id, deal_id, body.stage
    )

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "message": "Deal stage updated successfully.",
        "deal": updated,
    })
