from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional

ALLOWED_ROLES = {"sales_rep", "manager", "admin"}


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    """
    Two supported flows:

    * Self-signup  -> caller omits `org_id` but supplies `organization_name`;
                      the backend creates a new Organization and makes this
                      user its first `admin` (role is forced to admin).
    * Invite-based -> caller supplies `invite_token`; org_id / role / team_id
                      are taken from the invite, NOT from the request body.
    * Direct       -> caller supplies an existing `org_id` (admin tooling).
    """
    email: EmailStr
    password: str
    full_name: str
    role: str = "sales_rep"
    org_id: Optional[str] = None
    team_id: Optional[str] = None

    # self-signup
    organization_name: Optional[str] = None
    # invite-based
    invite_token: Optional[str] = None

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v

    @field_validator("role")
    @classmethod
    def role_must_be_valid(cls, v: str) -> str:
        if v not in ALLOWED_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(ALLOWED_ROLES))}")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr
    redirect_to: Optional[str] = None


class UpdatePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str

    @field_validator("new_password")
    @classmethod
    def new_password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v


# ---------------------------------------------------------------------------
# NEW: Reset password (token-based, completes the forgot-password loop)
# ---------------------------------------------------------------------------
class ResetPasswordRequest(BaseModel):
    """
    Supabase delivers a recovery link that lands on the frontend with an
    access token in the URL fragment. The frontend posts that token here
    together with the new password.
    """
    access_token: str
    new_password: str
    confirm_password: str

    @field_validator("new_password")
    @classmethod
    def new_password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v


# ---------------------------------------------------------------------------
# NEW: Verify email
# ---------------------------------------------------------------------------
class VerifyEmailRequest(BaseModel):
    """
    token_hash + type from the Supabase confirmation email (verifyOtp flow).
    """
    token_hash: str
    type: str = "email"


class ResendVerificationRequest(BaseModel):
    email: EmailStr
    redirect_to: Optional[str] = None


# ---------------------------------------------------------------------------
# NEW: Two-Factor (email code)
# ---------------------------------------------------------------------------
class TwoFactorVerifyRequest(BaseModel):
    challenge_id: str
    code: str

    @field_validator("code")
    @classmethod
    def code_is_six_digits(cls, v: str) -> str:
        if len(v) != 6 or not v.isdigit():
            raise ValueError("Code must be 6 digits.")
        return v


class TwoFactorResendRequest(BaseModel):
    challenge_id: str


# ---------------------------------------------------------------------------
# NEW: Google OAuth
# ---------------------------------------------------------------------------
class GoogleStartRequest(BaseModel):
    redirect_to: Optional[str] = None


class GoogleCallbackRequest(BaseModel):
    """
    The frontend completes the OAuth code exchange via Supabase and forwards
    the resulting tokens here so the backend can ensure a public.Users row
    exists (first Google login = provisioning).
    """
    access_token: str
    refresh_token: str
    # self-signup org for first-time Google users (optional)
    organization_name: Optional[str] = None
    org_id: Optional[str] = None
    invite_token: Optional[str] = None


# ---------------------------------------------------------------------------
# NEW: Invites
# ---------------------------------------------------------------------------
class CreateInviteRequest(BaseModel):
    email: EmailStr
    role: str = "sales_rep"
    team_id: Optional[str] = None

    @field_validator("role")
    @classmethod
    def role_must_be_valid(cls, v: str) -> str:
        if v not in ALLOWED_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(ALLOWED_ROLES))}")
        return v


# ---------------------------------------------------------------------------
# Responses (unchanged shape — tokens nested in `user`)
# ---------------------------------------------------------------------------
class UserResponse(BaseModel):
    user_id: str
    email: str
    full_name: str
    role: str


class AuthResponse(BaseModel):
    user_id: str
    email: str
    full_name: str
    role: str
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: Optional[int] = None
