import logging
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from supabase import Client

from app.core.dependencies import get_current_user, get_supabase_admin_client
from app.models.auth_models import InviteUserRequest, AcceptInviteRequest
from app.services.invitation_service import InvitationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invitations", tags=["Invitations"])

@router.post("", status_code=status.HTTP_201_CREATED, summary="Send an invitation to a new user")
async def send_invitation(
    request: InviteUserRequest,
    current_user: dict = Depends(get_current_user),
    supabase_admin: Client = Depends(get_supabase_admin_client),
):
    """
    Allows a manager or admin user to invite others to join their organization.
    Checks RBAC constraints to ensure appropriate permissions.
    """
    invitation_service = InvitationService(supabase_admin)
    try:
        result = invitation_service.send_invitation(current_user, request)
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=result)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error sending invitation: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to send invitation.")

@router.get("/verify", summary="Verify if an invitation token is valid")
async def verify_invitation(
    token: str,
    supabase_admin: Client = Depends(get_supabase_admin_client),
):
    """
    Checks if the invitation token is valid, has not expired, and has not been used.
    """
    invitation_service = InvitationService(supabase_admin)
    try:
        invite = invitation_service.verify_invitation_token(token)
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "success": True,
            "email": invite["email"],
            "role": invite["requested_role"],
            "org_id": invite["org_id"],
            "team_id": invite.get("team_id"),
        })
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error verifying invitation: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to verify invitation.")

@router.post("/accept", summary="Accept invitation and complete onboarding")
async def accept_invitation(
    request: AcceptInviteRequest,
    supabase_admin: Client = Depends(get_supabase_admin_client),
):
    """
    Registers the invited user based on their valid token, completing onboarding.
    """
    invitation_service = InvitationService(supabase_admin)
    try:
        result = invitation_service.accept_invitation(request)
        return JSONResponse(status_code=status.HTTP_200_OK, content=result)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error accepting invitation: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to accept invitation.")
