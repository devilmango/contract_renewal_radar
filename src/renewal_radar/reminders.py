from __future__ import annotations

import os
from datetime import date, timedelta

from .notifications import send_notification
from .store import Store


def run_reminders(store: Store, today: date | None = None) -> dict[str, int]:
    """Send once when the renewal window opens, then escalate once if still open."""
    today = today or date.today()
    sent = escalated = 0
    for task in store.due_tasks(today):
        if not task["reminder_sent_at"]:
            channel = send_notification(task, escalation=False)
            store.mark_notified(task["id"], escalation=False, channel=channel)
            sent += 1

    delay = max(0, int(os.getenv("ESCALATION_AFTER_DAYS", "7")))
    cutoff = today - timedelta(days=delay)
    for task in store.open_for_escalation(cutoff):
        channel = send_notification(task, escalation=True)
        store.mark_notified(task["id"], escalation=True, channel=channel)
        escalated += 1
    return {"reminders_sent": sent, "escalations_sent": escalated}
