"""Keycloak Admin API client for master-admin user management."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


ALLOWED_REALM_ROLES = frozenset({"master_admin", "admin", "content_curator", "viewer"})


@dataclass(frozen=True)
class KeycloakAdminConfig:
    base_url: str
    realm: str
    admin_user: str
    admin_password: str


def load_keycloak_admin_config() -> KeycloakAdminConfig | None:
    """Return admin config when enough env is present; otherwise None."""
    base = (
        os.environ.get("KEYCLOAK_ADMIN_BASE_URL")
        or os.environ.get("KEYCLOAK_BASE_URL")
        or ""
    ).rstrip("/")
    # Derive from issuer when possible: .../realms/{realm} → ... + realm
    issuer = (os.environ.get("KEYCLOAK_ISSUER") or "").rstrip("/")
    realm = (os.environ.get("KEYCLOAK_REALM") or "").strip()
    if not realm and "/realms/" in issuer:
        realm = issuer.rsplit("/realms/", 1)[-1].strip("/")
    if not base and issuer and "/realms/" in issuer:
        base = issuer.split("/realms/", 1)[0]
    password = os.environ.get("KEYCLOAK_ADMIN_PASSWORD") or ""
    user = os.environ.get("KEYCLOAK_ADMIN") or "admin"
    if not base or not realm or not password:
        return None
    return KeycloakAdminConfig(
        base_url=base,
        realm=realm,
        admin_user=user,
        admin_password=password,
    )


class KeycloakAdminError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class KeycloakAdminClient:
    """Thin Admin REST wrapper for listing users and updating access claims."""

    def __init__(self, config: KeycloakAdminConfig):
        self.config = config
        self._token: str | None = None

    def _req(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        body: Any = None,
        form: dict | None = None,
    ) -> tuple[int, Any]:
        headers: dict[str, str] = {}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise KeycloakAdminError(
                f"{method} {url} -> {exc.code}: {raw}",
                status_code=502 if exc.code >= 500 else exc.code,
            ) from exc

    def _access_token(self) -> str:
        if self._token:
            return self._token
        _, body = self._req(
            "POST",
            f"{self.config.base_url}/realms/master/protocol/openid-connect/token",
            form={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": self.config.admin_user,
                "password": self.config.admin_password,
            },
        )
        self._token = body["access_token"]
        return self._token

    @property
    def _admin(self) -> str:
        return f"{self.config.base_url}/admin/realms/{self.config.realm}"

    def list_users(self, *, search: str | None = None, first: int = 0, max_results: int = 50) -> list[dict]:
        params: dict[str, str | int] = {"first": first, "max": max_results, "briefRepresentation": "false"}
        if search:
            params["search"] = search
        _, users = self._req(
            "GET",
            f"{self._admin}/users?{urllib.parse.urlencode(params)}",
            token=self._access_token(),
        )
        return [self._serialize_user(u) for u in (users or [])]

    def get_user(self, user_id: str) -> dict:
        _, user = self._req(
            "GET",
            f"{self._admin}/users/{urllib.parse.quote(user_id)}",
            token=self._access_token(),
        )
        if not user:
            raise KeycloakAdminError("User not found", status_code=404)
        roles = self._realm_roles(user_id)
        return self._serialize_user(user, roles=roles)

    def update_user_access(
        self,
        user_id: str,
        *,
        instances: list[str] | None = None,
        envs: list[str] | None = None,
        roles: list[str] | None = None,
        enabled: bool | None = None,
    ) -> dict:
        if roles is not None:
            unknown = sorted({r.strip().lower() for r in roles if r.strip()} - ALLOWED_REALM_ROLES)
            if unknown:
                raise KeycloakAdminError(
                    f"Unknown roles: {', '.join(unknown)}. Allowed: {', '.join(sorted(ALLOWED_REALM_ROLES))}",
                    status_code=400,
                )

        _, user = self._req(
            "GET",
            f"{self._admin}/users/{urllib.parse.quote(user_id)}",
            token=self._access_token(),
        )
        if not user:
            raise KeycloakAdminError("User not found", status_code=404)

        attrs = dict(user.get("attributes") or {})
        if instances is not None:
            attrs["instances"] = [str(v).strip().lower() for v in instances if str(v).strip()]
        if envs is not None:
            attrs["envs"] = [str(v).strip().lower() for v in envs if str(v).strip()]

        body = {
            "id": user["id"],
            "username": user.get("username"),
            "email": user.get("email"),
            "firstName": user.get("firstName"),
            "lastName": user.get("lastName"),
            "enabled": user.get("enabled", True) if enabled is None else bool(enabled),
            "attributes": attrs,
        }
        self._req(
            "PUT",
            f"{self._admin}/users/{urllib.parse.quote(user_id)}",
            token=self._access_token(),
            body=body,
        )

        if roles is not None:
            self._replace_realm_roles(user_id, roles)

        return self.get_user(user_id)

    def _realm_roles(self, user_id: str) -> list[str]:
        _, mappings = self._req(
            "GET",
            f"{self._admin}/users/{urllib.parse.quote(user_id)}/role-mappings/realm",
            token=self._access_token(),
        )
        return sorted(
            {
                (item.get("name") or "").strip()
                for item in (mappings or [])
                if (item.get("name") or "").strip() in ALLOWED_REALM_ROLES
            }
        )

    def _replace_realm_roles(self, user_id: str, roles: list[str]) -> None:
        wanted = sorted(
            {
                r.strip().lower()
                for r in roles
                if r and r.strip().lower() in ALLOWED_REALM_ROLES
            }
        )

        current = self._realm_roles(user_id)
        to_remove = [r for r in current if r not in wanted]
        to_add = [r for r in wanted if r not in current]

        if to_remove:
            payloads = []
            for name in to_remove:
                _, role = self._req(
                    "GET",
                    f"{self._admin}/roles/{urllib.parse.quote(name)}",
                    token=self._access_token(),
                )
                payloads.append(role)
            self._req(
                "DELETE",
                f"{self._admin}/users/{urllib.parse.quote(user_id)}/role-mappings/realm",
                token=self._access_token(),
                body=payloads,
            )
        if to_add:
            payloads = []
            for name in to_add:
                _, role = self._req(
                    "GET",
                    f"{self._admin}/roles/{urllib.parse.quote(name)}",
                    token=self._access_token(),
                )
                payloads.append(role)
            self._req(
                "POST",
                f"{self._admin}/users/{urllib.parse.quote(user_id)}/role-mappings/realm",
                token=self._access_token(),
                body=payloads,
            )

    @staticmethod
    def _attr_list(attrs: dict | None, key: str) -> list[str]:
        raw = (attrs or {}).get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(v).strip() for v in raw if str(v).strip()]

    def _serialize_user(self, user: dict, roles: list[str] | None = None) -> dict:
        attrs = user.get("attributes") or {}
        return {
            "id": user.get("id"),
            "username": user.get("username"),
            "email": user.get("email"),
            "enabled": bool(user.get("enabled", True)),
            "instances": self._attr_list(attrs, "instances"),
            "envs": self._attr_list(attrs, "envs"),
            "roles": roles if roles is not None else [],
        }
