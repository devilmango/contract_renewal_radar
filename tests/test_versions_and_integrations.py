import base64
from datetime import date
from email import message_from_bytes

from renewal_radar import notifications
from renewal_radar.api import create_app
from renewal_radar.auth import AuthRegistry, Principal
from renewal_radar.schemas import ContractData, ConfirmationRequest
from renewal_radar.store import Store


def _confirm(store, contract_id, terms, tenant="acme"):
    store.save_contract(contract_id, f"{contract_id}.pdf", "rules", terms, "redacted source", "reviewer", tenant)
    return store.confirm_contract(contract_id, terms, "reviewer", tenant)


def test_amendment_lineage_reports_changed_terms_and_is_tenant_scoped(tmp_path):
    store = Store(str(tmp_path / "versions.db"))
    original = ContractData(
        contract="Support Services", start_date=date(2025, 1, 1),
        expiration_date=date(2026, 1, 1), renewal_notice_days=60, auto_renew=True,
    )
    _confirm(store, "original", original)
    amendment = ContractData(
        contract="Support Services", start_date=date(2025, 1, 1),
        expiration_date=date(2027, 1, 1), renewal_notice_days=90, auto_renew=True,
        supersedes_contract_id="original",
    )
    _confirm(store, "amendment", amendment)
    second_amendment = ContractData(
        contract="Support Services", start_date=date(2025, 1, 1),
        expiration_date=date(2028, 1, 1), renewal_notice_days=120, auto_renew=True,
        supersedes_contract_id="amendment",
    )
    _confirm(store, "second-amendment", second_amendment)

    versions = store.list_contract_versions("original", "acme")
    assert [version["contract_id"] for version in versions] == ["original", "amendment", "second-amendment"]
    assert versions[1]["supersedes_contract_id"] == "original"
    assert versions[1]["changes_from_previous"] == {
        "expiration_date": {"from": "2026-01-01", "to": "2027-01-01"},
        "renewal_notice_days": {"from": 60, "to": 90},
    }
    assert versions[2]["changes_from_previous"]["renewal_notice_days"] == {"from": 90, "to": 120}
    assert [version["contract_id"] for version in store.list_contract_versions("amendment", "acme")] == [
        "original", "amendment", "second-amendment",
    ]
    assert store.get_contract("original", "acme")["status"] == "superseded"
    assert store.list_contract_versions("amendment", "other") == []


def test_review_confirmation_queues_calendar_sync_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("CALENDAR_PROVIDER", "google")
    store = Store(str(tmp_path / "calendar-auto.db"))
    terms = ContractData(contract="Calendar Agreement", expiration_date=date(2027, 1, 1), renewal_notice_days=30)
    store.save_contract("calendar-contract", "contract.pdf", "rules", terms, "source", "reviewer", "acme")
    app = create_app(store, AuthRegistry([]))
    route = next(route for route in app.routes if route.path == "/contracts/{contract_id}/confirm")
    route.endpoint("calendar-contract", ConfirmationRequest(contract=terms), Principal("reviewer", frozenset({"reviewer"}), tenant_id="acme"))

    jobs = store.list_jobs("acme")
    assert len(jobs) == 1
    assert jobs[0]["job_type"] == "calendar.sync"


def test_gmail_delivery_uses_gmail_api_and_preserves_message_id(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("GMAIL_ACCESS_TOKEN", "test-token")
    calls = []
    monkeypatch.setattr(notifications, "_provider_post", lambda *args: calls.append(args))
    task = {"id": "task-1", "owner_email": "owner@example.com", "title": "Renewal review", "due_date": "2027-01-01", "expiration_date": "2027-04-01"}

    assert notifications.send_notification(task, escalation=False) == "email"
    assert notifications.delivery_channel(task, escalation=False) == "email"
    url, token, payload = calls[0]
    assert url.endswith("/users/me/messages/send") and token == "test-token"
    raw = payload["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    message = message_from_bytes(decoded)
    assert message["To"] == "owner@example.com"
    assert message["Message-ID"].startswith("<")


def test_microsoft_delivery_uses_graph_sendmail(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "microsoft")
    monkeypatch.setenv("MS_GRAPH_USER_ID", "radar@example.com")
    monkeypatch.setattr(notifications, "graph_access_token", lambda: "graph-token")
    calls = []
    monkeypatch.setattr(notifications, "_provider_post", lambda *args: calls.append(args))
    task = {"id": "task-2", "owner_email": "owner@example.com", "title": "Renewal review", "due_date": "2027-01-01", "expiration_date": "2027-04-01"}

    assert notifications.send_notification(task, escalation=True) == "email"
    url, token, payload = calls[0]
    assert url.endswith("/users/radar@example.com/sendMail") and token == "graph-token"
    assert payload["message"]["toRecipients"][0]["emailAddress"]["address"] == "owner@example.com"
    assert payload["message"]["subject"].startswith("Escalation:")
