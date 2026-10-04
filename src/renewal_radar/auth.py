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
        "contracts:upload",
        "contracts:review",
        "audit:read",
        "tasks:read",
        "tasks:resolve",
        "calendar:read",
    },
    "owner": {"tasks:read", "tasks:resolve", "calendar:read"},
    "scheduler": {"reminders:run"},
}


@dataclass(frozen=True)
class Principal:
    actor: str
    roles: frozenset[str]
    email: str | None = None

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
    """Authenticates opaque bearer tokens against configured SHA-256 digests."""

    def __init__(self, users: list[TokenRecord]):
        self.users = users

    @classmethod
    def from_env(cls) -> "AuthRegistry":
        raw = os.getenv("RADAR_AUTH_USERS_JSON", "").strip()
        if not raw:
            return cls([])
        try:
            entries = json.loads(raw)
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
            records.append(TokenRecord(digest.lower(), Principal(actor.strip(), frozenset(roles), email)))
        return cls(records)

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
        return None


def create_token_config(actor: str, roles: list[str], email: str | None = None) -> tuple[str, dict]:
    if not actor.strip():
        raise ValueError("Actor must not be empty")
    if not roles or any(role not in SUPPORTED_ROLES for role in roles):
        raise ValueError(f"Choose one or more roles from: {', '.join(sorted(SUPPORTED_ROLES))}")
    if "owner" in roles and not (email and email.strip()):
        raise ValueError("An email address is required when creating an owner token")
    token = secrets.token_urlsafe(32)
    config = {
        "actor": actor.strip(),
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "roles": sorted(set(roles)),
    }
    if email:
        config["email"] = email
    return token, config
