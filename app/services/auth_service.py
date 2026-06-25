import logging
from supabase import Client, AuthApiError
from app.repositories.org_repository import OrgRepository
from app.repositories.user_repository import UserRepository
from app.models.auth_models import OrganizationRegisterRequest

logger = logging.getLogger(__name__)

class AuthService:
    """
    Orchestrates authentication workflows and user/organization creation logic.
    """
    def __init__(self, supabase_admin: Client):
        self.supabase_admin = supabase_admin
        self.org_repo = OrgRepository(supabase_admin)
        self.user_repo = UserRepository(supabase_admin)

    def register_organization(self, request: OrganizationRegisterRequest) -> dict:
        """
        Creates an organization and registers the manager user in a unified flow.
        Performs rollbacks if any downstream database insert or auth creation fails.
        """
        # Step 1: Validate email is not already registered
        existing_user = self.user_repo.get_user_by_email(request.manager_email)
        if existing_user:
            raise ValueError("Email already registered.")

        # Step 2: Create organization record in the database
        org = self.org_repo.create_organization(
            name=request.org_name,
            industry_context=request.industry_context
        )
        org_id = org["id"]

        # Step 3: Create manager user in Supabase Auth
        user = None
        try:
            auth_response = self.supabase_admin.auth.admin.create_user({
                "email": request.manager_email,
                "password": request.manager_password,
                "email_confirm": True,
                "user_metadata": {
                    "full_name": request.manager_name,
                    "role": "manager",
                }
            })
            user = auth_response.user
            if not user:
                raise Exception("Auth creation response did not contain user data.")
        except AuthApiError as e:
            # ROLLBACK: clean up organization since auth user creation failed
            logger.error(f"Supabase auth registration failed: {e.message}")
            self.supabase_admin.table("Organizations").delete().eq("id", org_id).execute()
            raise ValueError(f"Registration failed: {e.message}")
        except Exception as e:
            logger.error(f"Auth user creation error: {e}")
            self.supabase_admin.table("Organizations").delete().eq("id", org_id).execute()
            raise ValueError("Registration failed: could not create credentials.")

        # Step 4: Create manager user record in public.Users table
        try:
            db_user = self.user_repo.create_user(
                user_id=str(user.id),
                org_id=org_id,
                email=request.manager_email,
                full_name=request.manager_name,
                role="manager",
                is_active=True
            )
        except Exception as e:
            # ROLLBACK: delete both auth user and organization on failure
            logger.error(f"DB user insert failed, rolling back organization and auth user: {e}")
            try:
                self.supabase_admin.auth.admin.delete_user(str(user.id))
            except Exception as rollback_err:
                logger.error(f"Failed to delete auth user during rollback: {rollback_err}")
            try:
                self.supabase_admin.table("Organizations").delete().eq("id", org_id).execute()
            except Exception as rollback_err:
                logger.error(f"Failed to delete organization during rollback: {rollback_err}")
                
            raise ValueError("Registration failed: could not map user record in database.")

        return {
            "organization": org,
            "user": {
                "user_id": db_user["id"],
                "email": db_user["email"],
                "full_name": db_user["full_name"],
                "role": db_user["role"],
            }
        }
