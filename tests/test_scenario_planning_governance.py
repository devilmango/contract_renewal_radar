import base64
import json
import sqlite3
from datetime import date
from decimal import Decimal

from renewal_radar.schemas import ContractData
from renewal_radar.store import Store
from renewal_radar.jobs import enqueue_retention_run, process_jobs


def _contract(store, contract_id="agreement", tenant="acme"):
    terms = ContractData(
        contract="Hosted Platform Agreement",
        start_date=date(2025, 1, 1),
        expiration_date=date(2027, 1, 1),
        renewal_notice_days=90,
        annual_contract_value=Decimal("12000"),
        currency="USD",
    )
    store.save_contract(contract_id, "agreement.pdf", "rules", terms, "redacted contract text", "reviewer", tenant)
    task_id = store.confirm_contract(contract_id, terms, "reviewer", tenant)
    return terms, task_id


def test_scenario_plan_compares_costs_with_risk_history_and_milestones(tmp_path):
    store = Store(str(tmp_path / "scenario.db"))
    terms, task_id = _contract(store)
    supplier = store.create_supplier("Cloud Vendor", "reviewer", "acme", "critical")
    store.assign_contract_supplier("agreement", supplier["id"], "reviewer", "acme")
    forecast = store.renewal_forecast("acme", date(2026, 1, 1), date(2028, 1, 1), Decimal("5"), Decimal("10"))
    forecast_id = store.record_renewal_forecast("acme", forecast, "reviewer")
    decision = store.create_renewal_decision({"task_id": task_id, "strategy": "negotiate", "reason": "Compare alternatives"}, "reviewer", "acme")
    store.add_renewal_decision_milestone(decision["id"], "acme", "reviewer", {
        "title": "Complete security review", "due_date": date(2026, 12, 15),
    })
    assumptions = {
        "renewal_increase_pct": Decimal("5"), "renegotiation_savings_pct": Decimal("10"),
        "termination_transition_cost": Decimal("2500"), "horizon_months": 24,
        "as_of_date": date(2026, 1, 1), "forecast_id": forecast_id,
        "assumptions_note": "Use current supplier quote assumptions.",
    }
    plan = store.create_renewal_scenario_plan(task_id, "acme", "reviewer", assumptions)
    scenarios = plan["results"]["scenarios"]
    assert plan["results"]["supplier_risk"] == "critical"
    assert "critical" in scenarios["renew"]["risk_implication"]
    assert Decimal(scenarios["renew"]["projected_cost"]) > Decimal(scenarios["renegotiate"]["projected_cost"])
    assert scenarios["terminate"]["projected_cost"] == "14532.88"
    assert plan["results"]["inputs"]["forecast"] == {"annual_contract_value": "12000", "currency": "USD"}
    assert plan["results"]["inputs"]["forecast_history"]
    assert plan["results"]["inputs"]["milestone_warnings"]
    assert plan["assumptions"]["assumptions_note"] == "Use current supplier quote assumptions."
    assert store.list_renewal_scenario_plans(task_id, "acme")[0]["id"] == plan["id"]
    assert store.list_renewal_scenario_plans(task_id, "other") == []


def test_configurable_retention_honors_holds_and_exports_audit_metadata(tmp_path):
    store = Store(str(tmp_path / "governance.db"))
    terms, task_id = _contract(store, "held-contract")
    obligation = store.create_contract_obligation("held-contract", {
        "category": "insurance_certificate", "title": "Submit certificate", "due_date": date(2026, 12, 1),
        "evidence": {"field": "obligation:insurance_certificate", "quote": "Submit certificate annually."},
    }, "Owner", "owner@example.com", "reviewer", "acme")
    evidence = store.submit_obligation_evidence(obligation["id"], "acme", "owner", "proof.txt", "text/plain",
                                               base64.b64encode(b"sensitive proof").decode(), "digest", "proof comment")
    second_terms, second_task = _contract(store, "unheld-contract")
    second_obligation = store.create_contract_obligation("unheld-contract", {
        "category": "insurance_certificate", "title": "Submit certificate", "due_date": date(2026, 12, 1),
        "evidence": {"field": "obligation:insurance_certificate", "quote": "Submit certificate annually."},
    }, "Owner", "owner@example.com", "reviewer", "acme")
    second_evidence = store.submit_obligation_evidence(second_obligation["id"], "acme", "owner", "proof.txt", "text/plain",
                                                       base64.b64encode(b"old proof").decode(), "digest", "proof comment")
    hold = store.place_legal_hold("held-contract", "acme", "admin", "Pending litigation")
    assert hold
    eval_run = store.record_provider_evaluation("acme", "rules", {"cases_scored": 1, "exact_case_match": {"accuracy": 1.0}}, "fingerprint", "reviewer")
    stale = "2000-01-01T00:00:00+00:00"
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE obligation_evidence SET created_at=? WHERE id IN (?,?)", (stale, evidence["id"], second_evidence["id"]))
        connection.execute("UPDATE provider_evaluation_runs SET created_at=? WHERE id=?", (stale, eval_run["id"]))
    policy = store.save_data_retention_policy("acme", {"contract_days": 0, "evidence_days": 1, "provider_evaluation_days": 1}, "admin")
    assert policy["evidence_days"] == 1
    job = enqueue_retention_run(store, "acme")
    processed = process_jobs(store, limit=1, worker_id="retention-worker", job_id=job["id"])
    result = store.get_job(job["id"], "acme")["result_json"]
    result = json.loads(result)
    assert processed["succeeded"] == 1
    assert result == {"contracts_redacted": 0, "evidence_deleted": 1, "provider_evaluations_deleted": 1}
    assert store.get_obligation_evidence_content(evidence["id"], "acme") is not None
    assert store.get_obligation_evidence_content(second_evidence["id"], "acme") is None
    assert store.provider_evaluation_history("acme") == []

    rows = store.audit_export_rows("acme", "auditor")
    assert any(row["event_type"] == "obligation.evidence_purged" for row in rows)
    assert any(row["event_type"] == "provider_evaluation.purged" for row in rows)
    assert any(row["event_type"] == "audit.exported" for row in rows)
    serialized = json.dumps(rows)
    assert "sensitive proof" not in serialized
    assert "content_base64" not in serialized


