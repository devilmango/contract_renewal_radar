import json
import sys
from datetime import date

import pytest
from fastapi import HTTPException

from renewal_radar.api import create_app
from renewal_radar.auth import AuthRegistry, Principal
from renewal_radar.cli import main
from renewal_radar.evaluation import load_cases
from renewal_radar.reminders import run_reminders
from renewal_radar.schemas import ContractData, EvaluationFeedbackApprovalRequest, Evidence
from renewal_radar.store import Store


def endpoint(app, path, method):
    return next(route.endpoint for route in app.routes if route.path == path and method in route.methods)


def make_confirmed_contract(store, tenant="ops-tenant"):
    contract_id = f"feedback-{tenant}"
    terms = ContractData(
        contract="Supplier Services Agreement", start_date=date(2025, 1, 1),
        expiration_date=date(2026, 1, 1), renewal_notice_days=30, auto_renew=True,
    )
    store.save_contract(contract_id, "agreement.pdf", "openai", terms, "Original contract text", "reviewer", tenant)
    reviewed = terms.model_copy(update={"expiration_date": date(2026, 2, 1)})
    store.confirm_contract(contract_id, reviewed, "reviewer", tenant)
    return contract_id


def test_notification_failures_and_job_health_appear_as_alerts(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "ops.db"))
    tenant = "ops-tenant"
    contract_id = "ops-contract"
    terms = ContractData(contract="Supplier Agreement", expiration_date=date(2026, 1, 1), renewal_notice_days=30)
    store.save_contract(contract_id, "agreement.pdf", "rules", terms, "source", "reviewer", tenant)
    task_id = store.confirm_contract(contract_id, terms, "reviewer", tenant)
    monkeypatch.setattr("renewal_radar.reminders.send_notification", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("SMTP unavailable")))

    for _ in range(3):
        with pytest.raises(OSError, match="SMTP unavailable"):
            run_reminders(store, today=date(2026, 1, 1), tenant_id=tenant)

    stuck = store.enqueue_job(tenant, "calendar.sync", {"tenant_id": tenant}, "dead-calendar", max_attempts=1)
    claimed = store.claim_job("operations-worker", job_id=stuck["id"])
    assert claimed["id"] == stuck["id"]
    assert store.fail_job(stuck["id"], "operations-worker", "provider unavailable", retry_delay_seconds=0)

    app = create_app(store, AuthRegistry([]))
    health = endpoint(app, "/operations/health", "GET")(
        stuck_after_minutes=15,
        principal=Principal("reviewer", frozenset({"reviewer"}), tenant_id=tenant),
    )
    assert health["status"] == "degraded"
    assert health["jobs"]["by_status"]["dead"] == 1
    assert health["notifications"]["failed_last_hour"] == 3
    assert any(alert["code"] == "repeated_notification_failures" for alert in health["alerts"])
    assert any(alert["code"] == "dead_jobs" for alert in health["alerts"])
    assert health["notifications"]["recent"][0]["task_id"] == task_id


def test_confirmed_review_corrections_require_governance_before_export(tmp_path, monkeypatch):
    database = tmp_path / "feedback.db"
    store = Store(str(database))
    tenant = "feedback-tenant"
    contract_id = make_confirmed_contract(store, tenant)
    captured = store.get_review_feedback(contract_id, tenant)
    corrections = json.loads(captured["corrections_json"])
    assert corrections["expiration_date"] == {"proposed": "2026-01-01", "reviewed": "2026-02-01"}
    assert captured["status"] == "captured"

    redacted = "Supplier Services Agreement Effective Date: January 1, 2025. Expiration Date: February 1, 2026."
    request = EvaluationFeedbackApprovalRequest(
        redacted_text=redacted,
        approval_reference="LEGAL-APPROVAL-42",
        fields=["start_date", "expiration_date"],
        clause_categories=["effective_term", "expiration"],
        evidence=[
            Evidence(field="start_date", quote="Effective Date: January 1, 2025"),
            Evidence(field="expiration_date", quote="Expiration Date: February 1, 2026"),
        ],
        attest_approved=True,
        attest_deidentified=True,
    )
    app = create_app(store, AuthRegistry([]))
    approve = endpoint(app, f"/contracts/{{contract_id}}/evaluation-feedback/approve", "POST")
    reviewer = Principal("reviewer", frozenset({"reviewer"}), tenant_id=tenant)
    approved = approve(contract_id, request, reviewer)
    assert approved["status"] == "approved"
    assert approved["evaluation_case"]["expected"] == {
        "start_date": "2025-01-01", "expiration_date": "2026-02-01",
    }

    export = tmp_path / "approved-cases"
    monkeypatch.setenv("DATABASE_PATH", str(database))
    monkeypatch.setattr(sys, "argv", ["renewal-radar", "export-feedback", "--tenant-id", tenant, "--output", str(export)])
    assert main() == 0
    cases = load_cases(export)
    assert len(cases) == 1
    assert cases[0]["provider_under_review"] == "openai"
    assert cases[0]["governance"]["approval_reference"] == "LEGAL-APPROVAL-42"


def test_approved_feedback_rejects_unredacted_text_and_unsupported_spans(tmp_path):
    store = Store(str(tmp_path / "feedback.db"))
    contract_id = make_confirmed_contract(store)
    app = create_app(store, AuthRegistry([]))
    approve = endpoint(app, "/contracts/{contract_id}/evaluation-feedback/approve", "POST")
    reviewer = Principal("reviewer", frozenset({"reviewer"}), tenant_id="ops-tenant")
    request = EvaluationFeedbackApprovalRequest(
        redacted_text="Agreement effective January 1, 2025 expiration February 1, 2026 vendor@example.com",
        approval_reference="LEGAL-APPROVAL-43",
        fields=["expiration_date"],
        clause_categories=["expiration"],
        evidence=[Evidence(field="expiration_date", quote="expiration February 1, 2026")],
        attest_approved=True,
        attest_deidentified=True,
    )
    with pytest.raises(HTTPException) as error:
        approve(contract_id, request, reviewer)
    assert error.value.status_code == 422
    assert store.get_review_feedback(contract_id, "ops-tenant")["status"] == "captured"


def test_redaction_removes_review_feedback_and_notification_history(tmp_path):
    store = Store(str(tmp_path / "redaction.db"))
    contract_id = make_confirmed_contract(store, "redaction-tenant")
    task = store.list_tasks(tenant_id="redaction-tenant")[0]
    store.record_notification_delivery(task["id"], "log", "reminder", "sent", 4)
    assert store.get_review_feedback(contract_id, "redaction-tenant")
    assert store.operations_snapshot("redaction-tenant")["notifications"]["recent"]

    hold = store.place_legal_hold(contract_id, "redaction-tenant", "admin", "Temporary hold")
    store.release_legal_hold(hold["id"], "redaction-tenant", "admin")
    assert store.redact_contract(contract_id, "redaction-tenant", "admin")
    assert store.get_review_feedback(contract_id, "redaction-tenant") is None
    assert store.operations_snapshot("redaction-tenant")["notifications"]["recent"] == []
