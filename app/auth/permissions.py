"""Role -> permission mapping. Kept tiny on purpose; extend here when roles are added."""

from __future__ import annotations

from dataclasses import dataclass

ALL = "*"

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset({ALL}),
    "user": frozenset({"dashboard.view", "account.manage", "telegram.manage"}),
}


@dataclass(frozen=True)
class Principal:
    """The authenticated user. Every user-owned query MUST filter by ``id``."""

    id: str
    email: str
    display_name: str
    is_admin: bool

    @property
    def role(self) -> str:
        return "admin" if self.is_admin else "user"

    @property
    def permissions(self) -> frozenset[str]:
        return ROLE_PERMISSIONS[self.role]

    def can(self, permission: str) -> bool:
        perms = self.permissions
        return ALL in perms or permission in perms
