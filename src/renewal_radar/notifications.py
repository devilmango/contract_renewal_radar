from __future__ import annotations

import os
import smtplib
import hashlib
import base64
import json
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener
from email.message import EmailMessage
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .calendar_sync import graph_access_token
from .integrations import access_token, decrypt_credentials


def send_notification(
    task: Any, escalation: bool, recipient_email: str | None = None, escalation_label: str | None = None,
    store=None, channel: str | None = None,
) -> str:
    """Send through the configured email provider, falling back to a log."""
    tenant_id = task["tenant_id"] if store else None
    if channel in {"slack", "teams"}:
        if not store or not tenant_id:
            raise ValueError("Collaboration notifications require a tenant connection")
        connection = store.get_integration_connection(tenant_id, channel)
        if not connection or connection["provider"] != f"{channel}_webhook":
            raise ValueError(f"The {channel} notification connection is not configured")
        credentials = decrypt_credentials(tenant_id, channel, connection["credentials_ciphertext"])
        title = ("Escalation" if escalation else "Reminder") + ": " + task["title"]
        task_kind = task.get("task_kind", "renewal") if isinstance(task, dict) else task["task_kind"]
        details = f"Due: {task['due_date']}"
        if task_kind != "obligation" and task.get("expiration_date"):
            details += f" · Contract expiration: {task['expiration_date']}"
        if escalation_label:
            details += f" · {escalation_label}"
        if channel == "slack":
            payload = {"text": f"*{title}*\n{task['title']}\n{details}"}
        else:
            payload = {"@type": "MessageCard", "@context": "https://schema.org/extensions",
                       "summary": title, "title": title, "text": f"{task['title']}<br>{details}"}
        _post_webhook(credentials.get("webhook_url", ""), payload, channel)
        return channel
    if channel == "log":
        print(f"[notification-log] {'ESCALATION' if escalation else 'REMINDER'} {task['id']} -> {recipient_email or task.get('owner_email') or 'unassigned'}: {task['title']}")
        return "log"
    connection = store.get_integration_connection(tenant_id, "email") if store and tenant_id else None
    settings: dict[str, str] = {}
    credentials: dict[str, str] = {}
    provider = os.getenv("EMAIL_PROVIDER", "smtp").strip().casefold()
    if connection:
        provider = connection["provider"]
        settings = connection["settings"]
        credentials = decrypt_credentials(task["tenant_id"], "email", connection["credentials_ciphertext"])
        provider = {"microsoft_email": "microsoft", "gmail": "gmail", "smtp": "smtp"}[provider]
    if provider not in {"smtp", "microsoft", "gmail"}:
        raise ValueError("EMAIL_PROVIDER must be one of: smtp, microsoft, gmail")
    recipient = recipient_email or (os.getenv("ESCALATION_EMAIL") if escalation else None) or task["owner_email"]
    host = settings.get("host", "").strip() if connection else os.getenv("SMTP_HOST", "").strip()
    if not recipient:
        print(f"[notification-log] {'ESCALATION' if escalation else 'REMINDER'} {task['id']} -> {recipient or 'unassigned'}: {task['title']}")
        return "log"
    if provider == "smtp" and not host:
        print(f"[notification-log] {'ESCALATION' if escalation else 'REMINDER'} {task['id']} -> {recipient}: {task['title']}")
        return "log"
    graph_user = settings.get("user_id", "") if connection else os.getenv("MS_GRAPH_USER_ID", "").strip()
    if provider == "microsoft" and not graph_user:
        raise ValueError("MS_GRAPH_USER_ID is required when EMAIL_PROVIDER=microsoft")
    if provider == "gmail" and not (credentials.get("access_token") or credentials.get("refresh_token") or os.getenv("GMAIL_ACCESS_TOKEN", "").strip()):
        raise ValueError("GMAIL_ACCESS_TOKEN is required when EMAIL_PROVIDER=gmail")

    message = EmailMessage()
    approval_type = task.get("approval_type") if isinstance(task, dict) else None
    prefix = (f"Approval escalation · {escalation_label}: " if escalation and approval_type and escalation_label else
              "Approval escalation: " if escalation and approval_type else
              f"Escalation · {escalation_label}: " if escalation and escalation_label else
              "Escalation: " if escalation else
              "Approval reminder: " if approval_type else "Action required: ")
    message["Subject"] = prefix + task["title"]
    message["From"] = settings.get("from_address", os.getenv("SMTP_FROM", "renewal-radar@example.com"))
    message["To"] = recipient
    message["Message-ID"] = f"<{hashlib.sha256((task['id'] + (':escalation' if escalation else ':reminder')).encode()).hexdigest()}@renewal-radar>"
    task_kind = task.get("task_kind", "renewal") if isinstance(task, dict) else task["task_kind"]
    action_label = "Reviewer approval" if approval_type else "Contract obligation" if task_kind == "obligation" else "Contract renewal"
    message.set_content(
        f"{action_label} task: {task['title']}\n"
        f"Action date: {task['due_date']}\n"
        + (f"Contract expiration: {task['expiration_date']}\n" if task_kind != "obligation" else "")
        + ("This item is awaiting reviewer sign-off. Open the approval inbox to record a decision.\n" if approval_type else "")
        + (f"This task is still unresolved. Escalation: {escalation_label or 'follow-up'}.\n" if escalation else
           "A reviewer decision is still needed. Please open the approval inbox.\n" if approval_type else
           "Please complete the obligation and update its task status.\n" if task_kind == "obligation" else
           "Please review the renewal and record the outcome.\n")
    )
    if provider == "microsoft":
        _send_microsoft_email(
            message, recipient,
            token=access_token(connection["provider"], credentials) if connection else None,
            user_id=graph_user,
        )
    elif provider == "gmail":
        token = access_token(connection["provider"], credentials) if connection else None
        _send_gmail_email(message, token)
    elif provider == "smtp":
        port = int(settings.get("port", os.getenv("SMTP_PORT", "587")))
        with smtplib.SMTP(host, port, timeout=20) as server:
            if settings.get("starttls", os.getenv("SMTP_STARTTLS", "true")).lower() == "true":
                server.starttls()
            username = credentials.get("username") if connection else os.getenv("SMTP_USERNAME")
            if username:
                server.login(username, credentials.get("password", "") if connection else os.getenv("SMTP_PASSWORD", ""))
            server.send_message(message)
    else:
        raise ValueError("EMAIL_PROVIDER must be one of: smtp, microsoft, gmail")
    return "email"


