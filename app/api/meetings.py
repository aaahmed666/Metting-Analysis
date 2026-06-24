"""
Module: Meetings API
Purpose: Endpoints backing the frontend meeting/transcript/analysis/scoring
         screens. Responses are wrapped in `{ "data": ... }` to match the
         frontend meeting/scoring service contract.

Status model (no AI yet): a meeting is created as `uploaded`. The rich
payloads (detail_data / deepdive_data / scoring_data) stay null until the AI
pipeline fills them, at which point these same endpoints return full data.
"""
from __future__ import annotations

import logging
import math

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import Client

from app.core.dependencies import get_current_user, get_supabase_admin_client
from app.core import meetings_service as ms

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["Meetings"])


def _data(payload) -> JSONResponse:
    """Wrap in the `{ data: ... }` envelope the frontend service expects."""
    return JSONResponse(status_code=status.HTTP_200_OK, content={"data": payload})


def _resolve_org(admin: Client, user_id: str) -> str | None:
    res = admin.table("Users").select("org_id").eq("id", user_id).limit(1).execute()
    if res.data and res.data[0].get("org_id"):
        return res.data[0]["org_id"]
    return None


# ---------------------------------------------------------------------------
# Create upload (called after the file is stored via POST /upload)
# ---------------------------------------------------------------------------
class CreateUploadBody(BaseModel):
    fileName: str
    size: int
    mimeType: str
    # Optional linkage to the stored object from /upload:
    fileId: str | None = None
    fileUrl: str | None = None
    title: str | None = None


@router.post("/uploads", status_code=status.HTTP_201_CREATED, summary="Register an uploaded meeting")
async def create_upload(
    body: CreateUploadBody,
    current_user: dict = Depends(get_current_user),
    admin: Client = Depends(get_supabase_admin_client),
):
    org_id = _resolve_org(admin, current_user["user_id"])
    row = ms.create_meeting(
        admin,
        org_id=org_id,
        owner_id=current_user["user_id"],
        payload={
            "file_name": body.fileName,
            "size": body.size,
            "mime_type": body.mimeType,
            "file_id": body.fileId,
            "file_url": body.fileUrl,
            "title": body.title,
        },
    )
    # Frontend CreateUploadResult: { meetingId, status }
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"data": {"meetingId": row["id"], "status": row["status"]}},
    )


# ---------------------------------------------------------------------------
# List meetings (upload queue / recent)
# ---------------------------------------------------------------------------
@router.get("", summary="List meetings for the current org")
async def list_meetings(
    current_user: dict = Depends(get_current_user),
    admin: Client = Depends(get_supabase_admin_client),
):
    org_id = _resolve_org(admin, current_user["user_id"])
    rows = ms.list_meetings(admin, org_id=org_id)
    return _data([ms.to_meeting(r) for r in rows])


# ---------------------------------------------------------------------------
# Retry (re-queue a failed meeting)
# ---------------------------------------------------------------------------
@router.post("/{meeting_id}/retry", summary="Retry processing a meeting")
async def retry_upload(
    meeting_id: str,
    current_user: dict = Depends(get_current_user),
    admin: Client = Depends(get_supabase_admin_client),
):
    org_id = _resolve_org(admin, current_user["user_id"])
    row = ms.get_meeting(admin, meeting_id, org_id=org_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found.")
    updated = ms.set_status(admin, meeting_id, "processing", progress=0)
    return _data(ms.to_meeting(updated or row))


# ---------------------------------------------------------------------------
# Analyses directory (paginated + filtered)
# ---------------------------------------------------------------------------
@router.get("/analyses", summary="Paginated analysis directory")
async def list_analyses(
    search: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    sentiment: str | None = Query(None),
    sortBy: str = Query("date"),
    sortDir: str = Query("desc"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(10, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    admin: Client = Depends(get_supabase_admin_client),
):
    org_id = _resolve_org(admin, current_user["user_id"])
    rows, total = ms.list_analyses(
        admin,
        org_id=org_id,
        search=search,
        status=status_filter,
        sentiment=sentiment,
        sort_by=sortBy,
        sort_dir=sortDir,
        page=page,
        page_size=pageSize,
    )
    # Frontend `Paginated<T>` shape.
    return _data({
        "items": [ms.to_meeting_analysis(r) for r in rows],
        "total": total,
        "page": page,
        "pageSize": pageSize,
        "totalPages": max(1, math.ceil(total / pageSize)) if total else 1,
    })


# ---------------------------------------------------------------------------
# Meeting detail (full payload incl. transcript)
# ---------------------------------------------------------------------------
@router.get("/{meeting_id}/analysis", summary="Full meeting detail")
async def get_analysis(
    meeting_id: str,
    current_user: dict = Depends(get_current_user),
    admin: Client = Depends(get_supabase_admin_client),
):
    org_id = _resolve_org(admin, current_user["user_id"])
    row = ms.get_meeting(admin, meeting_id, org_id=org_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found.")
    return _data(ms.to_meeting_detail(row))


# ---------------------------------------------------------------------------
# Deep dive
# ---------------------------------------------------------------------------
@router.get("/{meeting_id}/deep-dive", summary="AI deep-dive analysis")
async def get_deep_dive(
    meeting_id: str,
    current_user: dict = Depends(get_current_user),
    admin: Client = Depends(get_supabase_admin_client),
):
    org_id = _resolve_org(admin, current_user["user_id"])
    row = ms.get_meeting(admin, meeting_id, org_id=org_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found.")
    data = ms.to_deep_dive(row)
    if data is None:
        # Not yet analyzed — 409 so the UI can show a "still processing" state.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deep-dive analysis is not ready yet.",
        )
    return _data(data)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
@router.get("/{meeting_id}/scoring", summary="Sales scoring for a meeting")
async def get_scoring(
    meeting_id: str,
    current_user: dict = Depends(get_current_user),
    admin: Client = Depends(get_supabase_admin_client),
):
    org_id = _resolve_org(admin, current_user["user_id"])
    row = ms.get_meeting(admin, meeting_id, org_id=org_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found.")
    data = ms.to_sales_score(row)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Scoring is not ready yet.",
        )
    return _data(data)
