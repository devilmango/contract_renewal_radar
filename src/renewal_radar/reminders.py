from __future__ import annotations

import time
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .notifications import delivery_channel, send_notification
from .policies import effective_escalation_policy
from .store import Store


def run_reminders(store: Store, today: date | None = None, tenant_id: str | None = None) -> dict[str, int]:
    """Run each tenant's configured reminder schedule and escalation tiers."""
    supplied_today = today
    sent = escalated = 0
    now = datetime.now(timezone.utc)
    policy_cache: dict[str, dict] = {}
    if tenant_id:
        policy_cache[tenant_id] = effective_escalation_policy(store, tenant_id)
        max_offset = max(policy_cache[tenant_id]["reminder_days_before"], default=0)
    else:
        max_offset = 3650
    tasks = store.due_tasks(today, tenant_id=tenant_id, max_days_before=max_offset)
    for task in tasks:
        local_today = supplied_today or now.astimezone(ZoneInfo(task["notice_timezone"] or "UTC")).date()
        due = date.fromisoformat(task["due_date"])
        task_tenant = task["tenant_id"]
        if task_tenant not in policy_cache:
            policy_cache[task_tenant] = effective_escalation_policy(store, task_tenant)
        policy = policy_cache[task_tenant]
        sent_keys = store.task_notification_keys(task["id"], task_tenant)
        if task["reminder_sent_at"] and not any(key.startswith("reminder:") for key in sent_keys):
            sent_keys.add("reminder:0")  # Preserve the legacy single-reminder state after upgrade.

        for offset in policy["reminder_days_before"]:
            action_key = f"reminder:{offset}"
            if local_today < due - timedelta(days=offset) or action_key in sent_keys:
                continue
            backup = policy.get("backup_owner_email") if not task["owner_email"] else None
            channel = _send_and_record(store, task, escalation=False, recipient_email=backup)
            if store.record_task_notification(task["id"], task_tenant, action_key, channel):
                store.mark_notified(task["id"], escalation=False, channel=channel)
                sent_keys.add(action_key)
                sent += 1

        existing_escalations = {key for key in sent_keys if key.startswith("escalation:")}
        for index, step in enumerate(policy["escalation_steps"]):
            action_key = f"escalation:{step['after_due_days']}"
            if local_today < due + timedelta(days=step["after_due_days"]) or action_key in sent_keys:
                continue
            if task["escalated_at"] and not existing_escalations and index == 0:
                # Do not resend an escalation that was sent before policy scheduling was introduced.
                store.record_task_notification(task["id"], task_tenant, action_key, "legacy")
                sent_keys.add(action_key)
                continue
            channel = _send_and_record(
                store, task, escalation=True,
                recipient_email=step.get("recipient_email") or (policy.get("backup_owner_email") if not task["owner_email"] else None),
                escalation_label=step.get("label"),
            )
            if store.record_task_notification(task["id"], task_tenant, action_key, channel):
                store.mark_notified(task["id"], escalation=True, channel=channel)
                sent_keys.add(action_key)
                existing_escalations.add(action_key)
                escalated += 1
    approval_counts = run_approval_reminders(store, today= supplied_today or now.date(), tenant_id=tenant_id)
    return {"reminders_sent": sent + approval_counts["reminders_sent"],
            "escalations_sent": escalated + approval_counts["escalations_sent"]}


def run_approval_reminders(store: Store, today: date | None = None, tenant_id: str | None = None) -> dict[str, int]:
    """Notify reviewers about outstanding evidence and playbook decisions."""
    as_of = today or date.today()
    sla_days = max(1, int(os.getenv("APPROVAL_SLA_DAYS", "3")))
    tenants = [tenant_id] if tenant_id else _tenants_with_approvals(store)
    sent = escalated = 0
    for tenant in tenants:
        queue = store.approval_inbox(tenant, sla_days=sla_days, today=as_of)
        for item in queue["items"]:
            if item["age_days"] < 1:
                continue
            is_escalation = item["age_days"] >= sla_days
            configured = [email.strip() for email in os.getenv("APPROVAL_REVIEWER_EMAILS", "").split(",") if email.strip()]
            recipients = item["reviewers"] or configured
            if is_escalation:
                escalation = os.getenv("APPROVAL_ESCALATION_EMAIL", os.getenv("ESCALATION_EMAIL", "")).strip()
                if escalation:
                    recipients = [escalation]
            if not recipients:
                recipients = [None]
            action = "escalation" if is_escalation else "reminder"
            for recipient in recipients:
                action_key = f"{action}:{as_of.isoformat()}:{(recipient or 'log').casefold()}"
                task = store.get_task(item["task_id"], tenant)
                if not task:
                    continue
                task = dict(task)
                task["title"] = f"Approval {action}: {item['subject']} ({item['type'].replace('_', ' ')})"
                task["id"] = f"{task['id']}:{item['target_id']}"
                task["owner_email"] = recipient
                task["task_kind"] = "obligation" if item["type"] == "obligation_signoff" else "renewal"
                task["approval_type"] = item["type"]
                task["tenant_id"] = tenant
                channel = delivery_channel(task, is_escalation, recipient, store=store)
                started = time.perf_counter()
                status, error = "sent", None
                try:
                    send_notification(task, escalation=is_escalation, recipient_email=recipient,
                                      escalation_label="approval SLA breached" if is_escalation else None, store=store)
                except Exception as exc:
                    status, error = "failed", f"{type(exc).__name__}: {exc}"
                inserted = store.record_approval_notification(
                    tenant, item["type"], item["target_id"], item["task_id"], action_key, recipient,
                    channel, status, round((time.perf_counter() - started) * 1000), error,
                )
                if inserted and status == "sent":
                    if is_escalation:
                        escalated += 1
                    else:
                        sent += 1
    return {"reminders_sent": sent, "escalations_sent": escalated}


def _tenants_with_approvals(store: Store) -> list[str]:
    with store.connect() as db:
        rows = db.execute(
            "SELECT DISTINCT tenant_id FROM obligation_evidence WHERE status='pending_signoff' "
            "UNION SELECT DISTINCT tenant_id FROM task_playbook_reviews WHERE status='blocked'"
        ).fetchall()
    return [row["tenant_id"] for row in rows]


def _send_and_record(
    store: Store, task, escalation: bool, recipient_email: str | None = None,
    escalation_label: str | None = None,
) -> str:
    notification_type = "escalation" if escalation else "reminder"
    connection = store.get_integration_connection(task["tenant_id"], "email")
    channel = delivery_channel(task, escalation, recipient_email, store=store)
    started = time.perf_counter()
    try:
        options = {"escalation": escalation, "recipient_email": recipient_email, "escalation_label": escalation_label}
        if connection:
            options["store"] = store
        channel = send_notification(task, **options)
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
