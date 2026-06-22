from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional

ALLOWED_ROLES = {"sales_rep", "manager", "admin"}


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    role: str = "sales_rep"
    org_id: str           # UUID of the Organization this user belongs to
    team_id: Optional[str] = None  # optional — assign to team later

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
    redirect_to: Optional[str] = None  # URL to redirect after reset (Next.js page)


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
