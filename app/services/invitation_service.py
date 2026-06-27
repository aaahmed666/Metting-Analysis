import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from supabase import Client, AuthApiError, create_client

from config.setting import settings
from app.repositories.user_repository import UserRepository
from app.repositories.invitation_repository import InvitationRepository
from app.models.auth_models import InviteUserRequest, AcceptInviteRequest

logger = logging.getLogger(__name__)

class InvitationService:
    """
    Houses business logic for user invitation, token validation, and account activation.
    Remains free of HTTP/FastAPI imports to maintain a strict separation of concerns.
    """
    def __init__(self, supabase_admin: Client):
        self.supabase_admin = supabase_admin
        self.user_repo = UserRepository(supabase_admin)
        self.invite_repo = InvitationRepository(supabase_admin)

    def send_invitation(self, sender: dict, request: InviteUserRequest) -> dict:
        """
        Sends an invitation to a new user, enforcing strict RBAC rules.
        
        RBAC Rules:
        - sales_rep: Cannot send invitations.
        - admin: Can only invite sales_rep.
        - manager: Can invite admin or sales_rep.
        """
        sender_role = sender.get("role")
        sender_org_id = sender.get("org_id")
        sender_id = sender.get("user_id")

        if not sender_org_id:
            raise ValueError("Sender does not belong to any organization.")

        # 1. Enforce RBAC Rules
        if sender_role == "sales_rep":
            raise PermissionError("Access denied. Sales representatives cannot invite users.")
        elif sender_role == "admin":
            if request.role != "sales_rep":
                raise PermissionError("Access denied. Administrators can only invite sales representatives.")

        # 2. Verify that the invited email is not already a registered user
        existing_user = self.user_repo.get_user_by_email(request.email)
        if existing_user:
            raise ValueError("User with this email is already registered.")

        # 3. Revoke any active pending invites for this email within the organization
        self.invite_repo.revoke_pending_invitations(request.email, sender_org_id)

        # 4. Invite user via Supabase Auth
        try:
            auth_response = self.supabase_admin.auth.admin.invite_user_by_email(
                request.email,
                {
                    "data": {
                        "role": request.role,
                        "org_id": sender_org_id,
                        "team_id": request.team_id,
                    }
                }
            )
            invited_user = auth_response.user
            if not invited_user:
                raise Exception("Supabase invite response did not contain user details.")
        except AuthApiError as e:
            logger.error(f"Supabase auth invitation failed: {e.message}")
            raise ValueError(f"Failed to invite user via Supabase: {e.message}")
        except Exception as e:
            logger.error(f"Auth user invitation error: {e}")
            raise ValueError("Failed to invite user via Supabase.")

        # 5. Generate a secure placeholder token and a 48-hour expiration date to satisfy database schema constraints
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=48)

        # 6. Create invitation record in the database mapping the Supabase Auth UUID to the record's ID
        invite = self.invite_repo.create_invitation(
            org_id=sender_org_id,
            email=request.email,
            role=request.role,
            token=token,
            invited_by=sender_id,
            expires_at=expires_at,
            team_id=request.team_id,
            invitation_id=str(invited_user.id)
        )

        logger.info(f"Native Supabase invitation sent to {request.email}")

        return {
            "success": True,
            "message": "Invitation sent successfully.",
            "invitation_id": invite["id"],
        }

    def verify_invitation_by_user(self, current_user: dict) -> dict:
        """
        Validates if a pending invitation exists for the authenticated user and has not expired.
        """
        invite = self.invite_repo.get_pending_invitation_by_id(current_user["user_id"])
        if not invite:
            raise ValueError("No pending invitation found for this user.")

        # Check expiration date (handling ISO timestamp string)
        expires_at_str = invite["expires_at"]
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        
        if datetime.now(timezone.utc) > expires_at:
            self.invite_repo.update_invitation_status(invite["id"], "expired")
            raise ValueError("Invitation token has expired.")

        return invite

    def accept_invitation(self, current_user: dict, full_name: str, password: str) -> dict:
        """
        Accepts the invitation using the authenticated user's session,
        updates the password using a user-scoped client,
        and creates a database record in public.Users.
        """
        # 1. Verify invitation validity
        invite = self.verify_invitation_by_user(current_user)
        email = invite["email"]
        role = invite["requested_role"]
        org_id = invite["org_id"]
        team_id = invite.get("team_id")

        # 2. Check if user already exists in public.Users
        existing_user = self.user_repo.get_user_by_email(email)
        if existing_user:
            self.invite_repo.update_invitation_status(invite["id"], "revoked")
            raise ValueError("User with this email is already registered.")

        # 3. Update user credentials in Supabase Auth using a fresh user-scoped client
        try:
            # We initialize a new client instance rather than utilizing get_supabase_client
            # to avoid mutating a shared lru_cache client instance's credentials.
            user_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)
            user_client.auth.set_session(access_token=current_user["token"], refresh_token=current_user["token"])
            
            auth_response = user_client.auth.update_user({
                "password": password,
                "data": {
                    "full_name": full_name,
                }
            })
            user = auth_response.user
            if not user:
                raise Exception("Auth user update failed.")
        except AuthApiError as e:
            raise ValueError(f"Registration failed: {e.message}")
        except Exception as e:
            logger.error(f"Auth user update error: {e}")
            raise ValueError("Registration failed: could not update credentials.")

        # 4. Create user profile record in public.Users
        try:
            db_user = self.user_repo.create_user(
                user_id=current_user["user_id"],
                org_id=org_id,
                email=email,
                full_name=full_name,
                role=role,
                is_active=True,
                team_id=team_id
            )
        except Exception as e:
            logger.error(f"DB user insert failed: {e}")
            raise ValueError("Registration failed: could not map user record in database.")

        # 5. Mark invitation as accepted
        try:
            self.invite_repo.update_invitation_status(
                invitation_id=invite["id"],
                status="accepted",
                accepted_at=datetime.now(timezone.utc)
            )
        except Exception as e:
            logger.error(f"Failed to update invitation status to accepted: {e}")

        return {
            "success": True,
            "message": "Invitation accepted. Account created successfully.",
            "user": {
                "user_id": db_user["id"],
                "email": db_user["email"],
                "full_name": db_user["full_name"],
                "role": db_user["role"]
            }
        }

