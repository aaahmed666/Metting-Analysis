"""
Module: Manager – Team Management Routes
Purpose: All team CRUD operations accessible by managers (and admins).

Endpoints
─────────
GET    /manager/teams                        → list teams the caller manages
POST   /manager/teams                        → create a new team
GET    /manager/teams/{team_id}              → get one team + its members
PATCH  /manager/teams/{team_id}              → rename a team
DELETE /manager/teams/{team_id}              → delete a team (must be empty)
GET    /manager/teams/{team_id}/members      → list members of a team
POST   /manager/teams/{team_id}/members      → assign an existing user to a team
DELETE /manager/teams/{team_id}/members/{user_id} → remove a user from a team

Access control
──────────────
• require_manager  → both "manager" and "admin" roles may call these endpoints.
• A manager can only act on teams inside their own org.
• An admin may act on any team.
"""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from supabase import Client

from app.core.dependencies import get_supabase_admin_client, get_current_user
from app.core.rbac import require_manager
from app.models.manager_models import (
    AssignMemberRequest,
    TeamCreateRequest,
    TeamUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manager/teams", tags=["Manager – Teams"])


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_caller_org(current_user: dict, supabase: Client) -> str:
    """Return the org_id of the calling user from the Users table."""
    response = (
        supabase.table("Users")
        .select("org_id")
        .eq("id", current_user["user_id"])
        .single()
        .execute()
    )
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caller not found in Users table.",
        )
    return response.data["org_id"]


def _assert_team_belongs_to_org(team_id: str, org_id: str, supabase: Client) -> dict:
    """Fetch a team and verify it belongs to the caller's org."""
    response = (
        supabase.table("Teams")
        .select("*")
        .eq("id", team_id)
        .eq("org_id", org_id)
        .single()
        .execute()
    )
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found or you do not have access to it.",
        )
    return response.data


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", summary="List teams managed by the caller")
async def list_teams(
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Returns all teams inside the caller's organisation.
    Admins see every team; managers see teams where they are manager_id.
    """
    org_id = _get_caller_org(current_user, supabase)

    query = supabase.table("Teams").select("*").eq("org_id", org_id)

    # Managers are scoped to teams they manage.
    if current_user["role"] == "manager":
        query = query.eq("manager_id", current_user["user_id"])

    response = query.order("created_at", desc=False).execute()

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "teams":   response.data or [],
        "total":   len(response.data or []),
    })


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a new team")
async def create_team(
    body: TeamCreateRequest,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Creates a team inside the caller's organisation.
    The calling manager is automatically set as manager_id.
    """
    org_id = _get_caller_org(current_user, supabase)

    # Guard: team name must be unique within the org.
    exists = (
        supabase.table("Teams")
        .select("id")
        .eq("org_id", org_id)
        .eq("name", body.name)
        .execute()
    )
    if exists.data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A team named '{body.name}' already exists in your organisation.",
        )

    record = {
        "name":       body.name,
        "org_id":     org_id,
        "manager_id": current_user["user_id"],
    }
    response = supabase.table("Teams").insert(record).execute()

    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create team.",
        )

    team = response.data[0]
    logger.info("Team created: %s by %s", team.get("id"), current_user["email"])

    return JSONResponse(status_code=status.HTTP_201_CREATED, content={
        "success": True,
        "message": "Team created successfully.",
        "team":    team,
    })


@router.get("/{team_id}", summary="Get team details + members")
async def get_team(
    team_id: str,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """Returns team metadata and a list of its members."""
    org_id = _get_caller_org(current_user, supabase)
    team   = _assert_team_belongs_to_org(team_id, org_id, supabase)

    members_response = (
        supabase.table("Users")
        .select("id, full_name, email, role, is_active, team_id, created_at")
        .eq("team_id", team_id)
        .eq("is_active", True)
        .execute()
    )

    members = [
        {
            "user_id":    m["id"],
            "full_name":  m["full_name"],
            "email":      m["email"],
            "role":       m["role"],
            "is_active":  m["is_active"],
            "team_id":    m.get("team_id"),
            "created_at": m.get("created_at"),
        }
        for m in (members_response.data or [])
    ]

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "team":    team,
        "members": members,
        "total_members": len(members),
    })


