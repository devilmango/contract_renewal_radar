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


def test_scenario_sensitivity_returns_bounded_transparent_comparisons(tmp_path):
    store = Store(str(tmp_path / "sensitivity.db"))
    _terms, task_id = _contract(store)
    axes = {
        "renewal_increase_pcts": [Decimal("0"), Decimal("10")],
        "renegotiation_savings_pcts": [Decimal("5"), Decimal("15")],
        "termination_transition_costs": [Decimal("0"), Decimal("5000")],
        "horizon_months": [12, 24],
        "as_of_date": date(2026, 1, 1),
        "forecast_id": None,
    }
    result = store.analyze_renewal_sensitivity(task_id, "acme", "reviewer", axes)
    assert result["combination_count"] == 16
    row = result["rows"][0]
    assert set(row["scenario_costs"]) == {"renew", "renegotiate", "terminate"}
    assert row["cost_differences"]["renew_minus_renegotiate"]
    assert row["calculation_components"]["formula"].startswith("renew = current term")
    assert row["assumptions"]["horizon_months"] == 12
    assert store.list_renewal_scenario_plans(task_id, "acme") == []


def test_retention_policy_upgrade_adds_version_to_existing_database(tmp_path):
    path = tmp_path / "legacy-retention.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE data_retention_policies (tenant_id TEXT PRIMARY KEY, contract_days INTEGER NOT NULL, evidence_days INTEGER NOT NULL, provider_evaluation_days INTEGER NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL)")
        connection.execute("INSERT INTO data_retention_policies VALUES ('acme', 180, 90, 30, 'admin', '2026-01-01T00:00:00+00:00')")
    store = Store(str(path))
    policy = store.get_data_retention_policy("acme")
    assert policy["version"] == 1
    with store.connect() as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='data_retention_receipts'").fetchone()


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
    _old_terms, old_task = _contract(store, "old-rejected")
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE contracts SET status='rejected' WHERE id='old-rejected'")
    hold = store.place_legal_hold("held-contract", "acme", "admin", "Pending litigation")
    assert hold
    eval_run = store.record_provider_evaluation("acme", "rules", {"cases_scored": 1, "exact_case_match": {"accuracy": 1.0}}, "fingerprint", "reviewer")
    stale = "2000-01-01T00:00:00+00:00"
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE obligation_evidence SET created_at=? WHERE id IN (?,?)", (stale, evidence["id"], second_evidence["id"]))
        connection.execute("UPDATE provider_evaluation_runs SET created_at=? WHERE id=?", (stale, eval_run["id"]))
        connection.execute("UPDATE contracts SET created_at=? WHERE id='old-rejected'", (stale,))
    policy = store.save_data_retention_policy("acme", {"contract_days": 1, "evidence_days": 1, "provider_evaluation_days": 1}, "admin")
    assert policy["evidence_days"] == 1 and policy["version"] == 1
    preview = store.preview_data_retention("acme", "admin")
    assert preview["policy_version"] == "1"
    evidence_preview = {row["entity_id"]: row for row in preview["categories"]["evidence"]}
    assert evidence_preview[evidence["id"]]["exclusion_reason"] == "active_legal_hold"
    assert evidence_preview[second_evidence["id"]]["eligible"]
    contract_preview = {row["entity_id"]: row for row in preview["categories"]["contracts"]}
    assert contract_preview["old-rejected"]["eligible"]
    assert any(item["reason"] == "Pending litigation" for item in preview["legal_hold_reasons"])
    job = enqueue_retention_run(store, "acme")
    processed = process_jobs(store, limit=1, worker_id="retention-worker", job_id=job["id"])
    result = store.get_job(job["id"], "acme")["result_json"]
    result = json.loads(result)
    assert processed["succeeded"] == 1
    assert result["contracts_redacted"] == 1
    assert result["evidence_deleted"] == 1
    assert result["provider_evaluations_deleted"] == 1
    receipt = store.get_data_retention_receipt(result["receipt_id"], "acme")
    assert receipt["policy_version"] == "1"
    assert receipt["summary"]["evidence_deleted"] == 1
    assert any("obligation.evidence_purged" in reference for reference in receipt["audit_references"])
    assert any("contract.data_redacted" in reference for reference in receipt["audit_references"])
    assert any(item["reason"] == "Pending litigation" for item in receipt["legal_hold_reasons"])
    assert store.get_data_retention_receipt(receipt["id"], "other") is None
    assert store.list_data_retention_receipts("acme")[0]["id"] == receipt["id"]
    updated = store.save_data_retention_policy("acme", {"contract_days": 2, "evidence_days": 1, "provider_evaluation_days": 1}, "admin")
    assert updated["version"] == 2 and receipt["policy_version"] == "1"
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


