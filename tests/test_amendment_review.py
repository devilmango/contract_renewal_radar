from datetime import date

import pytest
from fastapi import HTTPException

from renewal_radar.api import create_app
from renewal_radar.auth import AuthRegistry, Principal
from renewal_radar.schemas import ConfirmationRequest, ContractData, ContractObligation, Evidence, ObligationCreateRequest
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


def test_amendment_preview_quotes_source_clause_changes_without_structured_differences(tmp_path):
    store = Store(str(tmp_path / "amendment-source-diff.db"))
    terms = ContractData(
        contract="Acme Cloud Services", expiration_date=date(2028, 1, 1), renewal_notice_days=90, auto_renew=True,
    )
    _save_confirmed(store, "original", terms)
    proposal = terms.model_copy(update={"supersedes_contract_id": "original"})
    store.save_contract(
        "proposal", "amendment.pdf", "rules", proposal,
        "Section 8. Data retention. Provider will delete customer data within 30 days after termination.\n"
        "Section 9. Liability. Liability is capped at fees paid.", "reviewer", "acme",
    )
    with store.connect() as db:
        db.execute(
            "UPDATE contracts SET raw_text=? WHERE id=? AND tenant_id=?",
            ("Section 8. Data retention. Provider will delete customer data within 90 days after termination.\n"
             "Section 9. Liability. Liability is capped at fees paid.", "original", "acme"),
        )

    preview = store.preview_contract_confirmation("proposal", proposal, "acme")

    assert preview["changes"] == {}
    changes = preview["source_comparison"]["changes"]
    assert any("within 30 days" in item["source_quote"] for item in changes)
    assert any("within 90 days" in item["source_quote"] for item in changes)
    assert all(item["needs_reviewer_attention"] for item in changes)
    assert any("data_retention" in item["categories"] for item in changes)


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


def test_amendment_preview_surfaces_obligation_rollover(tmp_path):
    store = Store(str(tmp_path / "amendment-obligations.db"))
    original = ContractData(
        contract="Acme Cloud Services", start_date=date(2026, 1, 1),
        expiration_date=date(2027, 1, 1), renewal_notice_days=30,
    )
    _save_confirmed(store, "original", original)
    old_obligation = ObligationCreateRequest(
        category="insurance_certificate", title="Submit annual certificate",
        due_date=date(2026, 8, 1), recurrence_interval_months=12,
        evidence=Evidence(field="obligation:insurance_certificate", quote="Submit an annual certificate."),
        owner_email="owner@example.com",
    )
    store.create_contract_obligation(
        "original", old_obligation.model_dump(), None, old_obligation.owner_email, "reviewer", "acme",
    )
    proposed_obligation = ContractObligation(
        category="insurance_certificate", title="Submit updated annual certificate",
        due_date=date(2027, 7, 1), recurrence_interval_months=12,
        evidence=Evidence(field="obligation:insurance_certificate", quote="Updated certificate due annually."),
    )
    proposal = ContractData(
        contract="Acme Cloud Services", start_date=date(2027, 1, 1),
        expiration_date=date(2028, 1, 1), renewal_notice_days=90,
        supersedes_contract_id="original", obligations=[proposed_obligation],
    )
    store.save_contract("proposal", "amendment.pdf", "rules", proposal, "source", "reviewer", "acme")

    preview = store.preview_contract_confirmation("proposal", proposal, "acme")

    assert preview["changes"]["obligations"]["before"][0]["title"] == "Submit annual certificate"
    assert preview["changes"]["obligations"]["after"][0]["title"] == "Submit updated annual certificate"
    assert preview["impact"]["open_obligations_to_review"][0]["due_date"] == "2026-08-01"
    assert preview["impact"]["proposed_obligations_to_review"][0]["recurrence_interval_months"] == 12


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
