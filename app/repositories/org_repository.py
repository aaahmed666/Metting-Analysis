from supabase import Client

class OrgRepository:
    """
    Handles all direct database interactions with the Organizations table.
    """
    def __init__(self, supabase_client: Client):
        self.client = supabase_client

    def create_organization(self, name: str, industry_context: str | None = None) -> dict:
        """
        Inserts a new organization record.
        """
        db_record = {
            "name": name,
        }
        if industry_context is not None:
            db_record["industry_context"] = industry_context

        # Insert record and execute query
        response = self.client.table("Organizations").insert(db_record).execute()
        
        if not response.data:
            raise Exception("Failed to insert organization record into the database.")
            
        return response.data[0]
