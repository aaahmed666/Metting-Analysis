from supabase import Client
from typing import Optional

class UserRepository:
    """
    Handles all direct database interactions with the Users table.
    """
    def __init__(self, supabase_client: Client):
        self.client = supabase_client

    def create_user(
        self,
        user_id: str,
        org_id: str,
        email: str,
        full_name: str,
        role: str,
        is_active: bool = True,
        team_id: Optional[str] = None
    ) -> dict:
        """
        Inserts a new user record.
        """
        db_record = {
            "id": user_id,
            "org_id": org_id,
            "email": email,
            "full_name": full_name,
            "role": role,
            "is_active": is_active,
        }
        if team_id is not None:
            db_record["team_id"] = team_id

        response = self.client.table("Users").insert(db_record).execute()
        if not response.data:
            raise Exception("Failed to insert user record into the database.")
            
        return response.data[0]

    def get_user_by_email(self, email: str) -> Optional[dict]:
        """
        Queries a user by their email address.
        """
        response = self.client.table("Users").select("*").eq("email", email).execute()
        if response.data:
            return response.data[0]
        return None

    def delete_user_record(self, user_id: str) -> None:
        """
        Deletes a user record by ID. This is typically used as a rollback option.
        """
        self.client.table("Users").delete().eq("id", user_id).execute()
