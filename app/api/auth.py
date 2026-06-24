import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from supabase import AuthApiError, Client

from app.core.dependencies import (
    get_current_user,
    get_supabase_admin_client,
    get_supabase_client,
)
from app.core.rbac import require_manager
from app.core import auth_services as svc
from app.models.auth_models import (
    CreateInviteRequest,
    ForgotPasswordRequest,
    GoogleCallbackRequest,
    GoogleStartRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    TwoFactorResendRequest,
    TwoFactorVerifyRequest,
    UpdatePasswordRequest,
    VerifyEmailRequest,
)
from config.setting import get_settings
from services.integrations.email_sender import (
    send_invite_email,
    send_two_factor_code,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _build_user_data(user, session=None) -> dict:
    data = {
        "user_id":   str(user.id),
        "email":     user.email,
        "full_name": (user.user_metadata or {}).get("full_name", ""),
        "role":      (user.user_metadata or {}).get("role", "sales_rep"),
        "two_factor_enabled": (user.user_metadata or {}).get("two_factor_enabled", False),
    }
    if session:
        data["access_token"]  = session.access_token
        data["refresh_token"] = session.refresh_token
        data["token_type"]    = "bearer"
        data["expires_in"]    = session.expires_in
    return data


def _provision_user_row(
    supabase_admin: Client,
    *,
    user,
    full_name: str,
    role: str,
    org_id: str,
    team_id: str | None,
) -> None:
    """Insert into public.Users; raise on failure (caller handles rollback)."""
    db_record = {
        "id":        str(user.id),
        "org_id":    org_id,
        "full_name": full_name,
        "email":     user.email,
        "role":      role,
        "is_active": True,
    }
    if team_id:
        db_record["team_id"] = team_id
    supabase_admin.table("Users").insert(db_record).execute()


def _resolve_registration_context(
    request: RegisterRequest, supabase_admin: Client
) -> tuple[str, str, str | None, str | None]:
    """
    Determine (role, org_id, team_id, invite_id) from the chosen flow.

    Priority: invite_token > org_id (direct) > organization_name (self-signup).
    """
    # --- Invite flow ---
    if request.invite_token:
        invite = svc.get_valid_invite(supabase_admin, request.invite_token)
        if not invite:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired invite token.",
            )
        if invite["email"].lower() != request.email.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This invite was issued for a different email address.",
            )
        return invite["role"], invite["org_id"], invite.get("team_id"), invite["id"]

    # --- Direct flow (existing org id supplied) ---
    if request.org_id:
        if not svc.organization_exists(supabase_admin, request.org_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Organization not found.",
            )
        return request.role, request.org_id, request.team_id, None

    # --- Self-signup flow (create a new org, user becomes its admin) ---
    if request.organization_name:
        org_id = svc.create_organization(supabase_admin, request.organization_name)
        return "admin", org_id, None, None

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Provide either invite_token, org_id, or organization_name.",
    )


# ===========================================================================
# Register
# ===========================================================================
@router.post("/register", status_code=status.HTTP_201_CREATED, summary="Register a new user")
async def register(
    request: RegisterRequest,
    supabase_admin: Client = Depends(get_supabase_admin_client),
):
    role, org_id, team_id, invite_id = _resolve_registration_context(request, supabase_admin)

    # Step 1: Create user in Supabase Auth
    try:
        response = supabase_admin.auth.admin.create_user({
            "email":         request.email,
            "password":      request.password,
            "email_confirm": True,
            "user_metadata": {
                "full_name":          request.full_name,
                "role":               role,
                "two_factor_enabled": False,
            },
        })
        user = response.user
        if not user:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Registration failed.")
    except AuthApiError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.message)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Auth user creation error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Registration failed.")

    # Step 2: Insert into public.Users
    try:
        _provision_user_row(
            supabase_admin, user=user, full_name=request.full_name,
            role=role, org_id=org_id, team_id=team_id,
        )
        if invite_id:
            svc.mark_invite_accepted(supabase_admin, invite_id)
        logger.info(f"User registered: {user.email} | role: {role} | org: {org_id}")
    except Exception as e:
        logger.error(f"DB insert failed, rolling back auth user: {e}")
        supabase_admin.auth.admin.delete_user(str(user.id))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Registration failed: DB error → {str(e)}"
        )

    return JSONResponse(status_code=status.HTTP_201_CREATED, content={
        "success": True,
        "message": "User registered successfully.",
        "user":    _build_user_data(user),
    })


