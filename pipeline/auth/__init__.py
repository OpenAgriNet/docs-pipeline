"""Auth plumbing for API access control (Keycloak JWT + permission guards)."""

from .config import AuthConfig, load_auth_config
from .deps import get_current_user, require_permission
from .models import AuthUser
from .permissions import Permission, permissions_for_roles

__all__ = [
    "AuthConfig",
    "AuthUser",
    "Permission",
    "get_current_user",
    "load_auth_config",
    "permissions_for_roles",
    "require_permission",
]
