from datetime import date

import pytest
from fastapi import HTTPException

from renewal_radar.api import create_app
from renewal_radar.auth import AuthRegistry, Principal
from renewal_radar.schemas import ConfirmationRequest, ContractData
from renewal_radar.store import Store


def _save_confirmed(store, contract_id: str, terms: ContractData, tenant: str = "acme") -> None:
    store.save_contract(contract_id, f"{contract_id}.pdf", "rules", terms, "redacted source", "reviewer", tenant)
    store.confirm_contract(contract_id, terms, "reviewer", tenant)


def test_amendment_candidates_rank_similar_active_contracts_per_tenant(tmp_path):
    store = Store(str(tmp_path / "candidate-review.db"))
    _save_confirmed(store, "active-cloud", ContractData(
        contract="Acme Cloud Services Agreement", expiration_date=date(2027, 1, 1),
        renewal_notice_days=30, auto_renew=True,
    ))
    _save_confirmed(store, "active-other", ContractData(
        contract="Northwind Facilities Agreement", expiration_date=date(2027, 1, 1),
        renewal_notice_days=30, auto_renew=True,
    ))
    proposal = ContractData(contract="Acme Cloud Services Amendment", expiration_date=date(2028, 1, 1))
    store.save_contract("proposal", "amendment.pdf", "rules", proposal, "redacted", "reviewer", "acme")

    candidates = store.amendment_candidates("proposal", "acme")
    assert candidates[0]["contract_id"] == "active-cloud"
    assert candidates[0]["match_score"] == 1.0
    assert store.amendment_candidates("proposal", "another-tenant") == []


def test_amendment_preview_shows_changes_and_task_effects_without_writes(tmp_path):
    store = Store(str(tmp_path / "amendment-preview.db"))
    original = ContractData(
        contract="Acme Cloud Services", expiration_date=date(2027, 1, 1),
        renewal_notice_days=30, auto_renew=True, owner_email="owner@example.com",
    )
    _save_confirmed(store, "original", original)
    proposal = ContractData(
        contract="Acme Cloud Services", expiration_date=date(2028, 1, 1),
        renewal_notice_days=90, auto_renew=True, owner_email="new-owner@example.com",
        supersedes_contract_id="original",
    )
    store.save_contract("proposal", "amendment.pdf", "rules", proposal, "redacted", "reviewer", "acme")

    preview = store.preview_contract_confirmation("proposal", proposal, "acme")

    assert preview["amendment"] is True
    assert preview["changes"]["expiration_date"] == {"before": "2027-01-01", "after": "2028-01-01"}
    assert preview["changes"]["renewal_notice_days"] == {"before": 30, "after": 90}
    assert len(preview["impact"]["open_tasks_to_resolve"]) == 1
    assert preview["impact"]["new_task"]["owner_email"] == "new-owner@example.com"
    assert preview["impact"]["new_task"]["due_date"] == "2027-10-03"
    assert store.get_contract("original", "acme")["status"] == "active"
    assert store.get_contract("proposal", "acme")["status"] == "pending_review"
    assert store.get_task(preview["impact"]["open_tasks_to_resolve"][0]["id"], "acme")["status"] == "open"


def test_amendment_preview_rejects_contract_from_another_tenant(tmp_path):
    store = Store(str(tmp_path / "cross-tenant-preview.db"))
    terms = ContractData(contract="Other tenant", expiration_date=date(2027, 1, 1))
    _save_confirmed(store, "foreign", terms, "tenant-b")
    proposal = ContractData(
        contract="Possible amendment", expiration_date=date(2028, 1, 1),
        supersedes_contract_id="foreign",
    )
    store.save_contract("proposal", "amendment.pdf", "rules", proposal, "redacted", "reviewer", "tenant-a")

    with pytest.raises(ValueError, match="active contract in this organization"):
        store.preview_contract_confirmation("proposal", proposal, "tenant-a")


def test_amendment_review_endpoints_require_reviewer_scope(tmp_path):
    store = Store(str(tmp_path / "amendment-api.db"))
    proposal = ContractData(contract="New Agreement", expiration_date=date(2028, 1, 1))
    store.save_contract("proposal", "new.pdf", "rules", proposal, "redacted", "reviewer", "acme")
    app = create_app(store, AuthRegistry([]))
    candidates_endpoint = next(
        route.endpoint for route in app.routes if route.path == "/contracts/{contract_id}/amendment-candidates"
    )
    preview_endpoint = next(
        route.endpoint for route in app.routes if route.path == "/contracts/{contract_id}/amendment-preview"
    )
    reviewer = Principal("reviewer", frozenset({"reviewer"}), tenant_id="acme")
    owner = Principal("owner", frozenset({"owner"}), tenant_id="acme")

    assert candidates_endpoint("proposal", reviewer)["candidates"] == []
    assert preview_endpoint("proposal", ConfirmationRequest(contract=proposal), reviewer)["amendment"] is False
    with pytest.raises(HTTPException) as error:
        candidates_endpoint("proposal", owner)
    assert error.value.status_code == 403
