from datetime import date
from decimal import Decimal
import base64
import hashlib

import pytest
from fastapi import HTTPException

from renewal_radar.api import create_app
from renewal_radar.auth import AuthRegistry, Principal
from renewal_radar.extractor import RulesExtractor
from renewal_radar.schemas import ContractData, ObligationCreateRequest, ObligationScheduleUpdateRequest, RenewalForecastRequest
from renewal_radar.store import Store


def _complete_with_signoff(store, obligation_id, actor="owner", tenant="acme"):
    obligation = store.get_contract_obligation(obligation_id, tenant)
    payload = b"redacted compliance evidence"
    evidence = store.submit_obligation_evidence(
        obligation_id, tenant, actor, "proof.txt", "text/plain",
        base64.b64encode(payload).decode(), hashlib.sha256(payload).hexdigest(), "Completed as required",
    )
    store.review_obligation_evidence(evidence["id"], tenant, "reviewer", "approved", "Verified")


def test_rules_extractor_proposes_dated_obligations_with_evidence():
    extracted = RulesExtractor().extract(
        "Agreement title: Hosted Platform Agreement. Effective Date: January 1, 2026. "
        "Expiration Date: December 31, 2026. The supplier must provide a certificate of insurance "
        "by March 1, 2026. The annual price increase takes effect July 1, 2026."
    )

    assert {(item.category, item.due_date) for item in extracted.obligations} == {
        ("insurance_certificate", date(2026, 3, 1)),
        ("price_increase", date(2026, 7, 1)),
    }
    assert all(item.evidence.quote and item.evidence.field.startswith("obligation:") for item in extracted.obligations)


def test_scheduling_extracted_obligation_creates_owned_reminder_task(tmp_path):
    store = Store(str(tmp_path / "obligations.db"))
    terms = ContractData(
        contract="Hosting", start_date=date(2026, 1, 1), expiration_date=date(2026, 12, 31),
        renewal_notice_days=30,
    )
    store.save_contract("contract-1", "hosting.pdf", "rules", terms, "redacted", "reviewer", "acme")
    store.confirm_contract("contract-1", terms, "reviewer", "acme")
    request = ObligationCreateRequest(
        category="insurance_certificate", title="Submit annual insurance certificate",
        due_offset_days=30, due_offset_from="expiration_date", due_offset_direction="before",
        evidence={"field": "obligation:insurance_certificate", "quote": "Certificate due 30 days before expiration."},
        owner_name="Alex Owner", owner_email="alex@example.com",
    )

    saved = store.create_contract_obligation(
        "contract-1", request.model_dump(), request.owner_name, request.owner_email, "reviewer", "acme",
    )
    assert saved["due_date"] == "2026-12-01"
    assert saved["owner_email"] == "alex@example.com"
    assert saved["evidence"]["quote"] == "Certificate due 30 days before expiration."
    tasks = store.list_tasks(status="open", tenant_id="acme", kind="obligation")
    assert len(tasks) == 1
    assert tasks[0]["task_kind"] == "obligation"
    assert store.list_contract_obligations("acme", status="open")[0]["task_id"] == saved["task_id"]


def test_recurring_obligation_creates_calendar_safe_occurrences_until_expiration(tmp_path):
    store = Store(str(tmp_path / "recurring-obligations.db"))
    terms = ContractData(
        contract="Hosting", start_date=date(2026, 1, 1), expiration_date=date(2026, 3, 31),
        renewal_notice_days=30,
    )
    store.save_contract("contract-1", "hosting.pdf", "rules", terms, "redacted", "reviewer", "acme")
    store.confirm_contract("contract-1", terms, "reviewer", "acme")
    request = ObligationCreateRequest(
        category="insurance_certificate", title="Submit insurance certificate",
        due_date=date(2026, 1, 31), recurrence_interval_months=1,
        evidence={"field": "obligation:insurance_certificate", "quote": "Submit monthly certificates."},
        owner_email="alex@example.com",
    )
    first = store.create_contract_obligation(
        "contract-1", request.model_dump(), None, request.owner_email, "reviewer", "acme",
    )

    _complete_with_signoff(store, first["id"])
    occurrences = store.list_contract_obligations("acme")
    assert [(item["due_date"], item["status"]) for item in occurrences] == [
        ("2026-01-31", "resolved"), ("2026-02-28", "open"),
    ]
    second = next(item for item in occurrences if item["status"] == "open")
    _complete_with_signoff(store, second["id"])
    third = next(item for item in store.list_contract_obligations("acme") if item["status"] == "open")
    assert third["due_date"] == "2026-03-28"
    _complete_with_signoff(store, third["id"])
    assert len(store.list_contract_obligations("acme")) == 3


