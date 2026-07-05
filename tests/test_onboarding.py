import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

from app.models.auth_models import (
    OrganizationRegisterRequest,
    InviteUserRequest,
    AcceptInviteRequest
)
from app.services.auth_service import AuthService
from app.services.invitation_service import InvitationService

@pytest.fixture
def mock_supabase_client():
    client = MagicMock()
    # Mock supabase auth admin interface
    client.auth = MagicMock()
    client.auth.admin = MagicMock()
    return client

# ---------------------------------------------------------------------------
# Organization / Manager Onboarding Tests
# ---------------------------------------------------------------------------
def test_register_organization_success(mock_supabase_client):
    auth_service = AuthService(mock_supabase_client)
    
    # Setup mock returns
    mock_supabase_client.auth.admin.create_user.return_value.user = MagicMock(id="mock-user-uuid")
    
    with patch.object(auth_service.user_repo, 'get_user_by_email', return_value=None), \
         patch.object(auth_service.org_repo, 'create_organization', return_value={"id": "mock-org-uuid", "name": "Acme"}) as mock_create_org, \
         patch.object(auth_service.user_repo, 'create_user', return_value={
             "id": "mock-user-uuid",
             "email": "manager@acme.com",
             "full_name": "John Doe",
             "role": "manager"
         }) as mock_create_user:
         
        request = OrganizationRegisterRequest(
            org_name="Acme",
            manager_name="John Doe",
            manager_email="manager@acme.com",
            manager_password="securepassword"
        )
        
        result = auth_service.register_organization(request)
        
        assert result["organization"]["id"] == "mock-org-uuid"
        assert result["user"]["role"] == "manager"
        mock_create_org.assert_called_once_with(name="Acme", industry_context=None)
        mock_create_user.assert_called_once()

def test_register_organization_email_exists(mock_supabase_client):
    auth_service = AuthService(mock_supabase_client)
    
    with patch.object(auth_service.user_repo, 'get_user_by_email', return_value={"id": "existing-user"}):
        request = OrganizationRegisterRequest(
            org_name="Acme",
            manager_name="John Doe",
            manager_email="manager@acme.com",
            manager_password="securepassword"
        )
        
        with pytest.raises(ValueError, match="Email already registered."):
            auth_service.register_organization(request)

def test_register_organization_rollback_on_db_fail(mock_supabase_client):
    auth_service = AuthService(mock_supabase_client)
    
    # Auth user creation succeeds, database user creation fails
    mock_supabase_client.auth.admin.create_user.return_value.user = MagicMock(id="mock-user-uuid")
    
    # Mock delete API for rollback verification
    mock_delete_user = mock_supabase_client.auth.admin.delete_user
    mock_table = mock_supabase_client.table
    
    with patch.object(auth_service.user_repo, 'get_user_by_email', return_value=None), \
         patch.object(auth_service.org_repo, 'create_organization', return_value={"id": "mock-org-uuid"}), \
         patch.object(auth_service.user_repo, 'create_user', side_effect=Exception("Database insert error")):
         
        request = OrganizationRegisterRequest(
            org_name="Acme",
            manager_name="John Doe",
            manager_email="manager@acme.com",
            manager_password="securepassword"
        )
        
        with pytest.raises(ValueError, match="could not map user record in database"):
            auth_service.register_organization(request)
            
        # Verify rollback triggers
        mock_delete_user.assert_called_once_with("mock-user-uuid")
        mock_table.assert_any_call("Organizations")

def test_organization_register_request_validation():
    from pydantic import ValidationError
    # Invalid industry should raise ValidationError
    with pytest.raises(ValidationError):
        OrganizationRegisterRequest(
            org_name="Acme",
            industry_context="IT",
            manager_name="John Doe",
            manager_email="manager@acme.com",
            manager_password="securepassword"
        )
    # Valid industry should succeed
    req = OrganizationRegisterRequest(
        org_name="Acme",
        industry_context="restaurants",
        manager_name="John Doe",
        manager_email="manager@acme.com",
        manager_password="securepassword"
    )
    assert req.industry_context == "restaurants"

# ---------------------------------------------------------------------------
# Invitation Flow & RBAC Tests
# ---------------------------------------------------------------------------
def test_send_invitation_rbac_manager(mock_supabase_client):
    invitation_service = InvitationService(mock_supabase_client)
    sender = {"role": "manager", "org_id": "org-1", "user_id": "user-manager"}
    
    mock_supabase_client.auth.admin.invite_user_by_email.return_value.user = MagicMock(id="mock-user-uuid")
    
    with patch.object(invitation_service.user_repo, 'get_user_by_email', return_value=None), \
         patch.object(invitation_service.invite_repo, 'revoke_pending_invitations'), \
         patch.object(invitation_service.invite_repo, 'create_invitation', return_value={"id": "invite-1"}) as mock_create_invite:
         
        request = InviteUserRequest(email="rep@acme.com", role="sales_rep")
        res = invitation_service.send_invitation(sender, request)
        
        assert res["success"] is True
        mock_create_invite.assert_called_once()

