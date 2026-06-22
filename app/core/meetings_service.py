"""
Module: Meetings domain service
Purpose: DB access for the Meetings table plus serializers that convert rows
         into the exact shapes the frontend expects (camelCase, with the rich
         JSONB payloads passed through). Keeps the route layer thin.
"""
from __future__ import annotations

import logging
from typing import Any

from supabase import Client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serializers (DB row -> frontend domain object)
# ---------------------------------------------------------------------------
def to_meeting(row: dict) -> dict:
    """Upload-centric `Meeting` shape (meetings list + upload queue)."""
    return {
        "id": row["id"],
        "title": row.get("title") or "Untitled meeting",
        "fileName": row.get("file_name") or "",
        "size": row.get("size_bytes") or 0,
        "status": row.get("status") or "uploaded",
        "progress": row.get("progress") or 0,
        "durationMinutes": row.get("duration_minutes"),
        "estimatedMinutesLeft": row.get("estimated_minutes_left"),
        "uploadedAt": row.get("uploaded_at"),
        "score": row.get("score"),
    }


def to_meeting_analysis(row: dict) -> dict:
    """`MeetingAnalysis` row for the directory table."""
    return {
        "id": row["id"],
        "title": row.get("title") or "Untitled meeting",
        "company": row.get("company") or "",
        "rep": _rep_stub(row),
        "date": row.get("uploaded_at"),
        "durationMinutes": row.get("duration_minutes") or 0,
        "status": row.get("status") or "uploaded",
        "score": row.get("score"),
        "sentiment": row.get("sentiment"),
        "insights": row.get("insights") or [],
        "dealValue": row.get("deal_value"),
    }


def to_meeting_detail(row: dict) -> dict:
    """
    `MeetingDetail` — extends MeetingAnalysis with rich fields. The rich data
    lives in `detail_data` (JSONB); until the pipeline fills it we return the
    analysis fields plus safe empty defaults so the UI renders a "processing"
    state instead of crashing.
    """
    base = to_meeting_analysis(row)
    detail = row.get("detail_data") or {}
    defaults = {
        "recordingAvailable": False,
        "summary": "",
        "summaryTags": [],
        "propensityLabel": "",
        "participants": [],
        "sentimentTimeline": [],
        "competitors": [],
        "highlights": [],
        "nextSteps": [],
        "transcript": [],
        "transcriptInsights": None,
        "urgency": None,
        "crmProvider": None,
    }
    return {**base, **defaults, **detail}


def to_deep_dive(row: dict) -> dict | None:
    """`MeetingDeepDive` — returns None if not yet computed."""
    data = row.get("deepdive_data")
    if not data:
        return None
    # Ensure id/title/date are present even if the payload omitted them.
    data.setdefault("id", row["id"])
    data.setdefault("title", row.get("title") or "")
    data.setdefault("company", row.get("company") or "")
    data.setdefault("date", row.get("uploaded_at"))
    return data


def to_sales_score(row: dict) -> dict | None:
    """`SalesScore` — returns None if not yet computed."""
    data = row.get("scoring_data")
    if not data:
        return None
    data.setdefault("id", row["id"])
    data.setdefault("meetingId", row["id"])
    data.setdefault("meetingTitle", row.get("title") or "")
    data.setdefault("date", row.get("uploaded_at"))
    return data


def _rep_stub(row: dict) -> dict:
    owner = row.get("owner_id") or ""
    return {"id": owner, "name": row.get("_owner_name") or "—"}


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def create_meeting(admin: Client, *, org_id: str | None, owner_id: str, payload: dict) -> dict:
    record = {
        "org_id": org_id,
        "owner_id": owner_id,
        "title": payload.get("title") or payload.get("file_name") or "Untitled meeting",
        "file_name": payload.get("file_name"),
        "file_id": payload.get("file_id"),
        "file_url": payload.get("file_url"),
        "size_bytes": payload.get("size") or 0,
        "mime_type": payload.get("mime_type"),
        "status": "uploaded",
        "progress": 0,
    }
    res = admin.table("Meetings").insert(record).execute()
    return res.data[0]


def get_meeting(admin: Client, meeting_id: str, *, org_id: str | None = None) -> dict | None:
    q = admin.table("Meetings").select("*").eq("id", meeting_id)
    if org_id:
        q = q.eq("org_id", org_id)
    res = q.limit(1).execute()
    return res.data[0] if res.data else None


def list_meetings(admin: Client, *, org_id: str | None, owner_id: str | None = None) -> list[dict]:
    q = admin.table("Meetings").select("*")
    if org_id:
        q = q.eq("org_id", org_id)
    if owner_id:
        q = q.eq("owner_id", owner_id)
    res = q.order("uploaded_at", desc=True).execute()
    return res.data or []


def list_analyses(
    admin: Client,
    *,
    org_id: str | None,
    search: str | None = None,
    status: str | None = None,
    sentiment: str | None = None,
    sort_by: str = "date",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[dict], int]:
    """Returns (rows, total). Filtering/sorting done in the query where possible."""
    # Map frontend sort fields -> DB columns.
    sort_map = {
        "title": "title",
        "company": "company",
        "date": "uploaded_at",
        "durationMinutes": "duration_minutes",
        "status": "status",
        "score": "score",
        "rep": "owner_id",
    }
    sort_col = sort_map.get(sort_by, "uploaded_at")

    q = admin.table("Meetings").select("*", count="exact")
    if org_id:
        q = q.eq("org_id", org_id)
    if status and status != "all":
        q = q.eq("status", status)
    if sentiment and sentiment != "all":
        q = q.eq("sentiment", sentiment)
    if search:
        # ilike on title OR company.
        q = q.or_(f"title.ilike.%{search}%,company.ilike.%{search}%")

    q = q.order(sort_col, desc=(sort_dir == "desc"))

    start = (page - 1) * page_size
    end = start + page_size - 1
    res = q.range(start, end).execute()
    total = res.count if res.count is not None else len(res.data or [])
    return (res.data or [], total)


def set_status(admin: Client, meeting_id: str, status: str, *, progress: int | None = None) -> dict | None:
    update: dict[str, Any] = {"status": status}
    if progress is not None:
        update["progress"] = progress
    if status == "failed":
        update["error_message"] = None  # clear on retry path; set elsewhere
    res = admin.table("Meetings").update(update).eq("id", meeting_id).execute()
    return res.data[0] if res.data else None
