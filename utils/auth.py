"""
utils/auth.py — JWT + Cookie + Role helpers
يقرأ الـ JWT من HttpOnly Cookie أو Authorization header
"""
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import bcrypt
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..config import settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


# ── Password ──────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ── JWT ───────────────────────────────────────────────
def create_token(user_id: int, role: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": str(user_id), "role": role, "exp": expire},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return {}


# ── get_current_user ──────────────────────────────────
def get_current_user(
    request:      Request,
    bearer_token: Optional[str] = Depends(oauth2_scheme),
    db:           Session       = Depends(get_db),
) -> User:
    """
    يقرأ الـ JWT من:
    1. HttpOnly Cookie (access_token) — للـ Web
    2. Authorization: Bearer — للـ Mobile / Swagger
    """
    creds_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="غير مصرح — يرجى تسجيل الدخول",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token = request.cookies.get("access_token") or bearer_token

    if not token:
        raise creds_exc

    payload     = _decode_token(token)
    user_id_str = payload.get("sub")

    if not user_id_str:
        raise creds_exc

    try:
        user_id = int(user_id_str)
    except (TypeError, ValueError):
        raise creds_exc

    user = db.query(User).filter(
        User.id        == user_id,
        User.is_active == True,
    ).first()

    if not user:
        raise creds_exc

    return user


# ── Role helpers ──────────────────────────────────────
def require_role(*roles: str):
    """
    Factory — يعمل dependency بيتحقق من الـ role.
    مثال: Depends(require_role("admin", "manager"))
    """
    def _checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"هذه العملية تتطلب صلاحية: {' أو '.join(roles)}",
            )
        return current_user
    return _checker


require_manager = require_role("manager", "admin")
require_admin   = require_role("admin")
