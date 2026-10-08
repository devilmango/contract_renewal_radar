from __future__ import annotations

import os
import smtplib
import hashlib
import base64
import json
from email.message import EmailMessage
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .calendar_sync import graph_access_token


def send_notification(task: Any, escalation: bool) -> str:
    """Send through the configured email provider, falling back to a log."""
    provider = os.getenv("EMAIL_PROVIDER", "smtp").strip().casefold()
    if provider not in {"smtp", "microsoft", "gmail"}:
        raise ValueError("EMAIL_PROVIDER must be one of: smtp, microsoft, gmail")
    recipient = (os.getenv("ESCALATION_EMAIL") if escalation else None) or task["owner_email"]
    host = os.getenv("SMTP_HOST", "").strip()
    if not recipient:
        print(f"[notification-log] {'ESCALATION' if escalation else 'REMINDER'} {task['id']} -> {recipient or 'unassigned'}: {task['title']}")
        return "log"
    if provider == "smtp" and not host:
        print(f"[notification-log] {'ESCALATION' if escalation else 'REMINDER'} {task['id']} -> {recipient}: {task['title']}")
        return "log"
    if provider == "microsoft" and not os.getenv("MS_GRAPH_USER_ID", "").strip():
        raise ValueError("MS_GRAPH_USER_ID is required when EMAIL_PROVIDER=microsoft")
    if provider == "gmail" and not os.getenv("GMAIL_ACCESS_TOKEN", "").strip():
        raise ValueError("GMAIL_ACCESS_TOKEN is required when EMAIL_PROVIDER=gmail")

    message = EmailMessage()
    message["Subject"] = ("Escalation: " if escalation else "Action required: ") + task["title"]
    message["From"] = os.getenv("SMTP_FROM", "renewal-radar@example.com")
    message["To"] = recipient
    message["Message-ID"] = f"<{hashlib.sha256((task['id'] + (':escalation' if escalation else ':reminder')).encode()).hexdigest()}@renewal-radar>"
    message.set_content(
        f"Contract renewal task: {task['title']}\n"
        f"Action date: {task['due_date']}\n"
        f"Contract expiration: {task['expiration_date']}\n"
        + ("This task is still unresolved and has been escalated.\n" if escalation else "Please review the renewal and record the outcome.\n")
    )
    if provider == "microsoft":
        _send_microsoft_email(message, recipient)
    elif provider == "gmail":
        _send_gmail_email(message)
    elif provider == "smtp":
        port = int(os.getenv("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=20) as server:
            if os.getenv("SMTP_STARTTLS", "true").lower() == "true":
                server.starttls()
            username = os.getenv("SMTP_USERNAME")
            if username:
                server.login(username, os.getenv("SMTP_PASSWORD", ""))
            server.send_message(message)
    else:
        raise ValueError("EMAIL_PROVIDER must be one of: smtp, microsoft, gmail")
    return "email"


def delivery_channel(task: Any, escalation: bool) -> str:
    recipient = (os.getenv("ESCALATION_EMAIL") if escalation else None) or task["owner_email"]
    provider = os.getenv("EMAIL_PROVIDER", "smtp").strip().casefold()
    configured = (provider == "smtp" and bool(os.getenv("SMTP_HOST", "").strip())) or provider in {"microsoft", "gmail"}
    return "email" if configured and recipient else "log"


def _send_microsoft_email(message: EmailMessage, recipient: str) -> None:
    user_id = os.getenv("MS_GRAPH_USER_ID", "").strip()
    if not user_id:
        raise ValueError("MS_GRAPH_USER_ID is required for Microsoft email delivery")
    payload = {
        "message": {
            "subject": message["Subject"],
            "body": {"contentType": "Text", "content": message.get_content()},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
            "internetMessageHeaders": [{"name": "X-Renewal-Radar-Message-ID", "value": message["Message-ID"]}],
        },
        "saveToSentItems": True,
    }
    _provider_post(
        f"https://graph.microsoft.com/v1.0/users/{user_id}/sendMail", graph_access_token(), payload,
    )


def _send_gmail_email(message: EmailMessage) -> None:
    token = os.getenv("GMAIL_ACCESS_TOKEN", "").strip()
    if not token:
        raise ValueError("GMAIL_ACCESS_TOKEN is required for Gmail delivery")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
    _provider_post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", token, {"raw": raw})


def _provider_post(url: str, token: str, payload: dict) -> None:
    request = Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            response.read()
    except HTTPError as exc:
        # Provider bodies can contain message or account details; keep these out of persisted job errors.
        raise RuntimeError(f"Email provider returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Email provider request failed: {type(exc).__name__}") from exc