# ===========================================================================
# Login (with optional 2FA)
# ===========================================================================
@router.post("/login", summary="Login and get JWT tokens (or a 2FA challenge)")
async def login(
    request: LoginRequest,
    supabase: Client = Depends(get_supabase_client),
    supabase_admin: Client = Depends(get_supabase_admin_client),
):
    try:
        response = supabase.auth.sign_in_with_password({
            "email":    request.email,
            "password": request.password,
        })
        if not response.session or not response.user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials.")
    except AuthApiError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=e.message)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Login failed.")

    user = response.user
    two_factor_enabled = (user.user_metadata or {}).get("two_factor_enabled", False)

    # 2FA path: issue an email code and DO NOT return tokens yet.
    if two_factor_enabled:
        # Sign back out of the password session — verification gates the real session.
        try:
            supabase.auth.sign_out()
        except Exception:  # noqa: BLE001
            pass
        challenge_id, code = svc.issue_two_factor_challenge(
            supabase_admin, user_id=str(user.id), email=user.email
        )
        send_two_factor_code(to=user.email, code=code)
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True,
            "message": "Two-factor verification required.",
            "two_factor_required": True,
            "challenge_id": challenge_id,
            "masked_email": svc.mask_email(user.email),
        })

    logger.info(f"User logged in: {user.email}")
    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "message": "Login successful.",
        "two_factor_required": False,
        "user":    _build_user_data(user, response.session),
    })


# ===========================================================================
# Two-Factor verify / resend
# ===========================================================================
@router.post("/two-factor", summary="Verify a 2FA email code and complete login")
async def two_factor_verify(
    request: TwoFactorVerifyRequest,
    supabase: Client = Depends(get_supabase_client),
    supabase_admin: Client = Depends(get_supabase_admin_client),
):
    user_id = svc.verify_two_factor_challenge(
        supabase_admin, challenge_id=request.challenge_id, code=request.code
    )
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired verification code.",
        )

    # Mint a real session for the verified user WITHOUT the password, using the
    # admin-magiclink -> verify_otp pattern (the supported supabase-py flow).
    try:
        user_resp = supabase_admin.auth.admin.get_user_by_id(user_id)
        user = user_resp.user
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

        link_resp = supabase_admin.auth.admin.generate_link({
            "type": "magiclink",
            "email": user.email,
        })
        hashed_token = link_resp.properties.hashed_token

        verified = supabase.auth.verify_otp({
            "token_hash": hashed_token,
            "type": "magiclink",
        })
        session = verified.session
        if not session:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not complete sign-in.",
            )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"2FA session mint error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not complete sign-in.",
        )

    logger.info(f"2FA verified, user logged in: {user.email}")
    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "message": "Login successful.",
        "user": _build_user_data(user, session),
    })


@router.post("/two-factor/resend", summary="Resend a 2FA email code")
async def two_factor_resend(
    request: TwoFactorResendRequest,
    supabase_admin: Client = Depends(get_supabase_admin_client),
):
    result = svc.regenerate_two_factor_code(supabase_admin, request.challenge_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Challenge not found or already used.",
        )
    email, code = result
    send_two_factor_code(to=email, code=code)
    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "message": "A new code has been sent.",
    })


# ===========================================================================
# Logout / Refresh / Me
# ===========================================================================
@router.post("/logout", summary="Logout")
async def logout(
    current_user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    try:
        supabase.auth.sign_out()
        logger.info(f"User logged out: {current_user.get('email')}")
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True, "message": "Logged out successfully.",
        })
    except Exception as e:
        logger.error(f"Logout error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Logout failed.")


