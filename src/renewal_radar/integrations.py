from __future__ import annotations

import base64
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _encryption_key() -> bytes:
    raw = os.getenv("INTEGRATION_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise RuntimeError("INTEGRATION_ENCRYPTION_KEY must be set before saving provider credentials.")
    try:
        key = bytes.fromhex(raw) if len(raw) == 64 else base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, base64.binascii.Error) as exc:
        raise RuntimeError("INTEGRATION_ENCRYPTION_KEY must be 32-byte urlsafe-base64 or 64-character hex.") from exc
    if len(key) != 32:
        raise RuntimeError("INTEGRATION_ENCRYPTION_KEY must decode to exactly 32 bytes.")
    return key


def encrypt_credentials(tenant_id: str, integration_name: str, credentials: dict[str, str]) -> str:
    nonce = os.urandom(12)
    aad = f"{tenant_id}:{integration_name}".encode()
    ciphertext = AESGCM(_encryption_key()).encrypt(nonce, json.dumps(credentials).encode(), aad)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode().rstrip("=")


def decrypt_credentials(tenant_id: str, integration_name: str, value: str) -> dict[str, str]:
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    aad = f"{tenant_id}:{integration_name}".encode()
    plain = AESGCM(_encryption_key()).decrypt(raw[:12], raw[12:], aad)
    result = json.loads(plain)
    if not isinstance(result, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in result.items()):
        raise RuntimeError("Stored integration credentials have an invalid format.")
    return result


def access_token(provider: str, credentials: dict[str, str]) -> str:
    token = credentials.get("access_token", "").strip()
    if token:
        return token
    if provider in {"microsoft_calendar", "microsoft_email"}:
        tenant = credentials.get("tenant_id", "").strip()
        client_id = credentials.get("client_id", "").strip()
        client_secret = credentials.get("client_secret", "").strip()
        if not all((tenant, client_id, client_secret)):
            raise RuntimeError("Microsoft connection needs access_token or tenant_id, client_id, and client_secret.")
        body = urlencode({
            "client_id": client_id, "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials",
        }).encode()
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    elif provider in {"google_calendar", "gmail"}:
        refresh = credentials.get("refresh_token", "").strip()
        client_id = credentials.get("client_id", "").strip()
        client_secret = credentials.get("client_secret", "").strip()
        if not all((refresh, client_id, client_secret)):
            raise RuntimeError("Google connection needs access_token or refresh_token, client_id, and client_secret.")
        body = urlencode({
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh, "grant_type": "refresh_token",
        }).encode()
        url = credentials.get("token_uri", "https://oauth2.googleapis.com/token")
    else:
        raise RuntimeError(f"Provider {provider!r} does not use OAuth access tokens.")
    request = Request(url, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read())
    if not payload.get("access_token"):
        raise RuntimeError("Identity provider did not return an access token.")
    return payload["access_token"]


def public_connection(row: dict) -> dict:
    return {
        "integration": row["integration_name"], "provider": row["provider"],
        "settings": row["settings"], "status": row["status"],
        "updated_by": row["updated_by"], "updated_at": row["updated_at"],
        "last_checked_at": row["last_checked_at"], "last_error": row["last_error"],
        "credentials_configured": bool(row.get("credentials_ciphertext")),
    }
