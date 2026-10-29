import base64
import hashlib
import json
import zipfile
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from fastapi import HTTPException

from renewal_radar.api import create_app
from renewal_radar.auth import AuthRegistry, Principal
from renewal_radar.evaluation import evaluate_extractor, load_cases
from renewal_radar.extractor import RulesExtractor
from renewal_radar.schemas import (
    ApprovalPlaybookRequest, BulkPortfolioRequest, ContractData, ObligationComplianceRequest,
    ObligationEvidenceRequest, ObligationEvidenceReviewRequest, PlaybookApprovalRequest,
    PlaybookApplyRequest, PlaybookChecklistRequest, SupplierCreateRequest, SupplierRiskUpdateRequest,
    PortfolioFilterRequest, ProviderEvaluationScheduleRequest,
)
from renewal_radar.store import Store


def _contract(store, contract_id="contract-a", tenant="acme", owner="owner@example.com", value="250000"):
    terms = ContractData(
        contract="Hosted Platform", expiration_date=date(2027, 12, 31), renewal_notice_days=90,
        owner_email=owner, annual_contract_value=Decimal(value), currency="USD",
    )
    store.save_contract(contract_id, f"{contract_id}.pdf", "rules", terms, "redacted fixture", "reviewer", tenant)
    task_id = store.confirm_contract(contract_id, terms, "reviewer", tenant)
    return task_id


def _obligation(store, contract_id="contract-a", tenant="acme"):
    return store.create_contract_obligation(
        contract_id,
        {"category": "insurance_certificate", "title": "Submit insurance certificate", "due_date": date(2027, 1, 1),
         "evidence": {"field": "obligation:insurance_certificate", "quote": "Submit current certificate."}},
        "Assigned owner", "owner@example.com", "reviewer", tenant,
    )


def _route(app, path, method):
    return next(route.endpoint for route in app.routes if route.path == path and method in route.methods)


def test_obligation_proof_requires_review_and_records_compliance_audit(tmp_path):
    store = Store(str(tmp_path / "obligation-proof.db"))
    _contract(store)
    obligation = _obligation(store)
    app = create_app(store, AuthRegistry([]))
    upload = _route(app, "/obligations/{obligation_id}/evidence", "POST")
    download = _route(app, "/obligation-evidence/{evidence_id}/download", "GET")
    review = _route(app, "/obligation-evidence/{evidence_id}/review", "POST")
    history = _route(app, "/obligations/{obligation_id}/compliance", "GET")
    resolve = _route(app, "/tasks/{task_id}/resolve", "POST")
    owner = Principal("assigned-owner", frozenset({"owner"}), "owner@example.com", "acme")
    reviewer = Principal("legal-reviewer", frozenset({"reviewer"}), "legal@example.com", "acme")
    outsider = Principal("outsider", frozenset({"owner"}), "other@example.com", "acme")
    payload = b"Certificate of Insurance 2027"
    submitted = upload(obligation["id"], ObligationEvidenceRequest(
        filename="certificate.txt", content_type="text/plain", content_base64=base64.b64encode(payload).decode(),
        comment="Uploaded the signed current certificate.",
    ), owner)
    assert submitted["status"] == "pending_signoff"
    assert "content_base64" not in submitted
    assert download(submitted["id"], owner).body == payload
    assert store.get_task(obligation["task_id"], "acme")["status"] == "open"
    with pytest.raises(HTTPException) as private:
        history(obligation["id"], outsider)
    assert private.value.status_code == 403
    with pytest.raises(HTTPException) as unauthorized:
        review(submitted["id"], ObligationEvidenceReviewRequest(decision="approved"), owner)
    assert unauthorized.value.status_code == 403
    with pytest.raises(HTTPException) as blocked:
        resolve(obligation["task_id"], type("Request", (), {"comment": "done"})(), owner)
    assert blocked.value.status_code == 409

    reviewed = review(submitted["id"], ObligationEvidenceReviewRequest(decision="approved", comment="Verified."), reviewer)
    assert reviewed["status"] == "approved"
    assert store.get_task(obligation["task_id"], "acme")["status"] == "resolved"
    compliance = history(obligation["id"], owner)
    assert [event["outcome"] for event in compliance["history"]] == ["submitted_for_signoff", "completed"]
    assert store.audit("contract-a", "acme")[-1]["event_type"] == "obligation.evidence_approved"