def test_rules_extractor_detects_explicit_annual_obligation_schedule():
    extracted = RulesExtractor().extract(
        "Agreement title: Hosting. Effective Date: January 1, 2026. Expiration Date: December 31, 2026. "
        "The supplier must provide a certificate of insurance by March 1, 2026 and annually thereafter."
    )
    obligation = next(item for item in extracted.obligations if item.category == "insurance_certificate")
    assert obligation.recurrence_interval_months == 12


def test_forecast_compares_renew_renegotiate_and_terminate_by_currency(tmp_path):
    store = Store(str(tmp_path / "forecast.db"))
    terms = ContractData(
        contract="Cloud platform", start_date=date(2026, 1, 1), expiration_date=date(2026, 12, 31),
        renewal_notice_days=30, annual_contract_value=Decimal("36500.00"), currency="USD",
    )
    store.save_contract("contract-1", "cloud.pdf", "rules", terms, "redacted", "reviewer", "acme")
    store.confirm_contract("contract-1", terms, "reviewer", "acme")

    forecast = store.renewal_forecast(
        "acme", date(2026, 12, 1), date(2027, 1, 31), Decimal("10"), Decimal("20"),
    )

    assert forecast["scenarios"]["renew"]["projected_cost_by_currency"] == {"USD": "6510.00"}
    assert forecast["scenarios"]["renegotiate"]["projected_cost_by_currency"] == {"USD": "5580.00"}
    assert forecast["scenarios"]["terminate"]["projected_cost_by_currency"] == {"USD": "3100.00"}
    assert forecast["scenarios"]["renew"]["notice_deadlines_by_month"] == {"2026-12": 1}
    assert forecast["scenarios"]["renew"]["expirations_by_month"] == {"2026-12": 1}


def test_recurring_schedule_can_pause_change_cadence_and_reschedule(tmp_path):
    store = Store(str(tmp_path / "schedule-controls.db"))
    terms = ContractData(contract="Hosting", expiration_date=date(2027, 12, 31))
    store.save_contract("contract", "hosting.pdf", "rules", terms, "source", "reviewer", "acme")
    store.confirm_contract("contract", terms, "reviewer", "acme")
    request = ObligationCreateRequest(
        category="insurance_certificate", title="Submit certificate", due_date=date(2026, 1, 15),
        recurrence_interval_months=12,
        evidence={"field": "obligation:insurance_certificate", "quote": "Submit this each year."},
        owner_email="owner@example.com",
    )
    first = store.create_contract_obligation("contract", request.model_dump(), None, request.owner_email, "reviewer", "acme")

    paused = store.update_obligation_schedule(first["id"], "acme", "reviewer", status="paused")
    _complete_with_signoff(store, first["id"])
    assert paused["schedule_status"] == "paused"
    assert len(store.list_contract_obligations("acme")) == 1

    active = store.update_obligation_schedule(first["id"], "acme", "reviewer", status="active", recurrence_interval_months=6)
    assert active["recurrence_interval_months"] == 6
    other = request.model_copy(update={"title": "Submit SOC report", "due_date": date(2026, 2, 15)})
    recurring = store.create_contract_obligation("contract", other.model_dump(), None, request.owner_email, "reviewer", "acme")
    store.update_obligation_schedule(recurring["id"], "acme", "reviewer", recurrence_interval_months=6)
    moved = store.reschedule_obligation(recurring["id"], date(2026, 3, 5), "acme", "reviewer")
    assert moved["due_date"] == "2026-03-05"
    _complete_with_signoff(store, recurring["id"])
    next_row = next(item for item in store.list_contract_obligations("acme") if item["status"] == "open")
    assert next_row["due_date"] == "2026-09-05"

    ended = store.update_obligation_schedule(next_row["id"], "acme", "reviewer", status="ended")
    assert ended["schedule_status"] == "ended"
    _complete_with_signoff(store, next_row["id"])
    assert len([item for item in store.list_contract_obligations("acme") if item["recurrence_group_id"] == recurring["recurrence_group_id"]]) == 2


