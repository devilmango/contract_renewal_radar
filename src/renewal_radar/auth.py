from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass


SUPPORTED_ROLES = {"admin", "reviewer", "owner", "scheduler"}

ROLE_SCOPES = {
    "admin": {"*"},
    "reviewer": {
        "contracts:read",
        "contracts:source",
        "contracts:upload",
        "contracts:review",
        "evaluation:approve",
        "audit:read",
        "tasks:read",
        "tasks:resolve",
        "tasks:workflow",
        "tasks:assign",
        "tasks:comment",
        "notices:create",
        "notices:read",
        "notices:approve",
        "notices:dispatch",
        "integrations:sync",
        "jobs:read",
        "data:hold",
        "access:read",
        "calendar:read",
        "calendar:sync",
    },
    "owner": {"tasks:read", "tasks:resolve", "tasks:workflow", "tasks:comment", "notices:create", "notices:read", "calendar:read"},
    "scheduler": {"reminders:run", "integrations:sync", "jobs:read"},
}


@dataclass(frozen=True)
class Principal:
    actor: str
    roles: frozenset[str]
    email: str | None = None
    tenant_id: str = "default"

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes

    @property
    def scopes(self) -> frozenset[str]:
        return frozenset(scope for role in self.roles for scope in ROLE_SCOPES[role])

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


@dataclass(frozen=True)
class TokenRecord:
    token_sha256: str
    principal: Principal


class AuthenticationConfigurationError(ValueError):
    pass


class AuthRegistry:
    """Authenticates local digested tokens or signed OIDC access tokens."""

    def __init__(self, users: list[TokenRecord], oidc: dict | None = None):
        self.users = users
        self.oidc = oidc
        self._jwks_client = None

    @classmethod
    def from_env(cls) -> "AuthRegistry":
        raw = os.getenv("RADAR_AUTH_USERS_JSON", "").strip()
        try:
            entries = json.loads(raw) if raw else []
        except json.JSONDecodeError as exc:
            raise AuthenticationConfigurationError("RADAR_AUTH_USERS_JSON must contain a JSON array.") from exc
        if not isinstance(entries, list):
            raise AuthenticationConfigurationError("RADAR_AUTH_USERS_JSON must contain a JSON array.")

        records: list[TokenRecord] = []
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise AuthenticationConfigurationError(f"User entry {index} must be a JSON object.")
            actor = entry.get("actor")
            digest = entry.get("token_sha256", "")
            roles = entry.get("roles")
            email = entry.get("email")
            if not isinstance(actor, str) or not actor.strip():
                raise AuthenticationConfigurationError(f"User entry {index} needs a non-empty actor.")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise AuthenticationConfigurationError(f"User entry {index} needs a 64-character SHA-256 token digest.")
            if digest.lower() in seen:
                raise AuthenticationConfigurationError("Each configured bearer token digest must be unique.")
            if (
                not isinstance(roles, list)
                or not roles
                or any(not isinstance(role, str) or role not in SUPPORTED_ROLES for role in roles)
            ):
                allowed = ", ".join(sorted(SUPPORTED_ROLES))
                raise AuthenticationConfigurationError(f"User entry {index} roles must be a non-empty list chosen from: {allowed}.")
            if email is not None and not isinstance(email, str):
                raise AuthenticationConfigurationError(f"User entry {index} email must be a string when provided.")
            if "owner" in roles and (not isinstance(email, str) or not email.strip()):
                raise AuthenticationConfigurationError(f"User entry {index} needs an email address for the owner role.")
            seen.add(digest.lower())
            tenant_id = entry.get("tenant_id", "default")
            if not isinstance(tenant_id, str) or not tenant_id.strip():
                raise AuthenticationConfigurationError(f"User entry {index} tenant_id must be a non-empty string.")
            records.append(TokenRecord(digest.lower(), Principal(actor.strip(), frozenset(roles), email, tenant_id.strip())))
        oidc = None
        jwks_url = os.getenv("OIDC_JWKS_URL", "").strip()
        if jwks_url:
            issuer = os.getenv("OIDC_ISSUER", "").strip()
            audience = os.getenv("OIDC_AUDIENCE", "").strip()
            if not issuer or not audience:
                raise AuthenticationConfigurationError("OIDC_ISSUER and OIDC_AUDIENCE are required with OIDC_JWKS_URL.")
            oidc = {
                "jwks_url": jwks_url,
                "issuer": issuer,
                "audience": audience,
                "roles_claim": os.getenv("OIDC_ROLES_CLAIM", "roles"),
                "tenant_claim": os.getenv("OIDC_TENANT_CLAIM", "tenant_id"),
            }
        return cls(records, oidc)

    def authenticate(self, authorization: str | None) -> Principal | None:
        if not authorization:
            return None
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.casefold() != "bearer":
            return None
        token = token.strip()
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for record in self.users:
            if hmac.compare_digest(digest, record.token_sha256):
                return record.principal
        if self.oidc:
            claims = self.verify_oidc_token(token)
            if claims is None:
                return None
            raw_roles = claims.get(self.oidc["roles_claim"], [])
            if isinstance(raw_roles, str):
                raw_roles = [raw_roles]
            roles = frozenset(role for role in raw_roles if role in SUPPORTED_ROLES) if isinstance(raw_roles, list) else frozenset()
            tenant_id = claims.get(self.oidc["tenant_claim"])
            actor = claims.get("preferred_username") or claims.get("email") or claims.get("sub")
            if not roles or not isinstance(tenant_id, str) or not tenant_id.strip() or not isinstance(actor, str):
                return None
            email = claims.get("email")
            return Principal(actor, roles, email if isinstance(email, str) else None, tenant_id)
        return None

    def verify_oidc_token(self, token: str) -> dict | None:
        if not self.oidc:
            return None
        try:
            import jwt

            if self._jwks_client is None:
                self._jwks_client = jwt.PyJWKClient(self.oidc["jwks_url"], cache_jwk_set=True, lifespan=300)
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self.oidc["audience"],
                issuer=self.oidc["issuer"],
                options={"require": ["exp", "iss", "sub", "aud"]},
            )
        except Exception:
            return None


def create_token_config(actor: str, roles: list[str], email: str | None = None, tenant_id: str = "default") -> tuple[str, dict]:
    if not actor.strip():
        raise ValueError("Actor must not be empty")
    if not roles or any(role not in SUPPORTED_ROLES for role in roles):
        raise ValueError(f"Choose one or more roles from: {', '.join(sorted(SUPPORTED_ROLES))}")
    if "owner" in roles and not (email and email.strip()):
        raise ValueError("An email address is required when creating an owner token")
    if not tenant_id.strip():
        raise ValueError("Tenant ID must not be empty")
    token = secrets.token_urlsafe(32)
    config = {
        "actor": actor.strip(),
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "roles": sorted(set(roles)),
        "tenant_id": tenant_id.strip(),
    }
    if email:
        config["email"] = email
    return token, config
