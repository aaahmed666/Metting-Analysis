"""
Module: Auth domain services
Purpose: DB-backed helpers for Organizations, Invites, and Two-Factor email
         challenges. Keeps the route layer thin.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from supabase import Client

from config.setting import get_settings

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------
def create_organization(admin: Client, name: str, created_by: str | None = None) -> str:
    record = {"name": name}
    if created_by:
        record["created_by"] = created_by
    res = admin.table("Organizations").insert(record).execute()
    org_id = res.data[0]["id"]
    logger.info("Organization created: %s (%s)", name, org_id)
    return org_id


def organization_exists(admin: Client, org_id: str) -> bool:
    res = admin.table("Organizations").select("id").eq("id", org_id).limit(1).execute()
    return bool(res.data)


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------
def create_invite(
    admin: Client,
    *,
    email: str,
    org_id: str,
    role: str,
    team_id: str | None,
    invited_by: str | None,
) -> dict:
    token = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(hours=get_settings().INVITE_TTL_HOURS)
    record = {
        "token": token,
        "email": email,
        "org_id": org_id,
        "role": role,
        "team_id": team_id,
        "invited_by": invited_by,
        "expires_at": expires_at.isoformat(),
    }
    res = admin.table("Invites").insert(record).execute()
    return res.data[0]


def get_valid_invite(admin: Client, token: str) -> dict | None:
    res = (
        admin.table("Invites")
        .select("*")
        .eq("token", token)
        .eq("accepted", False)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    invite = res.data[0]
    expires_at = datetime.fromisoformat(invite["expires_at"])
    if expires_at < _now():
        return None
    return invite


def mark_invite_accepted(admin: Client, invite_id: str) -> None:
    admin.table("Invites").update({"accepted": True}).eq("id", invite_id).execute()


def build_invite_link(token: str) -> str:
    base = get_settings().FRONTEND_BASE_URL.rstrip("/")
    return f"{base}/register?invite={token}"


# ---------------------------------------------------------------------------
# Two-Factor email challenges
# ---------------------------------------------------------------------------
def issue_two_factor_challenge(admin: Client, *, user_id: str, email: str) -> tuple[str, str]:
    """Create a challenge row, return (challenge_id, plaintext_code)."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    ttl = get_settings().TWO_FACTOR_CODE_TTL_SECONDS
    expires_at = _now() + timedelta(seconds=ttl)
    record = {
        "user_id": user_id,
        "email": email,
        "code_hash": _hash_code(code),
        "expires_at": expires_at.isoformat(),
    }
    res = admin.table("TwoFactorChallenges").insert(record).execute()
    return res.data[0]["id"], code


def verify_two_factor_challenge(admin: Client, *, challenge_id: str, code: str) -> str | None:
    """Return user_id if the code is valid; otherwise None. Consumes on success."""
    settings = get_settings()
    res = (
        admin.table("TwoFactorChallenges")
        .select("*")
        .eq("id", challenge_id)
        .eq("consumed", False)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None

    challenge = res.data[0]

    if datetime.fromisoformat(challenge["expires_at"]) < _now():
        return None

    if challenge["attempts"] >= settings.TWO_FACTOR_MAX_ATTEMPTS:
        return None

    dev_bypass = settings.TWO_FACTOR_DEV_BYPASS and settings.ENVIRONMENT != "production"
    is_match = challenge["code_hash"] == _hash_code(code) or (dev_bypass and code == "000000")

    if not is_match:
        admin.table("TwoFactorChallenges").update(
            {"attempts": challenge["attempts"] + 1}
        ).eq("id", challenge_id).execute()
        return None

    admin.table("TwoFactorChallenges").update({"consumed": True}).eq(
        "id", challenge_id
    ).execute()
    return challenge["user_id"]


def regenerate_two_factor_code(admin: Client, challenge_id: str) -> tuple[str, str] | None:
    """Issue a fresh code for an existing, unexpired challenge."""
    res = (
        admin.table("TwoFactorChallenges")
        .select("*")
        .eq("id", challenge_id)
        .eq("consumed", False)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None

    challenge = res.data[0]
    code = f"{secrets.randbelow(1_000_000):06d}"
    ttl = get_settings().TWO_FACTOR_CODE_TTL_SECONDS
    expires_at = _now() + timedelta(seconds=ttl)
    admin.table("TwoFactorChallenges").update(
        {
            "code_hash": _hash_code(code),
            "expires_at": expires_at.isoformat(),
            "attempts": 0,
        }
    ).eq("id", challenge_id).execute()
    return challenge["email"], code


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        masked = local[0] + "*"
    else:
        masked = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked}@{domain}"