@router.patch("/{team_id}", summary="Rename a team")
async def update_team(
    team_id: str,
    body: TeamUpdateRequest,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """Updates the team name. Duplicate names within the org are rejected."""
    org_id = _get_caller_org(current_user, supabase)
    _assert_team_belongs_to_org(team_id, org_id, supabase)

    # Guard: new name must be unique within the org (excluding this team).
    exists = (
        supabase.table("Teams")
        .select("id")
        .eq("org_id", org_id)
        .eq("name", body.name)
        .neq("id", team_id)
        .execute()
    )
    if exists.data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A team named '{body.name}' already exists in your organisation.",
        )

    response = (
        supabase.table("Teams")
        .update({"name": body.name})
        .eq("id", team_id)
        .execute()
    )

    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update team.",
        )

    logger.info("Team renamed: %s → '%s' by %s", team_id, body.name, current_user["email"])

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "message": "Team updated successfully.",
        "team":    response.data[0],
    })


@router.delete("/{team_id}", summary="Delete a team")
async def delete_team(
    team_id: str,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Deletes a team. Refuses if there are still active members assigned to it.
    """
    org_id = _get_caller_org(current_user, supabase)
    _assert_team_belongs_to_org(team_id, org_id, supabase)

    # Guard: cannot delete a team that still has members.
    members = (
        supabase.table("Users")
        .select("id")
        .eq("team_id", team_id)
        .eq("is_active", True)
        .execute()
    )
    if members.data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete team: {len(members.data)} active member(s) still assigned. "
                "Remove or reassign them first."
            ),
        )

    supabase.table("Teams").delete().eq("id", team_id).execute()
    logger.info("Team deleted: %s by %s", team_id, current_user["email"])

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "message": "Team deleted successfully.",
    })


# ─────────────────────────────────────────────────────────────────────────────
# Member management inside a team
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{team_id}/members", summary="List team members")
async def list_team_members(
    team_id: str,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """Returns all active users whose team_id matches."""
    org_id = _get_caller_org(current_user, supabase)
    _assert_team_belongs_to_org(team_id, org_id, supabase)

    response = (
        supabase.table("Users")
        .select("id, full_name, email, role, is_active, team_id, created_at")
        .eq("team_id", team_id)
        .eq("is_active", True)
        .execute()
    )

    members = [
        {
            "user_id":    m["id"],
            "full_name":  m["full_name"],
            "email":      m["email"],
            "role":       m["role"],
            "is_active":  m["is_active"],
            "created_at": m.get("created_at"),
        }
        for m in (response.data or [])
    ]

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "members": members,
        "total":   len(members),
    })


@router.post("/{team_id}/members", summary="Assign an existing user to a team")
async def assign_member(
    team_id: str,
    body: AssignMemberRequest,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Assigns a pre-existing user (already in the Users table) to this team.
    The user must belong to the same org as the manager.
    """
    org_id = _get_caller_org(current_user, supabase)
    _assert_team_belongs_to_org(team_id, org_id, supabase)

    # Verify target user exists in same org and is not already in another team.
    user_resp = (
        supabase.table("Users")
        .select("id, full_name, email, role, team_id, is_active")
        .eq("id", body.user_id)
        .eq("org_id", org_id)
        .single()
        .execute()
    )
    if not user_resp.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found in your organisation.",
        )

    user = user_resp.data

    if not user["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot assign a deactivated user to a team.",
        )

    if user.get("team_id") == team_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already a member of this team.",
        )

    # Update the user's team_id.
    supabase.table("Users").update({"team_id": team_id}).eq("id", body.user_id).execute()

    logger.info(
        "User %s assigned to team %s by %s",
        body.user_id, team_id, current_user["email"],
    )

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "message": f"User '{user['full_name']}' assigned to team successfully.",
    })


@router.delete("/{team_id}/members/{user_id}", summary="Remove a user from a team")
async def remove_member(
    team_id: str,
    user_id: str,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Clears team_id on the target user (sets it to NULL).
    Does NOT deactivate or delete the user account.
    """
    org_id = _get_caller_org(current_user, supabase)
    _assert_team_belongs_to_org(team_id, org_id, supabase)

    # Make sure the user actually belongs to this team.
    user_resp = (
        supabase.table("Users")
        .select("id, full_name, team_id")
        .eq("id", user_id)
        .eq("org_id", org_id)
        .single()
        .execute()
    )
    if not user_resp.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found in your organisation.",
        )

    if user_resp.data.get("team_id") != team_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not a member of this team.",
        )

    supabase.table("Users").update({"team_id": None}).eq("id", user_id).execute()

    logger.info(
        "User %s removed from team %s by %s",
        user_id, team_id, current_user["email"],
    )

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "message": f"User '{user_resp.data['full_name']}' removed from team.",
    })
