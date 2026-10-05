from datetime import date
import json
import sqlite3

import pytest

from renewal_radar import jobs
from renewal_radar.api import create_app
from renewal_radar.auth import AuthRegistry
from renewal_radar.evaluation import _evidence_supports, load_cases
from renewal_radar.extractor import ExtractionError, RulesExtractor, get_extractor
from renewal_radar.schemas import ContractData
from renewal_radar.store import Store


def save_pending(store: Store, contract_id: str = "retained-contract", tenant_id: str = "acme") -> None:
    store.save_contract(
        contract_id, "private-agreement.pdf", "rules",
        ContractData(contract="Private Supplier", expiration_date=date(2026, 1, 1)),
        "CONFIDENTIAL RAW CONTRACT TEXT", "reviewer", tenant_id,
    )


def test_jobs_are_durable_idempotent_leased_and_retryable(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "radar.db"))
    first = store.enqueue_job("acme", "calendar.sync", {"tenant_id": "acme"}, "sync-1")
    duplicate = store.enqueue_job("acme", "calendar.sync", {"tenant_id": "acme"}, "sync-1")
    assert first["id"] == duplicate["id"]

    claimed = store.claim_job("worker-1", lease_seconds=1)
    assert claimed["status"] == "running" and claimed["attempts"] == 1
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", claimed["id"]))
    reclaimed = store.claim_job("worker-2")
    assert reclaimed["id"] == claimed["id"] and reclaimed["attempts"] == 2

    monkeypatch.setattr(jobs, "sync_calendar", lambda *_args: (_ for _ in ()).throw(RuntimeError("provider down")))
    store.fail_job(reclaimed["id"], "worker-2", "RuntimeError: provider down", retry_delay_seconds=0)
    retry_claim = store.claim_job("worker-3")
    assert retry_claim["id"] == claimed["id"] and retry_claim["attempts"] == 3
    assert store.fail_job(retry_claim["id"], "worker-3", "provider down", retry_delay_seconds=0)
    assert store.get_job(claimed["id"], "acme")["status"] == "queued"


def test_job_worker_records_success_and_dead_letters_after_limit(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "radar.db"))
    store.enqueue_job("acme", "calendar.sync", {"tenant_id": "acme"}, "sync-1", max_attempts=1)
    monkeypatch.setattr(jobs, "sync_calendar", lambda *_args: {"synced": 2, "removed": 0})
    assert jobs.process_jobs(store, worker_id="calendar-worker") == {
        "processed": 1, "succeeded": 1, "retried": 0, "dead": 0,
    }
    succeeded = store.list_jobs("acme")[0]
    assert succeeded["status"] == "succeeded"
    assert json.loads(succeeded["result_json"]) == {"synced": 2, "removed": 0}

    store.enqueue_job("acme", "calendar.sync", {"tenant_id": "acme"}, "sync-2", max_attempts=1)
    monkeypatch.setattr(jobs, "sync_calendar", lambda *_args: (_ for _ in ()).throw(RuntimeError("down")))
    assert jobs.process_jobs(store, worker_id="calendar-worker") == {
        "processed": 1, "succeeded": 0, "retried": 0, "dead": 1,
    }
    dead = next(row for row in store.list_jobs("acme") if row["idempotency_key"] == "sync-2")
    assert dead["status"] == "dead" and "down" in dead["last_error"]
    assert store.retry_dead_job(dead["id"], "other-tenant") is None
    assert store.retry_dead_job(dead["id"], "acme")["status"] == "queued"


