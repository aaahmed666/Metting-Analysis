import logging
from typing import Callable

from fastapi import Depends, HTTPException, status

from app.core.dependencies import get_current_user

logger = logging.getLogger(__name__)


def require_roles(*allowed_roles: str) -> Callable:
    async def _guard(current_user: dict = Depends(get_current_user)) -> dict:
        user_role = current_user.get("role", "")

        if user_role not in allowed_roles:
            logger.warning(
                "Access denied: %s (role=%s) — required: %s",
                current_user.get("email"),
                user_role,
                ", ".join(allowed_roles),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required role(s): {', '.join(allowed_roles)}.",
            )

        return current_user

    return _guard


require_authenticated = require_roles("sales_rep", "manager", "admin")
require_manager = require_roles("manager", "admin")
require_admin = require_roles("admin")