def delivery_channel(task: Any, escalation: bool, recipient_email: str | None = None, store=None,
                     requested_channel: str | None = None) -> str:
    if requested_channel in {"slack", "teams"}:
        connection = store.get_integration_connection(task["tenant_id"], requested_channel) if store else None
        if connection and connection["provider"] == f"{requested_channel}_webhook":
            return requested_channel
        return requested_channel
    if requested_channel == "log":
        return "log"
    recipient = recipient_email or (os.getenv("ESCALATION_EMAIL") if escalation else None) or task["owner_email"]
    tenant_id = task["tenant_id"] if store else None
    connection = store.get_integration_connection(tenant_id, "email") if store and tenant_id else None
    provider = connection["provider"] if connection else os.getenv("EMAIL_PROVIDER", "smtp").strip().casefold()
    if provider == "microsoft_email":
        provider = "microsoft"
    configured = (provider == "smtp" and bool(os.getenv("SMTP_HOST", "").strip())) or provider in {"microsoft", "gmail"}
    return "email" if configured and recipient else "log"


def _post_webhook(url: str, payload: dict, channel: str) -> None:
    validate_webhook_url(url, channel)
    request = Request(url, data=json.dumps(payload).encode(), method="POST",
                      headers={"Accept": "application/json", "Content-Type": "application/json"})
    opener = build_opener(_NoRedirectHandler)
    try:
        with opener.open(request, timeout=15) as response:
            response.read(1024)
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"{channel.capitalize()} webhook returned HTTP {response.status}")
    except HTTPError as exc:
        raise RuntimeError(f"{channel.capitalize()} webhook returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"{channel.capitalize()} webhook request failed: {type(exc).__name__}") from exc


def validate_webhook_url(url: str, channel: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    allowed = host == "hooks.slack.com" if channel == "slack" else any(
        host == suffix or host.endswith("." + suffix)
        for suffix in ("webhook.office.com", "webhook.office365.com", "logic.azure.com")
    )
    if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
        raise ValueError(f"The configured {channel} webhook URL is not an approved HTTPS endpoint")


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _send_microsoft_email(message: EmailMessage, recipient: str, token: str | None = None, user_id: str | None = None) -> None:
    user_id = user_id or os.getenv("MS_GRAPH_USER_ID", "").strip()
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
        f"https://graph.microsoft.com/v1.0/users/{user_id}/sendMail", token or graph_access_token(), payload,
    )


def _send_gmail_email(message: EmailMessage, token: str | None = None) -> None:
    token = token or os.getenv("GMAIL_ACCESS_TOKEN", "").strip()
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
