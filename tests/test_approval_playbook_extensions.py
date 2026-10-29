from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from renewal_radar.api import create_app
import pytest
from fastapi import HTTPException
from renewal_radar.auth import AuthRegistry, Principal
from renewal_radar.reminders import run_approval_reminders
from renewal_radar.schemas import ApprovalPlaybookRequest, ContractData
from renewal_radar.store import Store


def _contract(store, contract_id="c1", tenant="acme", terms=None, text="Original service term ends December 31, 2027."):
    terms = terms or ContractData(
        contract="Northstar Cloud Services", start_date=date(2025, 1, 1),
        expiration_date=date(2027, 12, 31), renewal_notice_days=90,
        annual_contract_value=Decimal("250000"), currency="USD",
    )
    store.save_contract(contract_id, f"{contract_id}.pdf", "rules", terms, text, "reviewer", tenant)
    if terms.expiration_date:
        return store.confirm_contract(contract_id, terms, "reviewer", tenant)


def _route(app, path, method):
    return next(route.endpoint for route in app.routes if route.path == path and method in route.methods)


def test_approval_inbox_sla_notifications_and_delivery_audit(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "approval-inbox.db"))
    task_id = _contract(store)
    obligation = store.create_contract_obligation(
        "c1", {"category": "insurance_certificate", "title": "Insurance proof", "due_date": date(2027, 6, 1),
               "evidence": {"field": "obligation:insurance_certificate", "quote": "Provide insurance proof."}},
        "Owner", "owner@example.com", "reviewer", "acme",
    )
    proof = store.submit_obligation_evidence(
        obligation["id"], "acme", "owner", "proof.txt", "text/plain", "cHJvb2Y=", "digest", None,
    )
    playbook = store.create_renewal_playbook(
        {"name": "High value", "min_contract_value": "100000", "currency": "USD",
         "required_approvers": ["legal@example.com"], "checklist": []}, "reviewer", "acme",
    )
    review = store.apply_renewal_playbook(playbook["id"], task_id, "acme", "reviewer", [])
    stale = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    with store.connect() as db:
        db.execute("UPDATE obligation_evidence SET created_at=? WHERE id=?", (stale, proof["id"]))
        db.execute("UPDATE task_playbook_reviews SET applied_at=? WHERE id=?", (stale, review["id"]))

    inbox = store.approval_inbox("acme", sla_days=3, today=date.today())
    assert {item["type"] for item in inbox["items"]} == {"obligation_signoff", "playbook_decision"}
    assert all(item["sla_breached"] and item["age_days"] == 4 for item in inbox["items"])

    monkeypatch.setenv("APPROVAL_ESCALATION_EMAIL", "lead@example.com")
    monkeypatch.setattr("renewal_radar.reminders.send_notification", lambda *args, **kwargs: "email")
    first = run_approval_reminders(store, today=date.today(), tenant_id="acme")
    second = run_approval_reminders(store, today=date.today(), tenant_id="acme")
    assert first == {"reminders_sent": 0, "escalations_sent": 2}
    assert second == {"reminders_sent": 0, "escalations_sent": 0}
    deliveries = store.approval_inbox("acme")["deliveries"]
    assert len(deliveries) == 2
    assert {row["recipient"] for row in deliveries} == {"lead@example.com"}
    assert all(row["status"] == "sent" for row in deliveries)
    assert len([row for row in store.audit("c1", "acme") if row["event_type"] == "approval.notification_sent"]) == 2


def test_playbook_edits_keep_decision_snapshot_and_simulation_explains_route(tmp_path):
    store = Store(str(tmp_path / "playbook-versions.db"))
    task_id = _contract(store)
    playbook = store.create_renewal_playbook(
        {"name": "Value review", "min_contract_value": "100000", "currency": "USD",
         "supplier_risk_levels": [], "changed_clause_categories": [],
         "required_approvers": ["legal@example.com"], "checklist": ["Read the renewal terms"]},
        "reviewer", "acme",
    )
    review = store.apply_renewal_playbook(playbook["id"], task_id, "acme", "reviewer", [])
    assert review["playbook_snapshot"]["version"] == 1

    updated = store.edit_renewal_playbook(playbook["id"], {
        "name": "Value review", "min_contract_value": "200000", "currency": "USD",
        "supplier_risk_levels": ["high"], "changed_clause_categories": ["pricing"],
        "required_approvers": ["legal@example.com", "finance@example.com"], "checklist": ["Check cost impact"],
    }, "admin", "acme")
    assert updated["version"] == 2
    assert review["playbook_snapshot"]["required_approvers"] == ["legal@example.com"]
    history = store.renewal_playbook_history(playbook["id"], "acme")
    assert [entry["version"] for entry in history] == [1, 1, 2]

    simulation = store.simulate_renewal_playbooks("acme")
    route = next(item for item in simulation["routes"] if item["task_id"] == task_id)
    value_decision = next(item for item in route["matches"] if item["playbook_id"] == playbook["id"])
    assert not value_decision["matched"]
    assert "supplier risk does not match" in value_decision["unmatched_reasons"]
    assert route["route_name"] is None

    retired = store.edit_renewal_playbook(playbook["id"], {}, "admin", "acme", retire=True)
    assert retired["status"] == "retired"
    assert store.list_renewal_playbooks("acme") == []
    assert store.list_renewal_playbooks("acme", include_retired=True)[0]["status"] == "retired"


def test_amendment_pair_suggestion_and_clause_changes_are_automatic(tmp_path):
    store = Store(str(tmp_path / "automatic-amendment.db"))
    _contract(store)
    amendment = ContractData(
        contract="Northstar Cloud Services Amendment No. 1", start_date=date(2025, 1, 1),
        expiration_date=date(2028, 12, 31), renewal_notice_days=60,
        annual_contract_value=Decimal("270000"), currency="USD",
    )
    store.save_contract("amendment", "amendment.pdf", "rules", amendment,
                        "Amendment. The service term now expires December 31, 2028. Renewal requires 60 days notice.", "reviewer", "acme")
    candidates = store.amendment_candidates("amendment", "acme")
    assert candidates[0]["contract_id"] == "c1"
    assert candidates[0]["automatic_match"] is True
    linked = amendment.model_copy(update={"supersedes_contract_id": "c1"})
    store.confirm_contract("amendment", linked, "reviewer", "acme")
    task = store.list_tasks(status="open", kind="renewal", tenant_id="acme")[0]
    categories = store.detect_task_changed_clause_categories(task["id"], "acme")
    assert {"expiration", "notice_period", "pricing"}.issubset(categories)


def test_approval_inbox_api_and_playbook_edit_routes_are_reviewer_scoped(tmp_path):
    store = Store(str(tmp_path / "approval-api.db"))
    app = create_app(store, AuthRegistry([]))
    inbox = _route(app, "/approvals/inbox", "GET")
    edit = _route(app, "/renewal-playbooks/{playbook_id}", "PUT")
    reviewer = Principal("reviewer", frozenset({"reviewer"}), "legal@example.com", "acme")
    assert inbox(reviewer)["items"] == []
    with pytest.raises(HTTPException) as missing:
        edit("missing", ApprovalPlaybookRequest(name="x", required_approvers=["legal@example.com"]), reviewer)
    assert missing.value.status_code == 404
