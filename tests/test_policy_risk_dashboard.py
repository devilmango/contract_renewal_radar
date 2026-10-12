from datetime import date

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from renewal_radar.api import create_app
from renewal_radar.auth import AuthRegistry, Principal
from renewal_radar.reminders import run_reminders
from renewal_radar.schemas import ContractData, EscalationPolicy
from renewal_radar.store import Store


def _confirmed_task(store, contract_id, expiration, notice_days, tenant, owner_email=None):
    terms = ContractData(
        contract=contract_id, expiration_date=expiration,
        renewal_notice_days=notice_days, auto_renew=True, owner_email=owner_email,
    )
    store.save_contract(contract_id, f"{contract_id}.pdf", "rules", terms, "redacted text", "reviewer", tenant)
    return store.confirm_contract(contract_id, terms, "reviewer", tenant)


def test_tenant_policy_supports_multiple_reminder_dates_and_escalation_tiers(tmp_path, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    store = Store(str(tmp_path / "policies.db"))
    policy = EscalationPolicy(
        reminder_days_before=[30, 7, 0],
        escalation_steps=[
            {"after_due_days": 0, "recipient_email": "manager@example.com", "label": "Manager review"},
            {"after_due_days": 5, "recipient_email": "legal@example.com", "label": "Legal leadership"},
        ],
    )
    store.save_tenant_policy("acme", policy.model_dump(mode="json"), "admin")
    _confirmed_task(store, "multi-stage", date(2027, 5, 1), 30, "acme", "owner@example.com")
    sent = []

    def fake_send(task, escalation, recipient_email=None, escalation_label=None):
        sent.append((escalation, recipient_email, escalation_label))
        return "log"

    monkeypatch.setattr("renewal_radar.reminders.send_notification", fake_send)
    assert run_reminders(store, date(2027, 3, 2), "acme") == {"reminders_sent": 1, "escalations_sent": 0}
    assert run_reminders(store, date(2027, 3, 2), "acme") == {"reminders_sent": 0, "escalations_sent": 0}
    assert run_reminders(store, date(2027, 3, 25), "acme") == {"reminders_sent": 1, "escalations_sent": 0}
    assert run_reminders(store, date(2027, 4, 1), "acme") == {"reminders_sent": 1, "escalations_sent": 1}
    assert run_reminders(store, date(2027, 4, 6), "acme") == {"reminders_sent": 0, "escalations_sent": 1}
    assert sent == [
        (False, None, None), (False, None, None), (False, None, None),
        (True, "manager@example.com", "Manager review"),
        (True, "legal@example.com", "Legal leadership"),
    ]


def test_escalation_policy_validation_rejects_duplicate_schedule_entries():
    with pytest.raises(ValidationError, match="unique"):
        EscalationPolicy(reminder_days_before=[7, 7])
    with pytest.raises(ValidationError, match="unique"):
        EscalationPolicy(escalation_steps=[{"after_due_days": 3}, {"after_due_days": 3}])


def test_backup_owner_receives_reminder_when_task_has_no_owner(tmp_path, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    store = Store(str(tmp_path / "backup-owner.db"))
    policy = EscalationPolicy(reminder_days_before=[0], backup_owner_email="backup@example.com")
    store.save_tenant_policy("acme", policy.model_dump(mode="json"), "admin")
    _confirmed_task(store, "unassigned", date(2027, 1, 31), 0, "acme")
    recipients = []
    monkeypatch.setattr(
        "renewal_radar.reminders.send_notification",
        lambda _task, **kwargs: recipients.append(kwargs.get("recipient_email")) or "log",
    )

    assert run_reminders(store, date(2027, 1, 31), "acme")["reminders_sent"] == 1
    assert recipients == ["backup@example.com"]


def test_policy_api_is_tenant_scoped_and_admin_managed(tmp_path):
    store = Store(str(tmp_path / "policy-api.db"))
    app = create_app(store, AuthRegistry([]))
    get_policy = next(r.endpoint for r in app.routes if r.path == "/escalation-policy" and "GET" in r.methods)
    put_policy = next(r.endpoint for r in app.routes if r.path == "/escalation-policy" and "PUT" in r.methods)
    admin = Principal("radar-admin", frozenset({"admin"}), tenant_id="tenant-a")
    reviewer = Principal("legal-reviewer", frozenset({"reviewer"}), tenant_id="tenant-b")
    request = EscalationPolicy(reminder_days_before=[45, 14], escalation_steps=[])

    with pytest.raises(HTTPException) as forbidden:
        put_policy(request, reviewer)
    assert forbidden.value.status_code == 403
    saved = put_policy(request, admin)
    assert saved["source"] == "tenant"
    assert get_policy(reviewer)["source"] == "default"
    assert get_policy(admin)["policy"]["reminder_days_before"] == [45, 14]


def test_risk_dashboard_counts_and_orders_only_tenant_records(tmp_path):
    store = Store(str(tmp_path / "risk.db"))
    _confirmed_task(store, "overdue-unassigned", date(2026, 1, 15), 30, "tenant-a")
    _confirmed_task(store, "upcoming-owned", date(2026, 2, 20), 30, "tenant-a", "owner@example.com")
    _confirmed_task(store, "other-tenant", date(2026, 1, 15), 30, "tenant-b")
    store.save_contract(
        "awaiting-review", "review.pdf", "rules",
        ContractData(contract="Pending Vendor", expiration_date=date(2026, 8, 1)),
        "source", "reviewer", "tenant-a",
    )

    snapshot = store.renewal_risk_snapshot("tenant-a", date(2026, 1, 1))
    assert snapshot["summary"] == {
        "pending_review_contracts": 1, "overdue_tasks": 1, "unassigned_open_tasks": 1,
        "renewals_due_30_days": 1, "expirations_next_90_days": 2, "high_risk_tasks": 1,
        "annual_spend_at_risk": {}, "annual_spend_high_risk": {},
    }
    assert snapshot["tasks"][0]["contract_id"] == "overdue-unassigned"
    assert snapshot["tasks"][0]["risk_level"] == "high"
    assert len(snapshot["tasks"]) == 2
    app = create_app(store, AuthRegistry([]))
    summary = next(route.endpoint for route in app.routes if route.path == "/renewal-risk/summary")
    admin = Principal("radar-admin", frozenset({"admin"}), tenant_id="tenant-a")
    owner = Principal("task-owner", frozenset({"owner"}), email="owner@example.com", tenant_id="tenant-a")
    assert summary(date(2026, 1, 1), admin)["summary"] == snapshot["summary"]
    with pytest.raises(HTTPException) as forbidden:
        summary(date(2026, 1, 1), owner)
    assert forbidden.value.status_code == 403


def test_dashboard_routes_are_available(tmp_path):
    app = create_app(Store(str(tmp_path / "dashboard.db")), AuthRegistry([]))
    routes = {(route.path, tuple(sorted(route.methods or ()))) for route in app.routes}
    assert ("/renewal-risk", ("GET",)) in routes
    assert ("/renewal-risk/summary", ("GET",)) in routes
    assert ("/escalation-policy", ("GET",)) in routes
    assert ("/escalation-policy", ("PUT",)) in routes
