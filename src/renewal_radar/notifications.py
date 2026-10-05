from __future__ import annotations

import os
import smtplib
import hashlib
from email.message import EmailMessage
from typing import Any


def send_notification(task: Any, escalation: bool) -> str:
    """Send email when SMTP is configured; otherwise record a log notification."""
    host = os.getenv("SMTP_HOST")
    recipient = (os.getenv("ESCALATION_EMAIL") if escalation else None) or task["owner_email"]
    if not host or not recipient:
        print(f"[notification-log] {'ESCALATION' if escalation else 'REMINDER'} {task['id']} -> {recipient or 'unassigned'}: {task['title']}")
        return "log"

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
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=20) as server:
        if os.getenv("SMTP_STARTTLS", "true").lower() == "true":
            server.starttls()
        username = os.getenv("SMTP_USERNAME")
        if username:
            server.login(username, os.getenv("SMTP_PASSWORD", ""))
        server.send_message(message)
    return "email"
