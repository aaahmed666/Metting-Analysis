from supabase import Client
from typing import Optional
from datetime import datetime

class InvitationRepository:
    """
    Handles all direct database interactions with the Invitations table.
    """
    def __init__(self, supabase_client: Client):
        self.client = supabase_client

    def create_invitation(
        self,
        org_id: str,
        email: str,
        role: str,
        token: str,
        invited_by: str,
        expires_at: datetime,
        team_id: Optional[str] = None,
        invitation_id: Optional[str] = None
    ) -> dict:
        """
        Inserts a new pending invitation record.
        """
        db_record = {
            "org_id": org_id,
            "email": email,
            "requested_role": role,
            "token": token,
            "invited_by": invited_by,
            "status": "pending",
            "expires_at": expires_at.isoformat(),
        }
        if team_id is not None:
            db_record["team_id"] = team_id
        if invitation_id is not None:
            db_record["id"] = invitation_id

        response = self.client.table("Invitations").insert(db_record).execute()
        if not response.data:
            raise Exception("Failed to insert invitation record into the database.")
            
        return response.data[0]

    def get_pending_invitation_by_id(self, invitation_id: str) -> Optional[dict]:
        """
        Queries a pending invitation record by its ID (which matches the user's Auth UUID).
        """
        response = self.client.table("Invitations")\
            .select("*")\
            .eq("id", invitation_id)\
            .eq("status", "pending")\
            .execute()
        if response.data:
            return response.data[0]
        return None

    def get_invitation_by_token(self, token: str) -> Optional[dict]:
        """
        Queries an invitation record by its secure verification token.
        """
        response = self.client.table("Invitations").select("*").eq("token", token).execute()
        if response.data:
            return response.data[0]
        return None

    def update_invitation_status(self, invitation_id: str, status: str, accepted_at: Optional[datetime] = None) -> dict:
        """
        Updates the status of an invitation (e.g., to 'accepted' or 'expired').
        """
        update_data = {
            "status": status,
        }
        if accepted_at is not None:
            update_data["accepted_at"] = accepted_at.isoformat()

        response = self.client.table("Invitations").update(update_data).eq("id", invitation_id).execute()
        if not response.data:
            raise Exception("Failed to update invitation status.")
            
        return response.data[0]

    def revoke_pending_invitations(self, email: str, org_id: str) -> None:
        """
        Revokes any previous pending invitations for a specific email within an organization.
        """
        self.client.table("Invitations")\
            .update({"status": "revoked"})\
            .eq("email", email)\
            .eq("org_id", org_id)\
            .eq("status", "pending")\
            .execute()
