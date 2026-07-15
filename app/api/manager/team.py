from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from supabase import Client

from app.core.dependencies import get_supabase_admin_client
from app.core.rbac import require_manager, require_admin
from app.models.manager_models import (
    AssignMemberRequest,
    TeamCreateRequest,
    TeamUpdateRequest,
)
from app.repositories.manager_repository import ManagerRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manager/teams", tags=["Manager – Teams"])


def _assert_team_belongs_to_org(team_id: str, org_id: str, supabase: Client) -> dict:
    response = (
        supabase.table("Teams")
        .select("*")
        .eq("id", team_id)
        .eq("org_id", org_id)
        .maybe_single()
        .execute()
    )
    data = response.data if response else None
    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found or you do not have access to it.",
        )
    return data


def _assert_manager_owns_team(current_user: dict, team: dict) -> None:
    if current_user["role"] == "admin":
        return

    if team.get("manager_id") != current_user["user_id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. You are not the manager of this team.",
        )


@router.get("")
async def list_teams(
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    org_id = ManagerRepository(supabase).get_caller_org(current_user["user_id"])
    query = supabase.table("Teams").select("*").eq("org_id", org_id)

    if current_user["role"] == "manager":
        query = query.eq("manager_id", current_user["user_id"])

    response = query.order("created_at", desc=False).execute()

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "teams":   response.data or [],
        "total":   len(response.data or []),
    })


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_team(
    body: TeamCreateRequest,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    org_id = ManagerRepository(supabase).get_caller_org(current_user["user_id"])

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


@router.get("/{team_id}")
async def get_team(
    team_id: str,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    org_id = ManagerRepository(supabase).get_caller_org(current_user["user_id"])
    team   = _assert_team_belongs_to_org(team_id, org_id, supabase)
    _assert_manager_owns_team(current_user, team)

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
        "success":       True,
        "team":          team,
        "members":       members,
        "total_members": len(members),
    })


@router.patch("/{team_id}")
async def update_team(
    team_id: str,
    body: TeamUpdateRequest,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    org_id = ManagerRepository(supabase).get_caller_org(current_user["user_id"])
    team   = _assert_team_belongs_to_org(team_id, org_id, supabase)
    _assert_manager_owns_team(current_user, team)

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


@router.delete("/{team_id}")
async def delete_team(
    team_id: str,
    current_user: dict = Depends(require_admin),
    supabase: Client = Depends(get_supabase_admin_client),
):
    org_id = ManagerRepository(supabase).get_caller_org(current_user["user_id"])
    _assert_team_belongs_to_org(team_id, org_id, supabase)

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
    logger.info("Team deleted: %s by admin %s", team_id, current_user["email"])

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "message": "Team deleted successfully.",
    })


@router.get("/{team_id}/members")
async def list_team_members(
    team_id: str,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    org_id = ManagerRepository(supabase).get_caller_org(current_user["user_id"])
    team   = _assert_team_belongs_to_org(team_id, org_id, supabase)
    _assert_manager_owns_team(current_user, team)

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


@router.post("/{team_id}/members")
async def assign_member(
    team_id: str,
    body: AssignMemberRequest,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    org_id = ManagerRepository(supabase).get_caller_org(current_user["user_id"])
    team   = _assert_team_belongs_to_org(team_id, org_id, supabase)
    _assert_manager_owns_team(current_user, team)

    user_resp = (
        supabase.table("Users")
        .select("id, full_name, email, role, team_id, is_active")
        .eq("id", body.user_id)
        .eq("org_id", org_id)
        .maybe_single()
        .execute()
    )
    if not (user_resp and user_resp.data):
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

    if current_user["role"] == "manager" and user["role"] in ("manager", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managers can only assign sales_rep users. "
                   "Assigning managers or admins requires admin access.",
        )

    if user.get("team_id") == team_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already a member of this team.",
        )

    supabase.table("Users").update({"team_id": team_id}).eq("id", body.user_id).execute()

    logger.info(
        "User %s assigned to team %s by %s",
        body.user_id, team_id, current_user["email"],
    )

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "message": f"User '{user['full_name']}' assigned to team successfully.",
    })


@router.delete("/{team_id}/members/{user_id}")
async def remove_member(
    team_id: str,
    user_id: str,
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    org_id = ManagerRepository(supabase).get_caller_org(current_user["user_id"])
    team   = _assert_team_belongs_to_org(team_id, org_id, supabase)
    _assert_manager_owns_team(current_user, team)

    user_resp = (
        supabase.table("Users")
        .select("id, full_name, team_id")
        .eq("id", user_id)
        .eq("org_id", org_id)
        .maybe_single()
        .execute()
    )
    if not (user_resp and user_resp.data):
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