def test_send_invitation_rbac_admin_success(mock_supabase_client):
    invitation_service = InvitationService(mock_supabase_client)
    sender = {"role": "admin", "org_id": "org-1", "user_id": "user-admin"}
    
    mock_supabase_client.auth.admin.invite_user_by_email.return_value.user = MagicMock(id="mock-user-uuid")
    
    with patch.object(invitation_service.user_repo, 'get_user_by_email', return_value=None), \
         patch.object(invitation_service.invite_repo, 'revoke_pending_invitations'), \
         patch.object(invitation_service.invite_repo, 'create_invitation', return_value={"id": "invite-1"}):
         
        # Admins CAN invite sales reps
        request = InviteUserRequest(email="rep@acme.com", role="sales_rep")
        res = invitation_service.send_invitation(sender, request)
        assert res["success"] is True

def test_send_invitation_rbac_admin_forbidden(mock_supabase_client):
    invitation_service = InvitationService(mock_supabase_client)
    sender = {"role": "admin", "org_id": "org-1", "user_id": "user-admin"}
    
    # Admins CANNOT invite other admins
    request = InviteUserRequest(email="otheradmin@acme.com", role="admin")
    with pytest.raises(PermissionError, match="Administrators can only invite sales representatives"):
        invitation_service.send_invitation(sender, request)

def test_send_invitation_rbac_rep_forbidden(mock_supabase_client):
    invitation_service = InvitationService(mock_supabase_client)
    sender = {"role": "sales_rep", "org_id": "org-1", "user_id": "user-rep"}
    
    # Sales reps CANNOT invite anyone
    request = InviteUserRequest(email="newrep@acme.com", role="sales_rep")
    with pytest.raises(PermissionError, match="Sales representatives cannot invite users"):
        invitation_service.send_invitation(sender, request)

def test_send_invitation_missing_org_id(mock_supabase_client):
    invitation_service = InvitationService(mock_supabase_client)
    sender = {"role": "manager", "user_id": "user-manager"}
    
    request = InviteUserRequest(email="rep@acme.com", role="sales_rep")
    with pytest.raises(ValueError, match="Sender does not belong to any organization"):
        invitation_service.send_invitation(sender, request)

def test_verify_invitation_expired(mock_supabase_client):
    invitation_service = InvitationService(mock_supabase_client)
    expired_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    mock_invite = {
        "id": "invite-1",
        "email": "test@acme.com",
        "requested_role": "sales_rep",
        "org_id": "org-1",
        "status": "pending",
        "expires_at": expired_time
    }
    
    with patch.object(invitation_service.invite_repo, 'get_pending_invitation_by_id', return_value=mock_invite), \
         patch.object(invitation_service.invite_repo, 'update_invitation_status') as mock_update_status:
         
        with pytest.raises(ValueError, match="Invitation token has expired"):
            invitation_service.verify_invitation_by_user({"user_id": "invite-1"})
            
        mock_update_status.assert_called_once_with("invite-1", "expired")

def test_accept_invitation_success(mock_supabase_client):
    invitation_service = InvitationService(mock_supabase_client)
    mock_invite = {
        "id": "mock-user-uuid",
        "email": "test@acme.com",
        "requested_role": "sales_rep",
        "org_id": "org-1",
        "status": "pending",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    }
    
    mock_user_client = MagicMock()
    mock_user_client.auth = MagicMock()
    mock_user_client.auth.update_user.return_value.user = MagicMock(id="mock-user-uuid")
    
    with patch("app.services.invitation_service.create_client", return_value=mock_user_client), \
         patch.object(invitation_service, 'verify_invitation_by_user', return_value=mock_invite), \
         patch.object(invitation_service.user_repo, 'get_user_by_email', return_value=None), \
         patch.object(invitation_service.user_repo, 'create_user', return_value={
             "id": "mock-user-uuid",
             "email": "test@acme.com",
             "full_name": "Jane Doe",
             "role": "sales_rep"
         }) as mock_create_user, \
         patch.object(invitation_service.invite_repo, 'update_invitation_status') as mock_update_status:
         
        current_user = {
            "user_id": "mock-user-uuid",
            "email": "test@acme.com",
            "token": "valid-jwt-token"
        }
        
        res = invitation_service.accept_invitation(
            current_user=current_user,
            full_name="Jane Doe",
            password="newpassword123"
        )
        
        assert res["success"] is True
        assert res["user"]["email"] == "test@acme.com"
        mock_create_user.assert_called_once()
        mock_update_status.assert_called_once()
        mock_user_client.auth.update_user.assert_called_once()
