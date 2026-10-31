from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from renewal_radar import notifications
from renewal_radar.api import create_app
from renewal_radar.auth import AuthRegistry, Principal
from renewal_radar.reminders import run_reminders
from renewal_radar.schemas import ContractData, EscalationPolicy, IntegrationConnectionRequest
from renewal_radar.store import Store


def _task(store, contract_id="contract", tenant="acme", owner="owner@example.com"):
    terms = ContractData(contract="Cloud services", expiration_date=date(2027, 1, 1),
                         renewal_notice_days=30, annual_contract_value=Decimal("1200"), currency="USD")
    store.save_contract(contract_id, "agreement.pdf", "rules", terms, "redacted source", "reviewer", tenant)
    task_id = store.confirm_contract(contract_id, terms, "reviewer", tenant)
    with store.connect() as db:
        db.execute("UPDATE tasks SET owner_email=? WHERE id=?", (owner, task_id))
    return task_id


def test_notification_preferences_route_and_audit_slack_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", "11" * 32)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    store = Store(str(tmp_path / "notifications.db"))
    task_id = _task(store)
    policy = EscalationPolicy(reminder_days_before=[0], reminder_channels=["slack"],
                              reminder_recipients=["audit@example.com"])
    store.save_tenant_policy("acme", policy.model_dump(mode="json"), "admin")
    from renewal_radar.integrations import encrypt_credentials
    ciphertext = encrypt_credentials("acme", "slack", {"webhook_url": "https://hooks.slack.com/services/T/B/X"})
    store.save_integration_connection("acme", "slack", "slack_webhook", {}, ciphertext, "admin")
    calls = []
    monkeypatch.setattr(notifications, "_post_webhook", lambda url, payload, channel: calls.append((url, payload, channel)))

    result = run_reminders(store, today=date(2026, 12, 2), tenant_id="acme")

    assert result["reminders_sent"] == 1
    assert calls[0][2] == "slack"
    assert calls[0][1]["text"].startswith("*Reminder:")
    deliveries = store.operations_snapshot("acme")["notifications"]["recent"]
    assert deliveries[0]["channel"] == "slack"
    assert deliveries[0]["recipient"] is None
    assert store.task_notification_keys(task_id, "acme") == {"reminder:0:slack:channel"}
    assert any(event["event_type"] == "notification.sent" for event in store.audit("contract", "acme"))


def test_webhook_hosts_are_restricted_and_preferences_validate_recipients():
    with pytest.raises(ValueError, match="approved HTTPS"):
        notifications.validate_webhook_url("http://hooks.slack.com/services/x", "slack")
    with pytest.raises(ValueError, match="approved HTTPS"):
        notifications.validate_webhook_url("https://attacker.example/hook", "teams")
    with pytest.raises(ValidationError, match="valid email"):
        EscalationPolicy(reminder_recipients=["not-an-email"])
    with pytest.raises(ValidationError, match="unique"):
        EscalationPolicy(reminder_channels=["email", "email"])


def test_failed_webhook_attempt_is_written_to_operations_and_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", "33" * 32)
    store = Store(str(tmp_path / "failed-notification.db"))
    _task(store)
    store.save_tenant_policy("acme", EscalationPolicy(reminder_days_before=[0], reminder_channels=["teams"]).model_dump(mode="json"), "admin")
    from renewal_radar.integrations import encrypt_credentials
    encrypted = encrypt_credentials("acme", "teams", {"webhook_url": "https://tenant.webhook.office.com/webhook/secret"})
    store.save_integration_connection("acme", "teams", "teams_webhook", {}, encrypted, "admin")

    def fail_delivery(*_args):
        raise OSError("unavailable")
    monkeypatch.setattr(notifications, "_post_webhook", fail_delivery)
    with pytest.raises(OSError, match="unavailable"):
        run_reminders(store, today=date(2026, 12, 2), tenant_id="acme")

    attempt = store.operations_snapshot("acme")["notifications"]["recent"][0]
    assert attempt["channel"] == "teams" and attempt["status"] == "failed"
    assert attempt["error_message"] == "OSError: unavailable"
    assert any(event["event_type"] == "notification.failed" for event in store.audit("contract", "acme"))


