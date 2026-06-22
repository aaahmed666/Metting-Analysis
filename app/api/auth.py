import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from supabase import AuthApiError, Client

from app.core.dependencies import get_current_user, get_supabase_admin_client, get_supabase_client
from app.models.auth_models import (
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    UpdatePasswordRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _build_user_data(user, session=None) -> dict:
    data = {
        "user_id":   str(user.id),
        "email":     user.email,
        "full_name": (user.user_metadata or {}).get("full_name", ""),
        "role":      (user.user_metadata or {}).get("role", "sales_rep"),
    }
    if session:
        data["access_token"]  = session.access_token
        data["refresh_token"] = session.refresh_token
        data["token_type"]    = "bearer"
        data["expires_in"]    = session.expires_in
    return data


@router.post("/register", status_code=status.HTTP_201_CREATED, summary="Register a new user")
async def register(
    request: RegisterRequest,
    supabase_admin: Client = Depends(get_supabase_admin_client),
):
    # Step 1: Create user in Supabase Auth
    try:
        response = supabase_admin.auth.admin.create_user({
            "email":         request.email,
            "password":      request.password,
            "email_confirm": True,
            "user_metadata": {
                "full_name": request.full_name,
                "role":      request.role,
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
        db_record = {
            "id":        str(user.id),
            "org_id":    request.org_id,
            "full_name": request.full_name,
            "email":     request.email,
            "role":      request.role,
            "is_active": True,
        }

        if request.team_id:
            db_record["team_id"] = request.team_id

        supabase_admin.table("Users").insert(db_record).execute()
        logger.info(f"User inserted into public.Users: {user.email} | role: {request.role}")

    except Exception as e:
        # Rollback: delete the auth user if DB insert fails
        logger.error(f"DB insert failed, rolling back auth user: {e}")
        supabase_admin.auth.admin.delete_user(str(user.id))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Registration failed: could not save user to database."
        )

    return JSONResponse(status_code=status.HTTP_201_CREATED, content={
        "success": True,
        "message": "User registered successfully.",
        "user":    _build_user_data(user),
    })


@router.post("/login", summary="Login and get JWT tokens")
async def login(
    request: LoginRequest,
    supabase: Client = Depends(get_supabase_client),
):
    try:
        response = supabase.auth.sign_in_with_password({
            "email":    request.email,
            "password": request.password,
        })

        if not response.session or not response.user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials.")

        logger.info(f"User logged in: {response.user.email}")

        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True,
            "message": "Login successful.",
            "user":    _build_user_data(response.user, response.session),
        })

    except AuthApiError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=e.message)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Login failed.")


@router.post("/logout", summary="Logout")
async def logout(
    current_user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    try:
        supabase.auth.sign_out()
        logger.info(f"User logged out: {current_user.get('email')}")
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True,
            "message": "Logged out successfully.",
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
            "success": True,
            "user":    _build_user_data(user),
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"GET /me error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch profile.")


@router.post("/forgot-password", summary="Send password reset email")
async def forgot_password(
    request: ForgotPasswordRequest,
    supabase: Client = Depends(get_supabase_client),
):
    try:
        if request.redirect_to:
            supabase.auth.reset_password_for_email(
                request.email,
                {"redirect_to": request.redirect_to}
            )
        else:
            supabase.auth.reset_password_for_email(request.email)

        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True,
            "message": "Password reset email sent.",
        })

    except AuthApiError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.message)
    except Exception as e:
        logger.error(f"Forgot password error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to send reset email.")


@router.patch("/update-password", summary="Update password from settings")
async def update_password(
    request: UpdatePasswordRequest,
    current_user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    if request.new_password != request.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password and confirmation do not match."
        )

    # Verify current password by signing in
    try:
        supabase.auth.sign_in_with_password({
            "email":    current_user["email"],
            "password": request.current_password,
        })
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect."
        )

    # Update to new password
    try:
        supabase.auth.update_user({"password": request.new_password})
        logger.info(f"Password updated: {current_user.get('email')}")
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True,
            "message": "Password updated successfully.",
        })

    except AuthApiError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.message)
    except Exception as e:
        logger.error(f"Update password error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update password.")
