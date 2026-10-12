from datetime import date
from decimal import Decimal

from renewal_radar.extractor import RulesExtractor
from renewal_radar.schemas import ContractData, ObligationCreateRequest
from renewal_radar.store import Store


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
