from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .notifications import delivery_channel, send_notification
from .store import Store


def run_reminders(store: Store, today: date | None = None, tenant_id: str | None = None) -> dict[str, int]:
    """Send once when the renewal window opens, then escalate once if still open."""
    supplied_today = today
    sent = escalated = 0
    now = datetime.now(timezone.utc)
    for task in store.due_tasks(today, tenant_id=tenant_id):
        local_today = supplied_today or now.astimezone(ZoneInfo(task["notice_timezone"] or "UTC")).date()
        if date.fromisoformat(task["due_date"]) > local_today:
            continue
        if not task["reminder_sent_at"]:
            channel = _send_and_record(store, task, escalation=False)
            store.mark_notified(task["id"], escalation=False, channel=channel)
            sent += 1

    delay = max(0, int(os.getenv("ESCALATION_AFTER_DAYS", "7")))
    cutoff = supplied_today - timedelta(days=delay) if supplied_today is not None else None
    for task in store.open_for_escalation(cutoff, tenant_id=tenant_id):
        if supplied_today is None:
            zone = ZoneInfo(task["notice_timezone"] or "UTC")
            sent_at = datetime.fromisoformat(task["reminder_sent_at"])
            if (now.astimezone(zone).date() - sent_at.astimezone(zone).date()).days < delay:
                continue
        channel = _send_and_record(store, task, escalation=True)
        store.mark_notified(task["id"], escalation=True, channel=channel)
        escalated += 1
    return {"reminders_sent": sent, "escalations_sent": escalated}


def _send_and_record(store: Store, task, escalation: bool) -> str:
    notification_type = "escalation" if escalation else "reminder"
    channel = delivery_channel(task, escalation)
    started = time.perf_counter()
    try:
        channel = send_notification(task, escalation=escalation)
    except Exception as exc:
        store.record_notification_delivery(
            task["id"], channel, notification_type, "failed",
            round((time.perf_counter() - started) * 1000), f"{type(exc).__name__}: {exc}",
        )
        raise
    store.record_notification_delivery(
        task["id"], channel, notification_type, "sent", round((time.perf_counter() - started) * 1000),
    )
    return channel
