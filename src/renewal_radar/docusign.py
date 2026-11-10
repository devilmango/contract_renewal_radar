"""Small DocuSign REST helpers for approved renewal signature handoffs."""

from __future__ import annotations

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class DocuSignError(RuntimeError):
    pass


def api_base_uri(value: str) -> str:
    parts = urlsplit(value)
    host = (parts.hostname or "").casefold()
    if parts.scheme != "https" or not (host == "docusign.net" or host.endswith(".docusign.net") or host == "docusign.com" or host.endswith(".docusign.com")):
        raise DocuSignError("DocuSign API base URI must use HTTPS on a DocuSign domain")
    path = parts.path.rstrip("/")
    if path.endswith("/restapi"):
        path = path[:-8]
    return f"https://{host}{path}/restapi/v2.1"


def _request(url: str, token: str, method: str = "GET", payload: dict | None = None) -> tuple[bytes, str]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return response.read(30 * 1024 * 1024 + 1), response.headers.get("Content-Type", "application/json")
    except HTTPError as exc:
        raise DocuSignError(f"DocuSign returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise DocuSignError(f"DocuSign request failed ({type(exc.reason).__name__})") from exc


def create_envelope(
    base_uri: str, account_id: str, access_token: str, pdf: bytes, filename: str,
    signer_name: str, signer_email: str, email_subject: str,
) -> dict:
    base = api_base_uri(base_uri)
    payload = {
        "emailSubject": email_subject,
        "documents": [{
            "documentBase64": base64.b64encode(pdf).decode("ascii"),
            "name": filename[:200], "fileExtension": "pdf", "documentId": "1",
        }],
        "recipients": {"signers": [{
            "email": signer_email, "name": signer_name,
            "recipientId": "1", "routingOrder": "1",
        }]},
        "status": "sent",
    }
    body, _ = _request(f"{base}/accounts/{account_id}/envelopes", access_token, "POST", payload)
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DocuSignError("DocuSign returned an invalid envelope response") from exc
    if not result.get("envelopeId"):
        raise DocuSignError("DocuSign response did not include an envelope ID")
    return result


def get_account(base_uri: str, account_id: str, access_token: str) -> dict:
    body, _ = _request(f"{api_base_uri(base_uri)}/accounts/{account_id}", access_token)
    return json.loads(body)


def download_completed_document(base_uri: str, account_id: str, envelope_id: str, access_token: str) -> bytes:
    body, content_type = _request(
        f"{api_base_uri(base_uri)}/accounts/{account_id}/envelopes/{envelope_id}/documents/combined",
        access_token,
    )
    if len(body) > 30 * 1024 * 1024:
        raise DocuSignError("Completed agreement exceeds the 30 MB attachment limit")
    if not body or not (content_type.casefold().startswith("application/pdf") or body.startswith(b"%PDF-")):
        raise DocuSignError("DocuSign did not return a completed PDF agreement")
    return body
