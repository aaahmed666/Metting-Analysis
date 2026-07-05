"""
Module: Manager Pydantic Models
Purpose: Request/response schemas for all manager-scoped endpoints.
         Keeps validation logic out of the route layer and gives the
         frontend a stable contract to code against.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, field_validator

# ─────────────────────────────────────────────────────────────────────────────
# Shared / primitives
# ─────────────────────────────────────────────────────────────────────────────

ALLOWED_ROLES = {"sales_rep", "manager", "admin"}


# ─────────────────────────────────────────────────────────────────────────────
# Team management
# ─────────────────────────────────────────────────────────────────────────────

class TeamMemberOut(BaseModel):
    """A single team member as returned to the manager."""
    user_id:    str
    full_name:  str
    email:      str
    role:       str
    is_active:  bool
    team_id:    Optional[str] = None
    created_at: Optional[str] = None


class TeamOut(BaseModel):
    """Basic team metadata."""
    team_id:    str
    name:       str
    org_id:     str
    manager_id: Optional[str] = None
    created_at: Optional[str] = None


class TeamCreateRequest(BaseModel):
    """Body for POST /manager/teams — create a new team."""
    name: str

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Team name cannot be empty.")
        if len(v) > 100:
            raise ValueError("Team name must be 100 characters or fewer.")
        return v


class TeamUpdateRequest(BaseModel):
    """Body for PATCH /manager/teams/{team_id} — rename a team."""
    name: str

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Team name cannot be empty.")
        if len(v) > 100:
            raise ValueError("Team name must be 100 characters or fewer.")
        return v


class AssignMemberRequest(BaseModel):
    """Body for POST /manager/teams/{team_id}/members — add a user to a team."""
    user_id: str


class RemoveMemberRequest(BaseModel):
    """Body for DELETE /manager/teams/{team_id}/members/{user_id} — no body needed,
    kept here for future extension (e.g. reason field)."""
    pass


# Remove completed successfully
