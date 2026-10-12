from datetime import date

import pytest

from renewal_radar.schemas import ContractData, RenewalOutcomeRequest
from renewal_radar.store import Store


def test_supplier_canonicalization_and_portfolio_rollup(tmp_path):
    store = Store(str(tmp_path / "suppliers.db"))
    terms = ContractData(
        contract="Cloud services", expiration_date=date(2027, 1, 1), renewal_notice_days=90,
        annual_contract_value="12000.00", currency="USD",
    )
    store.save_contract("contract-1", "agreement.pdf", "rules", terms, "redacted", "reviewer", "acme")
    store.confirm_contract("contract-1", terms, "reviewer", "acme")
    supplier = store.create_supplier("Acme Cloud", "reviewer", "acme")

    with pytest.raises(ValueError, match="already exists"):
        store.create_supplier("  ACME   CLOUD  ", "reviewer", "acme")

    assert store.assign_contract_supplier("contract-1", supplier["id"], "reviewer", "acme")
    portfolio = store.list_suppliers("acme")
    assert portfolio[0]["contract_count"] == 1
    assert portfolio[0]["active_contract_count"] == 1
    assert portfolio[0]["open_renewal_count"] == 1
    assert portfolio[0]["annual_spend_at_risk"] == {"USD": "12000.00"}
    assert store.list_suppliers("another-tenant") == []


def test_recorded_renewal_outcome_closes_task_and_rolls_up_value(tmp_path):
    store = Store(str(tmp_path / "outcomes.db"))
    terms = ContractData(contract="Cloud services", expiration_date=date(2027, 1, 1), renewal_notice_days=90)
    store.save_contract("contract-1", "agreement.pdf", "rules", terms, "redacted", "reviewer", "acme")
    task_id = store.confirm_contract("contract-1", terms, "reviewer", "acme")
    request = RenewalOutcomeRequest(
        outcome="renegotiated", actual_expiration_date=date(2029, 1, 1),
        actual_annual_value="9500.00", annual_savings="2500.00", currency="USD",
        notes="Two-year renewal after negotiation.",
    )

    outcome = store.record_renewal_outcome(task_id, request.model_dump(), "reviewer", "acme")
    task = store.get_task(task_id, "acme")
    assert outcome["outcome"] == "renegotiated"
    assert task["status"] == "resolved"
    assert task["workflow_state"] == "renewed"
    assert store.renewal_outcome_summary("acme") == {
        "total": 1,
        "by_outcome": {"renegotiated": 1},
        "actual_annual_value_by_currency": {"USD": "9500.00"},
        "annual_savings_by_currency": {"USD": "2500.00"},
    }
    with pytest.raises(ValueError, match="already been recorded"):
        store.record_renewal_outcome(task_id, request.model_dump(), "reviewer", "acme")


def test_renewal_outcome_requires_currency_for_amounts_and_expiration_for_renewal():
    with pytest.raises(ValueError, match="currency is required"):
        RenewalOutcomeRequest(outcome="terminated", annual_savings="100")
    with pytest.raises(ValueError, match="actual_expiration_date is required"):
        RenewalOutcomeRequest(outcome="renewed")