def test_redaction_respects_legal_holds_and_preserves_access_history(tmp_path):
    store = Store(str(tmp_path / "radar.db"))
    save_pending(store)
    store.record_access("acme", "reviewer", "GET /contracts/retained-contract/source -> 200", "contracts", "retained-contract")
    hold = store.place_legal_hold("retained-contract", "acme", "admin", "Active dispute")
    assert hold is not None
    with pytest.raises(ValueError, match="legal hold"):
        store.redact_contract("retained-contract", "acme", "admin")

    store.release_legal_hold(hold["id"], "acme", "admin")
    assert store.redact_contract("retained-contract", "acme", "admin")
    contract = store.get_contract("retained-contract", "acme")
    assert contract["status"] == "redacted"
    assert contract["filename"] == "redacted.pdf"
    assert contract["raw_text"] == ""
    assert ContractData.model_validate_json(contract["extracted_json"]).contract is None
    events = store.audit("retained-contract", "acme")
    assert len(events) == 1 and events[0]["event_type"] == "contract.data_redacted"
    assert store.access_history("acme")[0]["actor"] == "reviewer"


def test_retention_is_tenant_scoped_and_excludes_held_contracts(tmp_path):
    store = Store(str(tmp_path / "radar.db"))
    save_pending(store, "rejected-acme", "acme")
    store.reject_contract("rejected-acme", "reviewer", "acme")
    save_pending(store, "rejected-other", "other")
    store.reject_contract("rejected-other", "reviewer", "other")
    hold = store.place_legal_hold("rejected-acme", "acme", "admin", "Discovery hold")
    assert hold

    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE contracts SET created_at='2000-01-01T00:00:00+00:00' WHERE id LIKE 'rejected-%'")
    assert store.redact_expired_contracts(1, tenant_id="acme") == 0
    assert store.get_contract("rejected-other", "other")["status"] == "rejected"
    store.release_legal_hold(hold["id"], "acme", "admin")
    assert store.redact_expired_contracts(1, tenant_id="acme") == 1
    assert store.get_contract("rejected-acme", "acme")["status"] == "redacted"
    assert store.get_contract("rejected-other", "other")["status"] == "rejected"


def test_provider_allowlist_blocks_unapproved_llm_and_auto_uses_rules(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_ALLOWED_PROVIDERS", "rules,anthropic")
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    assert isinstance(get_extractor(), RulesExtractor)
    with pytest.raises(ExtractionError, match="blocked"):
        get_extractor("openai")


def test_evaluation_checks_value_support_not_just_quote_grounding():
    assert _evidence_supports("renewal_notice_days", 60, "Notice must be sent at least 60 business days before expiration")
    assert not _evidence_supports("renewal_notice_days", 60, "Termination notice period: 60 days")
    assert _evidence_supports("start_date", date(2025, 1, 1), "Effective Date: January 1, 2025")
    assert not _evidence_supports("start_date", date(2026, 1, 1), "Effective Date: January 1, 2025")


def test_approved_deidentified_cases_require_governance_metadata(tmp_path):
    case = {
        "id": "approved-001",
        "source_type": "approved_deidentified",
        "contract_text": "[VENDOR] Services Agreement. Effective Date: January 1, 2025.",
        "expected": {"start_date": "2025-01-01"},
    }
    case_path = tmp_path / "approved.json"
    case_path.write_text(json.dumps(case))
    with pytest.raises(ValueError, match="approval_reference"):
        load_cases(tmp_path)

    case["governance"] = {"approved": True, "deidentified": True, "approval_reference": "LEGAL-1234"}
    case_path.write_text(json.dumps(case))
    assert load_cases(tmp_path)[0]["source_type"] == "approved_deidentified"


def test_api_registers_durable_job_and_governance_routes(tmp_path):
    app = create_app(Store(str(tmp_path / "api.db")), AuthRegistry([]))
    routes = {(route.path, tuple(sorted(route.methods or ()))) for route in app.routes}
    assert ("/jobs/{job_id}/retry", ("POST",)) in routes
    assert ("/data-retention/run", ("POST",)) in routes
    assert ("/access-history", ("GET",)) in routes
    assert ("/contracts/{contract_id}/legal-holds", ("GET",)) in routes
    assert ("/contracts/{contract_id}/legal-holds", ("POST",)) in routes