def test_forecast_accuracy_compares_scenarios_and_recommends_historical_assumptions(tmp_path):
    store = Store(str(tmp_path / "forecast-accuracy.db"))
    terms = ContractData(
        contract="Cloud", expiration_date=date(2026, 12, 31), annual_contract_value=Decimal("1000"), currency="USD",
    )
    store.save_contract("contract", "cloud.pdf", "rules", terms, "source", "reviewer", "acme")
    task_id = store.confirm_contract("contract", terms, "reviewer", "acme")
    supplier = store.create_supplier("CloudCo", "reviewer", "acme")
    store.assign_contract_supplier("contract", supplier["id"], "reviewer", "acme")
    forecast = store.renewal_forecast("acme", date(2026, 1, 1), date(2027, 1, 1), Decimal("10"), Decimal("20"))
    store.record_renewal_forecast("acme", forecast, "reviewer")
    store.record_renewal_outcome(task_id, {
        "outcome": "renegotiated", "actual_annual_value": Decimal("950"), "annual_savings": Decimal("50"),
        "currency": "USD", "actual_expiration_date": date(2028, 12, 31),
    }, "reviewer", "acme")

    accuracy = store.renewal_forecast_accuracy("acme")

    assert accuracy["outcome_count"] == 1
    assert {(item["scenario"], item["supplier"]) for item in accuracy["by_supplier_and_scenario"]} == {
        ("renew", "CloudCo"), ("renegotiate", "CloudCo"), ("terminate", "CloudCo"),
    }
    assumptions = accuracy["recommended_assumptions"]
    assert assumptions["renegotiation_savings_pct"] == "5.00"
    assert assumptions["renewal_increase_pct"] is None
    assert accuracy["by_supplier_and_scenario"][0]["mean_variance"] is not None


def test_schedule_controls_are_reviewer_only_and_forecast_generation_is_snapshotted(tmp_path):
    store = Store(str(tmp_path / "reviewer-controls.db"))
    terms = ContractData(contract="Cloud", expiration_date=date(2027, 1, 1), annual_contract_value=Decimal("1000"), currency="USD")
    store.save_contract("contract", "cloud.pdf", "rules", terms, "source", "reviewer", "acme")
    store.confirm_contract("contract", terms, "reviewer", "acme")
    obligation = ObligationCreateRequest(
        category="insurance_certificate", title="Submit certificate", due_date=date(2026, 6, 1), recurrence_interval_months=12,
        evidence={"field": "obligation:insurance_certificate", "quote": "Submit this yearly."}, owner_email="owner@example.com",
    )
    scheduled = store.create_contract_obligation("contract", obligation.model_dump(), None, obligation.owner_email, "reviewer", "acme")
    app = create_app(store, AuthRegistry([]))
    update = next(route.endpoint for route in app.routes if route.path == "/obligations/{obligation_id}/schedule")
    forecast = next(route.endpoint for route in app.routes if route.path == "/renewal-forecast" and "POST" in route.methods)
    accuracy = next(route.endpoint for route in app.routes if route.path == "/renewal-forecast/accuracy")
    reviewer = Principal("reviewer", frozenset({"reviewer"}), tenant_id="acme")
    owner = Principal("owner", frozenset({"owner"}), email="owner@example.com", tenant_id="acme")

    assert update(scheduled["id"], ObligationScheduleUpdateRequest(status="paused"), reviewer)["schedule_status"] == "paused"
    with pytest.raises(HTTPException) as error:
        update(scheduled["id"], ObligationScheduleUpdateRequest(status="ended"), owner)
    assert error.value.status_code == 403
    result = forecast(RenewalForecastRequest(start_date=date(2026, 1, 1), end_date=date(2027, 1, 1)), reviewer)
    assert result["forecast_id"]
    assert accuracy(reviewer)["outcome_count"] == 0
