import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from supabase import Client, AuthApiError

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

        # 4. Generate a secure token and a 48-hour expiration date
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=48)

        # 5. Create invitation record in the database
        invite = self.invite_repo.create_invitation(
            org_id=sender_org_id,
            email=request.email,
            role=request.role,
            token=token,
            invited_by=sender_id,
            expires_at=expires_at,
            team_id=request.team_id
        )

        logger.info(f"Simulated invite email sent to {request.email} with token {token}")

        return {
            "success": True,
            "message": "Invitation sent successfully.",
            "invitation_id": invite["id"],
            "token": token,  # Return token for integration test purposes
        }

    def verify_invitation_token(self, token: str) -> dict:
        """
        Validates if the invitation token exists, is pending, and has not expired.
        """
        invite = self.invite_repo.get_invitation_by_token(token)
        if not invite:
            raise ValueError("Invalid invitation token.")

        if invite["status"] != "pending":
            raise ValueError(f"Invitation has already been {invite['status']}.")

        # Check expiration date (handling ISO timestamp string)
        expires_at_str = invite["expires_at"]
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        
        if datetime.now(timezone.utc) > expires_at:
            self.invite_repo.update_invitation_status(invite["id"], "expired")
            raise ValueError("Invitation token has expired.")

        return invite

    def accept_invitation(self, request: AcceptInviteRequest) -> dict:
        """
        Accepts the invitation, creates Supabase auth credentials,
        and creates a database record in public.Users.
        """
        # 1. Verify invitation token validity
        invite = self.verify_invitation_token(request.token)
        email = invite["email"]
        role = invite["requested_role"]
        org_id = invite["org_id"]
        team_id = invite.get("team_id")

        # 2. Check if user already exists
        existing_user = self.user_repo.get_user_by_email(email)
        if existing_user:
            self.invite_repo.update_invitation_status(invite["id"], "revoked")
            raise ValueError("User with this email is already registered.")

        # 3. Create user in Supabase Auth
        user = None
        try:
            auth_response = self.supabase_admin.auth.admin.create_user({
                "email": email,
                "password": request.password,
                "email_confirm": True,
                "user_metadata": {
                    "full_name": request.full_name,
                    "role": role,
                }
            })
            user = auth_response.user
            if not user:
                raise Exception("Auth user creation failed.")
        except AuthApiError as e:
            raise ValueError(f"Registration failed: {e.message}")
        except Exception as e:
            logger.error(f"Auth user creation error: {e}")
            raise ValueError("Registration failed: could not create credentials.")

        # 4. Create user profile record in public.Users
        try:
            db_user = self.user_repo.create_user(
                user_id=str(user.id),
                org_id=org_id,
                email=email,
                full_name=request.full_name,
                role=role,
                is_active=True,
                team_id=team_id
            )
        except Exception as e:
            # ROLLBACK: delete created auth user
            logger.error(f"DB user insert failed, rolling back auth user: {e}")
            try:
                self.supabase_admin.auth.admin.delete_user(str(user.id))
            except Exception as rollback_err:
                logger.error(f"Failed to delete auth user during rollback: {rollback_err}")
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