def test_admin_can_save_and_test_encrypted_collaboration_webhook(tmp_path, monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", "22" * 32)
    store = Store(str(tmp_path / "webhook-api.db"))
    app = create_app(store, AuthRegistry([]))
    put = next(r.endpoint for r in app.routes if r.path == "/integrations/connections/{integration}" and "PUT" in r.methods)
    check = next(r.endpoint for r in app.routes if r.path == "/integrations/connections/{integration}/check")
    list_connections = next(r.endpoint for r in app.routes if r.path == "/integrations/connections" and "GET" in r.methods)
    admin = Principal("admin", frozenset({"admin"}), tenant_id="acme")
    request = IntegrationConnectionRequest(provider="slack_webhook", credentials={
        "webhook_url": "https://hooks.slack.com/services/T/B/secret",
    })
    saved = put("slack", request, admin)
    assert saved["credentials_configured"] is True
    assert "secret" not in str(list_connections(admin))
    posted = []
    monkeypatch.setattr(notifications, "_post_webhook", lambda url, payload, channel: posted.append(channel))
    assert check("slack", admin)["status"] == "healthy"
    assert posted == ["slack"]
    unsafe = IntegrationConnectionRequest(provider="teams_webhook", credentials={"webhook_url": "https://attacker.example/incoming"})
    with pytest.raises(HTTPException) as rejected:
        put("teams", unsafe, admin)
    assert rejected.value.status_code == 422


def test_portfolio_performance_filters_and_rolls_up_compliance_and_approvals(tmp_path):
    store = Store(str(tmp_path / "performance.db"))
    task_id = _task(store, owner="owner@example.com")
    supplier = store.create_supplier("Acme Cloud", "reviewer", "acme")
    store.assign_contract_supplier("contract", supplier["id"], "reviewer", "acme")
    store.record_renewal_outcome(task_id, {
        "outcome": "renegotiated", "actual_annual_value": Decimal("1000"),
        "annual_savings": Decimal("200"), "currency": "USD", "actual_expiration_date": date(2028, 1, 1),
    }, "reviewer", "acme")

    obligation = store.create_contract_obligation(
        "contract", {"category": "insurance_certificate", "title": "Insurance proof", "due_date": date(2026, 6, 1),
                     "evidence": {"field": "insurance", "quote": "Provide proof annually."}},
        "Owner", "owner@example.com", "reviewer", "acme",
    )
    completed_obligation = store.create_contract_obligation(
        "contract", {"category": "audit_right", "title": "Audit report", "due_date": date(2026, 8, 1),
                     "evidence": {"field": "audit", "quote": "Submit an annual audit report."}},
        "Owner", "owner@example.com", "reviewer", "acme",
    )
    submitted = store.submit_obligation_evidence(completed_obligation["id"], "acme", "owner", "audit.txt", "text/plain",
                                                  "YXVkaXQ=", "audit-digest", None)
    store.review_obligation_evidence(submitted["id"], "acme", "reviewer", "approved", "Verified")
    pending = store.submit_obligation_evidence(obligation["id"], "acme", "owner", "proof.txt", "text/plain",
                                               "cHJvb2Y=", "digest", None)
    stale = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    with store.connect() as db:
        db.execute("UPDATE obligation_evidence SET created_at=? WHERE id=?", (stale, pending["id"]))
        db.execute("UPDATE tasks SET due_date=? WHERE id=?", ((date.today() - timedelta(days=1)).isoformat(), obligation["task_id"]))

    report = store.portfolio_performance("acme", start_date=date.today() - timedelta(days=30),
                                         end_date=date.today(), supplier_id=supplier["id"],
                                         owner_email="OWNER@example.com", sla_days=3, as_of=date.today())
    assert report["summary"]["renewal_outcomes_by_type"] == {"renegotiated": 1}
    assert report["summary"]["annual_savings_by_currency"] == {"USD": "200.00"}
    assert report["summary"]["obligations_completed"] == 1
    assert report["summary"]["overdue_open_obligations"] == 1
    assert report["summary"]["overdue_signoffs"] == 1
    assert report["summary"]["pending_signoffs"] == 1
    assert report["summary"]["approval_decisions"] == 1
    assert report["summary"]["mean_approval_turnaround_days"] is not None
    assert report["monthly"][0]["renewal_outcomes"] == 1
    assert report["monthly"][0]["obligations_completed"] == 1
    assert report["monthly"][0]["approval_decisions"] == 1

    filtered = store.portfolio_performance("acme", supplier_id="other-supplier")
    assert filtered["summary"]["renewal_outcome_count"] == 0


def test_performance_api_exposes_dashboard_and_validates_date_range(tmp_path):
    app = create_app(Store(str(tmp_path / "performance-api.db")), AuthRegistry([]))
    route = next(r.endpoint for r in app.routes if r.path == "/portfolio/performance/summary")
    admin = Principal("admin", frozenset({"admin"}), tenant_id="acme")
    with pytest.raises(Exception, match="end_date must be on or after"):
        route(date(2026, 2, 1), date(2026, 1, 1), None, None, admin)
    assert any(r.path == "/portfolio/performance" for r in app.routes)
