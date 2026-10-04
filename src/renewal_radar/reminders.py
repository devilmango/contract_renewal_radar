from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .notifications import send_notification
from .store import Store


def run_reminders(store: Store, today: date | None = None) -> dict[str, int]:
    """Send once when the renewal window opens, then escalate once if still open."""
    supplied_today = today
    sent = escalated = 0
    now = datetime.now(timezone.utc)
    for task in store.due_tasks(today):
        local_today = supplied_today or now.astimezone(ZoneInfo(task["notice_timezone"] or "UTC")).date()
        if date.fromisoformat(task["due_date"]) > local_today:
            continue
        if not task["reminder_sent_at"]:
            channel = send_notification(task, escalation=False)
            store.mark_notified(task["id"], escalation=False, channel=channel)
            sent += 1

    delay = max(0, int(os.getenv("ESCALATION_AFTER_DAYS", "7")))
    cutoff = supplied_today - timedelta(days=delay) if supplied_today is not None else None
    for task in store.open_for_escalation(cutoff):
        if supplied_today is None:
            zone = ZoneInfo(task["notice_timezone"] or "UTC")
            sent_at = datetime.fromisoformat(task["reminder_sent_at"])
            if (now.astimezone(zone).date() - sent_at.astimezone(zone).date()).days < delay:
                continue
        channel = send_notification(task, escalation=True)
        store.mark_notified(task["id"], escalation=True, channel=channel)
        escalated += 1
    return {"reminders_sent": sent, "escalations_sent": escalated}
