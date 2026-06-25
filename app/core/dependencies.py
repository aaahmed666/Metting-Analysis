import logging
from functools import lru_cache

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import Client, create_client

from config.setting import settings

logger = logging.getLogger(__name__)

oauth2_scheme = HTTPBearer(auto_error=False)


@lru_cache()
def get_supabase_admin_client() -> Client:
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)


@lru_cache()
def get_supabase_client() -> Client:
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(oauth2_scheme),
) -> dict:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    try:
        admin_client = get_supabase_admin_client()
        user_response = admin_client.auth.get_user(token)
        user = user_response.user

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        role: str = (user.user_metadata or {}).get("role", "sales_rep")
        org_id = None

        try:
            db_user_res = admin_client.table("Users").select("org_id, role").eq("id", str(user.id)).execute()
            if db_user_res.data:
                db_user = db_user_res.data[0]
                org_id = db_user.get("org_id")
                role = db_user.get("role", role)
        except Exception as db_exc:
            logger.error(f"Error fetching user profile from database: {db_exc}")

        return {
            "user_id": str(user.id),
            "email":   user.email,
            "role":    role,
            "org_id":  org_id,
            "token":   token,
        }

    except HTTPException:
        raise

    except Exception as exc:
        logger.error(f"Token validation error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