@router.post("/refresh", summary="Refresh access token")
async def refresh(
    request: RefreshRequest,
    supabase: Client = Depends(get_supabase_client),
):
    try:
        response = supabase.auth.refresh_session(request.refresh_token)
        if not response.session or not response.user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token.")
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True,
            "message": "Token refreshed.",
            "user":    _build_user_data(response.user, response.session),
        })
    except AuthApiError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=e.message)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Token refresh failed.")


@router.get("/me", summary="Get current user profile")
async def get_me(
    current_user: dict = Depends(get_current_user),
    supabase_admin: Client = Depends(get_supabase_admin_client),
):
    try:
        response = supabase_admin.auth.admin.get_user_by_id(current_user["user_id"])
        user = response.user
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True, "user": _build_user_data(user),
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"GET /me error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch profile.")


# ===========================================================================
# Forgot / Reset / Update password
# ===========================================================================
@router.post("/forgot-password", summary="Send password reset email")
async def forgot_password(
    request: ForgotPasswordRequest,
    supabase: Client = Depends(get_supabase_client),
):
    try:
        if request.redirect_to:
            supabase.auth.reset_password_for_email(request.email, {"redirect_to": request.redirect_to})
        else:
            supabase.auth.reset_password_for_email(request.email)
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True, "message": "Password reset email sent.",
        })
    except AuthApiError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.message)
    except Exception as e:
        logger.error(f"Forgot password error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to send reset email.")


@router.post("/reset-password", summary="Reset password using recovery token")
async def reset_password(
    request: ResetPasswordRequest,
    supabase: Client = Depends(get_supabase_client),
):
    if request.new_password != request.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password and confirmation do not match.",
        )
    try:
        # The recovery access_token authenticates this update.
        supabase.auth.set_session(request.access_token, request.access_token)
        supabase.auth.update_user({"password": request.new_password})
        logger.info("Password reset via recovery token.")
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True, "message": "Password has been reset.",
        })
    except AuthApiError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.message)
    except Exception as e:
        logger.error(f"Reset password error: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired reset token.")


@router.patch("/update-password", summary="Update password from settings")
async def update_password(
    request: UpdatePasswordRequest,
    current_user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    if request.new_password != request.confirm_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password and confirmation do not match.")
    try:
        supabase.auth.sign_in_with_password({
            "email": current_user["email"], "password": request.current_password,
        })
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect.")
    try:
        supabase.auth.update_user({"password": request.new_password})
        logger.info(f"Password updated: {current_user.get('email')}")
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True, "message": "Password updated successfully.",
        })
    except AuthApiError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.message)
    except Exception as e:
        logger.error(f"Update password error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update password.")


# ===========================================================================
# Verify email
# ===========================================================================
@router.post("/verify-email", summary="Verify email with confirmation token")
async def verify_email(
    request: VerifyEmailRequest,
    supabase: Client = Depends(get_supabase_client),
):
    try:
        supabase.auth.verify_otp({
            "token_hash": request.token_hash,
            "type": request.type,
        })
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True, "message": "Email verified successfully.",
        })
    except AuthApiError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.message)
    except Exception as e:
        logger.error(f"Verify email error: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired verification link.")


@router.post("/resend-verification", summary="Resend verification email")
async def resend_verification(
    request: ResendVerificationRequest,
    supabase: Client = Depends(get_supabase_client),
):
    try:
        options = {"email_redirect_to": request.redirect_to} if request.redirect_to else {}
        supabase.auth.resend({"type": "signup", "email": request.email, "options": options})
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True, "message": "Verification email sent.",
        })
    except AuthApiError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.message)
    except Exception as e:
        logger.error(f"Resend verification error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to resend verification.")


# ===========================================================================
# Google OAuth
# ===========================================================================
@router.post("/google", summary="Get Google OAuth authorization URL")
async def google_start(
    request: GoogleStartRequest,
    supabase: Client = Depends(get_supabase_client),
):
    try:
        redirect_to = request.redirect_to or f"{get_settings().FRONTEND_BASE_URL}/login"
        result = supabase.auth.sign_in_with_oauth({
            "provider": "google",
            "options": {"redirect_to": redirect_to},
        })
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True, "url": result.url,
        })
    except Exception as e:
        logger.error(f"Google OAuth start error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to start Google sign-in.")