def test_obligation_failed_and_missed_outcomes_are_audited(tmp_path):
    store = Store(str(tmp_path / "compliance.db"))
    _contract(store)
    obligation = _obligation(store)
    event = store.record_obligation_compliance(obligation["id"], "acme", "owner", "failed", "Certificate was rejected")
    assert event["outcome"] == "failed"
    with pytest.raises(ValueError, match="after its due date"):
        store.record_obligation_compliance(obligation["id"], "acme", "owner", "missed", "Not received")

    with store.connect() as db:
        db.execute("UPDATE tasks SET due_date='2020-01-01' WHERE id=?", (obligation["task_id"],))
    missed = store.record_obligation_compliance(obligation["id"], "acme", "reviewer", "missed", "No submission by deadline")
    assert missed["outcome"] == "missed"
    assert [event["outcome"] for event in store.obligation_compliance_history(obligation["id"], "acme")] == ["failed", "missed"]


def test_redaction_removes_attached_compliance_evidence_and_playbook_data(tmp_path):
    store = Store(str(tmp_path / "redact-proof.db"))
    task_id = _contract(store)
    obligation = _obligation(store)
    payload = b"private proof"
    proof = store.submit_obligation_evidence(
        obligation["id"], "acme", "owner", "proof.txt", "text/plain",
        base64.b64encode(payload).decode(), hashlib.sha256(payload).hexdigest(), None,
    )
    playbook = store.create_renewal_playbook({
        "name": "Review", "required_approvers": ["legal@example.com"], "checklist": [],
    }, "reviewer", "acme")
    store.apply_renewal_playbook(playbook["id"], task_id, "acme", "reviewer", [])

    assert store.redact_contract("contract-a", "acme", "admin")
    assert store.get_obligation_evidence_content(proof["id"], "acme") is None
    assert store.list_contract_obligations("acme") == []
    assert store.task_playbook_review(task_id, "acme") is None


def test_playbook_matches_risk_value_and_clause_and_requires_all_gates(tmp_path):
    store = Store(str(tmp_path / "playbooks.db"))
    task_id = _contract(store)
    supplier = store.create_supplier("CriticalCloud", "reviewer", "acme", "high")
    store.assign_contract_supplier("contract-a", supplier["id"], "reviewer", "acme")
    app = create_app(store, AuthRegistry([]))
    create = _route(app, "/renewal-playbooks", "POST")
    apply = _route(app, "/tasks/{task_id}/playbooks/{playbook_id}", "POST")
    approve = _route(app, "/tasks/{task_id}/playbook-review/approval", "POST")
    checklist = _route(app, "/tasks/{task_id}/playbook-review/checklist", "PATCH")
    reviewer_a = Principal("reviewer-a", frozenset({"reviewer"}), "legal-a@example.com", "acme")
    reviewer_b = Principal("reviewer-b", frozenset({"reviewer"}), "legal-b@example.com", "acme")
    playbook = create(ApprovalPlaybookRequest(
        name="High value cloud renewal", min_contract_value=Decimal("100000"), currency="USD",
        supplier_risk_levels=["high", "critical"], changed_clause_categories=["renewal", "notice_period"],
        required_approvers=["legal-a@example.com", "legal-b@example.com"], checklist=["Review amendment", "Confirm notice route"],
    ), reviewer_a)
    with pytest.raises(HTTPException) as unmatched:
        apply(task_id, playbook["id"], PlaybookApplyRequest(changed_clause_categories=["pricing"]), reviewer_a)
    assert unmatched.value.status_code == 409

    review = apply(task_id, playbook["id"], PlaybookApplyRequest(changed_clause_categories=["renewal"]), reviewer_a)
    assert review["status"] == "blocked"
    assert store.get_task(task_id, "acme")["workflow_state"] == "pending_approval"
    approve(task_id, PlaybookApprovalRequest(approved=True), reviewer_a)
    current = approve(task_id, PlaybookApprovalRequest(approved=True), reviewer_b)
    assert current["status"] == "blocked"  # Checklist is still open.
    checklist(task_id, PlaybookChecklistRequest(index=0, completed=True), reviewer_a)
    current = checklist(task_id, PlaybookChecklistRequest(index=1, completed=True), reviewer_a)
    assert current["status"] == "approved"
    assert current["changed_clause_categories"] == ["renewal"]


