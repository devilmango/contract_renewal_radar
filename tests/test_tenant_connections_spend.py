from datetime import date

import pytest
from fastapi import HTTPException

from renewal_radar.api import create_app
from renewal_radar.auth import AuthRegistry, Principal
from renewal_radar.integrations import decrypt_credentials, encrypt_credentials
from renewal_radar.extractor import RulesExtractor
from renewal_radar.schemas import ContractData, IntegrationConnectionRequest
from renewal_radar.store import Store


def test_integration_credentials_are_encrypted_and_tenant_bound(monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", "ab" * 32)
    encrypted = encrypt_credentials("tenant-a", "calendar", {"refresh_token": "secret"})

    assert "secret" not in encrypted
    assert decrypt_credentials("tenant-a", "calendar", encrypted) == {"refresh_token": "secret"}
    with pytest.raises(Exception):
        decrypt_credentials("tenant-b", "calendar", encrypted)


def test_connection_api_redacts_secrets_and_limits_mutations_to_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", "cd" * 32)
    store = Store(str(tmp_path / "connections.db"))
    app = create_app(store, AuthRegistry([]))
    put = next(r.endpoint for r in app.routes if r.path == "/integrations/connections/{integration}" and "PUT" in r.methods)
    list_connections = next(r.endpoint for r in app.routes if r.path == "/integrations/connections" and "GET" in r.methods)
    admin = Principal("admin", frozenset({"admin"}), tenant_id="acme")
    reviewer = Principal("reviewer", frozenset({"reviewer"}), tenant_id="acme")
    request = IntegrationConnectionRequest(
        provider="google_calendar", settings={"calendar_id": "primary"},
        credentials={"refresh_token": "private", "client_id": "client", "client_secret": "secret"},
    )

    with pytest.raises(HTTPException) as error:
        put("calendar", request, reviewer)
    assert error.value.status_code == 403
    saved = put("calendar", request, admin)
    assert saved["credentials_configured"] is True
    assert "credentials" not in saved and "ciphertext" not in saved
    assert list_connections(reviewer)[0]["provider"] == "google_calendar"


def test_risk_snapshot_reports_confirmed_annual_values_per_currency(tmp_path):
    store = Store(str(tmp_path / "spend.db"))
    terms = ContractData(
        contract="Cloud Services", expiration_date=date(2027, 1, 1), renewal_notice_days=90,
        owner_email="owner@example.com", annual_contract_value="12500.50", currency="USD",
    )
    store.save_contract("usd", "usd.pdf", "rules", terms, "redacted", "reviewer", "acme")
    store.confirm_contract("usd", terms, "reviewer", "acme")
    overdue = ContractData(
        contract="Hosting", expiration_date=date(2027, 1, 1), renewal_notice_days=90,
        annual_contract_value="4000", currency="EUR",
    )
    store.save_contract("eur", "eur.pdf", "rules", overdue, "redacted", "reviewer", "acme")
    store.confirm_contract("eur", overdue, "reviewer", "acme")

    snapshot = store.renewal_risk_snapshot("acme", date(2026, 10, 1))
    assert snapshot["summary"]["annual_spend_at_risk"] == {"EUR": "4000.00", "USD": "12500.50"}
    assert snapshot["summary"]["annual_spend_high_risk"] == {"EUR": "4000.00"}
    assert snapshot["tasks"][0]["currency"] == "EUR"


def test_rules_extractor_captures_explicit_annual_spend_with_evidence():
    extracted = RulesExtractor().extract(
        "Agreement title: Cloud services. Effective Date: January 1, 2026. "
        "Expiration Date: January 1, 2027. Annual contract value: USD 12,500.50."
    )

    assert str(extracted.annual_contract_value) == "12500.50"
    assert extracted.currency == "USD"
    assert any(item.field == "annual_contract_value" for item in extracted.evidence)
