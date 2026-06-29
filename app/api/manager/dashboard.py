"""
Module: Manager – Dashboard & Analytics Routes
Purpose: Exposes endpoints for the team manager to view team meetings,
         meeting reports, team KPIs, and the rep leaderboard.

Endpoints
─────────
GET  /manager/meetings                     → list all meetings for all team members
GET  /manager/meetings/{meeting_id}        → get full meeting details + AI report
GET  /manager/dashboard/kpis              → team-wide KPI metrics
GET  /manager/dashboard/leaderboard       → rep ranking sorted by avg score

Access control
──────────────
• require_manager → both "manager" and "admin" roles may call these endpoints.
• A manager only sees meetings belonging to their own team members.
• An admin sees all meetings in the org (all teams).

"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from supabase import Client

from app.core.rbac import require_manager
from app.core.dependencies import get_supabase_admin_client
from app.repositories.manager_repository import ManagerRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manager", tags=["Manager – Dashboard"])


# ─────────────────────────────────────────────────────────────────────────────
# Route: List Team Meetings
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/meetings",
    summary="List all meetings for all team members",
)
async def list_team_meetings(
    status_filter: Optional[str] = Query(
        default=None,
        alias="status",
        description="Filter by meeting status: pending | processing | completed | rejected",
    ),
    limit: int = Query(default=20, ge=1, le=100, description="Number of results per page"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Returns a paginated list of all meetings belonging to any member
    of the teams managed by the calling manager.

    - Each meeting entry includes basic meeting info + the rep's name/email.
    - Use `status` query param to filter (e.g. `?status=done` to see only analysed meetings).
    - Use `limit` and `offset` for pagination.

    Example: GET /api/v1/manager/meetings?status=completed&limit=10&offset=0
    """
    repo = ManagerRepository(supabase)

    org_id = repo.get_caller_org(current_user["user_id"])
    member_ids = repo.resolve_member_ids(
        user_id=current_user["user_id"],
        role=current_user["role"],
        org_id=org_id,
    )

    if not member_ids:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "total": 0,
                "limit": limit,
                "offset": offset,
                "meetings": [],
                "message": "No team members found. Please assign members to your team first.",
            },
        )

    meetings = repo.get_meetings_for_members(
        member_ids=member_ids,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    total = repo.count_meetings_for_members(member_ids=member_ids, status=status_filter)

    logger.info(
        "Manager %s fetched %d meetings (total=%d)",
        current_user["email"], len(meetings), total,
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            "total": total,
            "limit": limit,
            "offset": offset,
            "meetings": meetings,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route: Get Single Meeting + Full Report
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/meetings/{meeting_id}",
    summary="Get full meeting details + AI analysis report",
)
async def get_team_meeting(
    meeting_id: str,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Returns full details for a single meeting belonging to one of the manager's team members.
    The response includes:
    - Meeting metadata (date, duration, source, status)
    - Rep name and email
    - Full AI report (scores, grade, summary, opening script, etc.)

    Returns 404 if the meeting doesn't belong to the manager's team (security isolation).
    """
    repo = ManagerRepository(supabase)

    org_id = repo.get_caller_org(current_user["user_id"])
    member_ids = repo.resolve_member_ids(
        user_id=current_user["user_id"],
        role=current_user["role"],
        org_id=org_id,
    )

    if not member_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found or you do not have access to it.",
        )

    meeting = repo.get_meeting_with_report(meeting_id=meeting_id, member_ids=member_ids)

    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found or you do not have access to it.",
        )

    logger.info("Manager %s viewed meeting %s", current_user["email"], meeting_id)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            "meeting": meeting,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route: Team KPIs
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dashboard/kpis", summary="Team KPIs ")
async def get_team_kpis(
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Returns aggregated KPI metrics for the entire team:

    - total_meetings / completed_meetings 
    - meetings_by_status (pending / processing / completed / rejected)
    - avg_score 
    - grade_distribution (A / B / C / D)
    - avg_scores 
    - avg_talk_ratio 
    """
    repo = ManagerRepository(supabase)
    org_id = repo.get_caller_org(current_user["user_id"])
    member_ids = repo.resolve_member_ids(
        user_id=current_user["user_id"],
        role=current_user["role"],
        org_id=org_id,
    )

    kpis = repo.get_team_kpis(member_ids)

    logger.info("Manager %s fetched team KPIs", current_user["email"])

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"success": True, "kpis": kpis},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route: Leaderboard
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dashboard/leaderboard", summary="Rep Leaderboard ")
async def get_leaderboard(
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Returns a ranked list of sales reps based on their average total score.

    Each entry includes:
    - rank 
    - rep_name / rep_email 
    - total_meetings
    - avg_score
    - best_grade
    - avg_scores 
    """
    repo = ManagerRepository(supabase)
    org_id = repo.get_caller_org(current_user["user_id"])
    member_ids = repo.resolve_member_ids(
        user_id=current_user["user_id"],
        role=current_user["role"],
        org_id=org_id,
    )

    if not member_ids:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"success": True, "leaderboard": []},
        )

    #get sales_rap data
    members = repo.get_team_members_info(member_ids)
    member_map = {m["id"]: m for m in members}

    #get all completed meetings + id for sales_rap
    meetings_resp = (
        supabase.table("Meetings")
        .select("id, user_id")
        .in_("user_id", member_ids)
        .eq("status", "completed")
        .execute()
    )
    meetings = meetings_resp.data or []

    if not meetings:
        leaderboard = [
            {
                "rank": i + 1,
                "user_id": m["id"],
                "rep_name": m["full_name"],
                "rep_email": m["email"],
                "total_meetings": 0,
                "avg_score": None,
                "best_grade": None,
                "avg_scores": {},
            }
            for i, m in enumerate(members)
        ]
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"success": True, "leaderboard": leaderboard},
        )

    
    meeting_to_user: dict[str, str] = {m["id"]: m["user_id"] for m in meetings}
    completed_ids = list(meeting_to_user.keys())

    # get reports
    reports_resp = (
        supabase.table("Meeting_Reports")
        .select(
            "meeting_id, total_score, grade, "
            "discovery_score, objection_score, next_steps_score, "
            "closing_score, listening_score"
        )
        .in_("meeting_id", completed_ids)
        .execute()
    )
    reports = reports_resp.data or []


    score_fields = [
        "total_score", "discovery_score", "objection_score",
        "next_steps_score", "closing_score", "listening_score",
    ]
    grade_priority = {"A": 4, "B": 3, "C": 2, "D": 1}  

    # per_rep[user_id] = {sums, counts, grades}
    per_rep: dict[str, dict] = {}
    for uid in member_ids:
        per_rep[uid] = {
            "sums": {f: 0.0 for f in score_fields},
            "counts": {f: 0 for f in score_fields},
            "grades": [],
        }

    for r in reports:
        uid = meeting_to_user.get(r["meeting_id"])
        if not uid or uid not in per_rep:
            continue
        for field in score_fields:
            val = r.get(field)
            if val is not None:
                per_rep[uid]["sums"][field] += float(val)
                per_rep[uid]["counts"][field] += 1
        if r.get("grade"):
            per_rep[uid]["grades"].append(r["grade"])

    # built list
    def avg(uid: str, field: str) -> Optional[float]:
        c = per_rep[uid]["counts"][field]
        return round(per_rep[uid]["sums"][field] / c, 1) if c else None

    def best_grade(uid: str) -> Optional[str]:
        grades = per_rep[uid]["grades"]
        if not grades:
            return None
        return max(grades, key=lambda g: grade_priority.get(g, 0))

    entries = []
    for uid in member_ids:
        rep = member_map.get(uid)
        if not rep:
            continue
        rep_meetings = sum(
            1 for m in meetings if m["user_id"] == uid
        )
        entries.append({
            "user_id": uid,
            "rep_name": rep["full_name"],
            "rep_email": rep["email"],
            "total_meetings": rep_meetings,
            "avg_score": avg(uid, "total_score"),
            "best_grade": best_grade(uid),
            "avg_scores": {
                "discovery":  avg(uid, "discovery_score"),
                "objection":  avg(uid, "objection_score"),
                "next_steps": avg(uid, "next_steps_score"),
                "closing":    avg(uid, "closing_score"),
                "listening":  avg(uid, "listening_score"),
            },
        })

    
    entries.sort(key=lambda e: e["avg_score"] or -1, reverse=True)
    for i, entry in enumerate(entries):
        entry["rank"] = i + 1

    logger.info("Manager %s fetched leaderboard (%d reps)", current_user["email"], len(entries))

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"success": True, "leaderboard": entries},
    )
