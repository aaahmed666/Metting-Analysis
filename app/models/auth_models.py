from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, Literal


ALLOWED_ROLES = {"sales_rep", "manager", "admin"}


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):

    email: EmailStr
    password: str
    full_name: str
    role: str = "sales_rep"
    org_id: str
    team_id: Optional[str] = None

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


class OrganizationRegisterRequest(BaseModel):
    org_name: str
    industry_context: Optional[Literal["restaurants", "clinics", "retail", "other"]] = None
    manager_name: str
    manager_email: EmailStr
    manager_password: str

    @field_validator("manager_password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v


class InviteUserRequest(BaseModel):
    email: EmailStr
    role: str
    team_id: Optional[str] = None

    @field_validator("role")
    @classmethod
    def role_must_be_valid(cls, v: str) -> str:
        allowed = {"admin", "sales_rep"}
        if v not in allowed:
            raise ValueError(f"Role must be one of: {', '.join(sorted(allowed))}")
        return v


class AcceptInviteRequest(BaseModel):
    full_name: str
    password: str

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v


class AdminCreateUserRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    role: str = "sales_rep"
    team_id: Optional[str] = None

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


class AdminUpdateUserRequest(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    team_id: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def role_must_be_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ALLOWED_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(ALLOWED_ROLES))}")
        return v


class AdminResetPasswordRequest(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def new_password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v