@router.post("/google/callback", summary="Complete Google sign-in & provision user")
async def google_callback(
    request: GoogleCallbackRequest,
    supabase: Client = Depends(get_supabase_client),
    supabase_admin: Client = Depends(get_supabase_admin_client),
):
    # Establish the session from the OAuth tokens supplied by the frontend.
    try:
        session_resp = supabase.auth.set_session(request.access_token, request.refresh_token)
        user = session_resp.user
        session = session_resp.session
        if not user or not session:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google session.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Google callback session error: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google session.")

    # First Google login → ensure a public.Users row exists.
    existing = supabase_admin.table("Users").select("id").eq("id", str(user.id)).limit(1).execute()
    if not existing.data:
        # Resolve org for the brand-new Google user.
        if request.invite_token:
            invite = svc.get_valid_invite(supabase_admin, request.invite_token)
            if not invite:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired invite token.")
            role, org_id, team_id = invite["role"], invite["org_id"], invite.get("team_id")
            svc.mark_invite_accepted(supabase_admin, invite["id"])
        elif request.org_id:
            if not svc.organization_exists(supabase_admin, request.org_id):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Organization not found.")
            role, org_id, team_id = "sales_rep", request.org_id, None
        elif request.organization_name:
            org_id = svc.create_organization(supabase_admin, request.organization_name, created_by=str(user.id))
            role, team_id = "admin", None
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="New Google user requires invite_token, org_id, or organization_name.",
            )

        full_name = (user.user_metadata or {}).get("full_name") \
            or (user.user_metadata or {}).get("name") or user.email.split("@")[0]

        # Sync role into auth metadata so /me and tokens carry it.
        supabase_admin.auth.admin.update_user_by_id(str(user.id), {
            "user_metadata": {
                "full_name": full_name, "role": role, "two_factor_enabled": False,
            }
        })
        _provision_user_row(
            supabase_admin, user=user, full_name=full_name,
            role=role, org_id=org_id, team_id=team_id,
        )
        logger.info(f"Google user provisioned: {user.email} | role: {role}")
        # Refresh user to pick up metadata.
        user = supabase_admin.auth.admin.get_user_by_id(str(user.id)).user

    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "message": "Google sign-in successful.",
        "user": _build_user_data(user, session),
    })


# ===========================================================================
# Invites (manager/admin only)
# ===========================================================================
@router.post("/invites", status_code=status.HTTP_201_CREATED, summary="Invite a user to your organization")
async def create_invite(
    request: CreateInviteRequest,
    current_user: dict = Depends(require_manager),
    supabase_admin: Client = Depends(get_supabase_admin_client),
):
    # Resolve the inviter's org from public.Users.
    inviter = supabase_admin.table("Users").select("org_id").eq("id", current_user["user_id"]).limit(1).execute()
    if not inviter.data or not inviter.data[0].get("org_id"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Your account is not linked to an organization.")
    org_id = inviter.data[0]["org_id"]

    invite = svc.create_invite(
        supabase_admin,
        email=request.email, org_id=org_id, role=request.role,
        team_id=request.team_id, invited_by=current_user["user_id"],
    )
    link = svc.build_invite_link(invite["token"])
    send_invite_email(to=request.email, invite_link=link, role=request.role)

    return JSONResponse(status_code=status.HTTP_201_CREATED, content={
        "success": True,
        "message": "Invite sent.",
        "invite": {
            "id": invite["id"], "email": invite["email"],
            "role": invite["role"], "expires_at": invite["expires_at"],
            "invite_link": link,
        },
    })


@router.get("/invites/{token}", summary="Look up an invite (prefill register form)")
async def get_invite(
    token: str,
    supabase_admin: Client = Depends(get_supabase_admin_client),
):
    invite = svc.get_valid_invite(supabase_admin, token)
    if not invite:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid or expired invite.")
    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "success": True,
        "invite": {
            "email": invite["email"], "role": invite["role"],
            "org_id": invite["org_id"], "team_id": invite.get("team_id"),
        },
    })