def test_rejected_playbook_can_be_replaced_and_returns_task_for_changes(tmp_path):
    store = Store(str(tmp_path / "playbook-rejection.db"))
    task_id = _contract(store)
    store.create_supplier("CriticalCloud", "reviewer", "acme", "critical")
    supplier = store.list_suppliers("acme")[0]
    store.assign_contract_supplier("contract-a", supplier["id"], "reviewer", "acme")
    app = create_app(store, AuthRegistry([]))
    create = _route(app, "/renewal-playbooks", "POST")
    apply = _route(app, "/tasks/{task_id}/playbooks/{playbook_id}", "POST")
    approve = _route(app, "/tasks/{task_id}/playbook-review/approval", "POST")
    reviewer = Principal("reviewer", frozenset({"reviewer"}), "legal@example.com", "acme")
    playbook = create(ApprovalPlaybookRequest(name="Review", required_approvers=["legal@example.com"]), reviewer)
    first = apply(task_id, playbook["id"], PlaybookApplyRequest(), reviewer)
    rejected = approve(task_id, PlaybookApprovalRequest(approved=False), reviewer)
    assert rejected["status"] == "rejected"
    assert store.get_task(task_id, "acme")["workflow_state"] == "needs_changes"
    second = apply(task_id, playbook["id"], PlaybookApplyRequest(), reviewer)
    assert second["id"] != first["id"] and second["status"] == "blocked"


def test_bulk_actions_are_tenant_scoped_and_exports_support_csv_and_xlsx(tmp_path):
    store = Store(str(tmp_path / "portfolio.db"))
    task_id = _contract(store)
    _contract(store, "contract-b")
    supplier = store.create_supplier("Example Supplier", "reviewer", "acme")
    _obligation(store)
    other_tenant_task = _contract(store, "tenant-b", "tenant-b")
    app = create_app(store, AuthRegistry([]))
    bulk = _route(app, "/portfolio/bulk", "POST")
    export = _route(app, "/portfolio/export", "GET")
    reviewer = Principal("reviewer", frozenset({"reviewer"}), "legal@example.com", "acme")
    assigned = bulk(BulkPortfolioRequest(action="assign_tasks", ids=[task_id], owner_name="Casey", owner_email="casey@example.com"), reviewer)
    assert assigned["count"] == 1
    transitioned = bulk(BulkPortfolioRequest(action="transition_tasks", ids=[task_id], workflow_state="pending_approval"), reviewer)
    assert transitioned["updated"] == [task_id]
    contracts = bulk(BulkPortfolioRequest(action="assign_supplier", ids=["contract-a", "contract-b"], supplier_id=supplier["id"]), reviewer)
    assert contracts["count"] == 2
    with pytest.raises(HTTPException) as cross_tenant:
        bulk(BulkPortfolioRequest(action="assign_tasks", ids=[other_tenant_task], owner_email="x@example.com"), reviewer)
    assert cross_tenant.value.status_code == 409

    csv_export = export("contracts", "csv", reviewer)
    assert csv_export.headers["content-disposition"].endswith('"renewal-radar-contracts.csv"')
    assert b"contract-a" in csv_export.body and b"redacted fixture" not in csv_export.body
    workbook = export("obligations", "xlsx", reviewer)
    with zipfile.ZipFile(BytesIO(workbook.body)) as archive:
        assert "xl/worksheets/sheet1.xml" in archive.namelist()
        assert "Submit insurance certificate" in archive.read("xl/worksheets/sheet1.xml").decode()


def test_bulk_obligation_assignment_and_evidence_review_are_audited(tmp_path):
    store = Store(str(tmp_path / "bulk-obligations.db"))
    _contract(store)
    first, second = _obligation(store), _obligation(store)
    proofs = [store.submit_obligation_evidence(item["id"], "acme", "owner", "proof.txt", "text/plain", "cHJvb2Y=", "digest", None) for item in (first, second)]
    app = create_app(store, AuthRegistry([]))
    bulk = _route(app, "/portfolio/bulk", "POST")
    reviewer = Principal("reviewer", frozenset({"reviewer"}), "legal@example.com", "acme")
    assigned = bulk(BulkPortfolioRequest(action="assign_obligations", ids=[first["id"], second["id"]], owner_name="New Owner", owner_email="new@example.com"), reviewer)
    assert assigned["count"] == 2
    assert store.get_contract_obligation(first["id"], "acme")["owner_email"] == "new@example.com"
    reviewed = bulk(BulkPortfolioRequest(action="review_obligations", ids=[proof["id"] for proof in proofs], decision="approved", comment="Verified as a batch"), reviewer)
    assert reviewed["count"] == 2
    assert all(store.get_task(item["task_id"], "acme")["status"] == "resolved" for item in (first, second))
    assert json.loads(store.audit("contract-a", "acme")[-1]["details_json"])["bulk"] is True


def test_scheduled_evaluation_and_saved_filter_export_preview(tmp_path):
    store = Store(str(tmp_path / "portfolio-schedule.db"))
    task_id = _contract(store)
    app = create_app(store, AuthRegistry([]))
    schedule = _route(app, "/evaluations/schedule", "PUT")
    filters = _route(app, "/portfolio/filters", "POST")
    preview = _route(app, "/portfolio/export/preview", "GET")
    reviewer = Principal("reviewer", frozenset({"reviewer"}), "legal@example.com", "acme")
    saved_schedule = schedule(ProviderEvaluationScheduleRequest(enabled=True, interval_hours=12, alert_emails=["legal@example.com"]), reviewer)
    assert saved_schedule["interval_hours"] == 12
    due, _ = store.provider_evaluation_due("acme", "model-v1")
    assert due
    store.complete_provider_evaluation_schedule("acme", "model-v1")
    due, _ = store.provider_evaluation_due("acme", "model-v1")
    assert not due
    saved = filters(PortfolioFilterRequest(name="Open renewals", dataset="tasks", filters={"status": "open", "task_kind": "renewal"}), reviewer)
    result = preview("tasks", saved["id"], reviewer)
    assert result["row_count"] == 1
    assert result["sample"][0]["id"] == task_id


def test_provider_evaluation_scores_amendment_clause_evidence_and_detects_drift(tmp_path, monkeypatch):
    dataset = load_cases(__import__("pathlib").Path(__file__).parents[1] / "evaluation" / "cases")
    report = evaluate_extractor(RulesExtractor(), dataset)
    assert report["clause_categories"]["total"] > 0
    assert report["amendment_comparison"]["accuracy"] == 1.0
    amendment = next(item for item in report["case_results"] if item["case"] == "amendment-replaces-expiration")
    assert set(amendment["amendment_clause_categories"]) == {"effective_term", "expiration", "renewal", "auto_renewal", "notice_period"}

    store = Store(str(tmp_path / "provider-runs.db"))
    fingerprint = "fixture-hash"
    first = {"cases_scored": 10, "exact_case_match": {"accuracy": 0.95},
             "field_evidence_support": {"accuracy": 1.0}, "clause_categories": {"accuracy": 1.0},
             "amendment_comparison": {"accuracy": 1.0}}
    second = {"cases_scored": 10, "exact_case_match": {"accuracy": 0.8},
              "field_evidence_support": {"accuracy": 0.8}, "clause_categories": {"accuracy": 0.75},
              "amendment_comparison": {"accuracy": 0.75}}
    store.record_provider_evaluation("acme", "rules", first, fingerprint, "reviewer")
    degraded = store.record_provider_evaluation("acme", "rules", second, fingerprint, "reviewer")
    assert {item["metric"] for item in degraded["alerts"]} == {
        "exact_accuracy", "evidence_support_accuracy", "clause_accuracy", "amendment_comparison_accuracy",
    }
    assert not store.provider_evaluation_history("other-tenant")

    import renewal_radar.api as api_module
    monkeypatch.setattr(api_module, "get_extractor", lambda provider: RulesExtractor() if provider == "rules" else (_ for _ in ()).throw(RuntimeError("not configured")))
    monkeypatch.setattr(api_module, "load_cases", lambda _path: dataset)
    app = create_app(store, AuthRegistry([]))
    run_endpoint = _route(app, "/evaluations/runs", "POST")
    drift_endpoint = _route(app, "/evaluations/drift", "GET")
    reviewer = Principal("reviewer", frozenset({"reviewer"}), "legal@example.com", "acme")
    result = run_endpoint(reviewer)
    assert len(result["runs"]) == 1 and result["runs"][0]["provider"] == "rules"
    drift = drift_endpoint(None, reviewer)
    assert drift["threshold"]["metric_drop"] == 0.1
    assert any(item["provider"] == "rules" for item in drift["alerts"])


def test_playbook_schema_and_supplier_risk_validation():
    with pytest.raises(ValueError, match="currency is required"):
        ApprovalPlaybookRequest(name="Bad", min_contract_value=10, required_approvers=["legal@example.com"])
    with pytest.raises(ValueError, match="valid email"):
        ApprovalPlaybookRequest(name="Bad", required_approvers=["not-an-email"])
