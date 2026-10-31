import json
from datetime import date

from renewal_radar import procurement_sync
from renewal_radar.deadlines import calculate_deadline
from renewal_radar.integrations import encrypt_credentials
from renewal_radar.api import create_app
from renewal_radar.auth import AuthRegistry, Principal
from renewal_radar.schemas import ContractData, DeadlineCalculationRequest
from renewal_radar.store import Store


def _contract(store, contract_id, terms, tenant="acme"):
    store.save_contract(contract_id, f"{contract_id}.pdf", "rules", terms, f"Source for {contract_id}", "reviewer", tenant)
    store.confirm_contract(contract_id, terms, "reviewer", tenant)


def test_relationship_graph_inherits_terms_and_combines_version_timeline(tmp_path):
    store = Store(str(tmp_path / "graph.db"))
    master = ContractData(contract="Enterprise Master", expiration_date=date(2028, 1, 1),
                          renewal_notice_days=90, auto_renew=True)
    _contract(store, "master", master)
    sow = ContractData(contract="Cloud Services SOW", expiration_date=date(2027, 1, 1))
    _contract(store, "sow", sow)
    relationship = store.link_contracts("sow", "master", "sow_under", "reviewer", "acme", "Governed by master terms")

    graph = store.contract_relationship_graph("sow", "acme")
    assert relationship["relationship_type"] == "sow_under"
    assert graph["effective_terms"]["renewal_notice_days"] == 90
    assert graph["term_sources"]["renewal_notice_days"] == "master"
    assert graph["term_sources"]["expiration_date"] == "sow"
    assert {node["id"] for node in graph["nodes"]} == {"master", "sow"}
    assert any(event["event_type"] == "contract.relationship_linked" for event in graph["timeline"])
    assert store.contract_relationship_graph("sow", "other") is None


def test_contract_inheritance_relationships_reject_cycles(tmp_path):
    import pytest

    store = Store(str(tmp_path / "cycle.db"))
    terms = ContractData(contract="Agreement", expiration_date=date(2028, 1, 1))
    _contract(store, "master", terms)
    _contract(store, "sow", terms)
    store.link_contracts("master", "sow", "master_of", "reviewer", "acme")
    with pytest.raises(ValueError, match="cycles"):
        store.link_contracts("master", "sow", "sow_under", "reviewer", "acme")


def test_business_deadline_explains_jurisdiction_holiday_timezone_and_weekend():
    result = calculate_deadline(date(2026, 7, 10), 5, "business", timezone="America/New_York",
                                jurisdiction="US-FEDERAL")
    assert result["due_date"] == "2026-07-02"  # July 3 is the observed Independence Day holiday.
    assert result["timezone"] == "America/New_York"
    assert "2026-07-03" in result["holiday_dates_used"]
    assert result["jurisdiction"] == "US-FEDERAL"

    friday_saturday = calculate_deadline(date(2026, 5, 11), 1, "business", timezone="Asia/Dubai",
                                         weekend_days={4, 5})
    assert friday_saturday["due_date"] == "2026-05-10"


def test_confirmed_task_persists_deadline_calculation_and_source(tmp_path):
    store = Store(str(tmp_path / "deadline.db"))
    terms = ContractData(contract="US Federal Agreement", expiration_date=date(2026, 7, 10),
                         renewal_notice_days=5, notice_day_type="business", notice_timezone="America/New_York",
                         notice_jurisdiction="US-FEDERAL")
    _contract(store, "deadline", terms)
    task = store.list_tasks(tenant_id="acme")[0]
    assert task["due_date"] == "2026-07-02"
    assert json.loads(task["deadline_calculation_json"])["jurisdiction"] == "US-FEDERAL"


def test_procurement_sync_imports_records_and_reports_new_revision_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", "11" * 32)
    store = Store(str(tmp_path / "procurement.db"))
    settings = {"base_url": "https://procurement.example/api/", "suppliers_path": "suppliers", "contracts_path": "contracts"}
    encrypted = encrypt_credentials("acme", "procurement", {"api_key": "secret"})
    store.save_integration_connection("acme", "procurement", "coupa", settings, encrypted, "admin")
    records = {
        "https://procurement.example/api/suppliers": [{"id": "sup-1", "name": "Acme Networks"}],
        "https://procurement.example/api/contracts": [{"id": "c-1", "revision": "1", "name": "Network Agreement",
            "supplier_id": "sup-1", "start_date": "2026-01-01", "expiration_date": "2027-01-01",
            "annual_value": "12000", "currency": "USD"}],
    }
    monkeypatch.setattr(procurement_sync, "validate_procurement_base_url", lambda value: "https://procurement.example/api/")
    monkeypatch.setattr(procurement_sync, "_fetch_records", lambda url, headers: records[url])

    first = procurement_sync.sync_procurement(store, "acme")
    assert first["imported"] == 1
    assert first["suppliers_seen"] == 1
    contract = store.list_contracts(tenant_id="acme")[0]
    assert contract["supplier_id"]
    assert ContractData.model_validate_json(contract["extracted_json"]).expiration_date == date(2027, 1, 1)

    duplicate = procurement_sync.sync_procurement(store, "acme")
    assert duplicate["duplicates"] == 1
    assert len(store.list_contracts(tenant_id="acme")) == 1

    records["https://procurement.example/api/contracts"][0]["revision"] = "2"
    second = procurement_sync.sync_procurement(store, "acme")
    assert second["imported"] == 0
    assert second["conflicts"][0]["reason"] == "new_revision_requires_review"
    history = store.procurement_sync_history("acme")
    assert history[0]["status"] == "completed"
    assert history[0]["conflicts"][0]["incoming_revision"] == "2"


def test_procurement_base_url_rejects_insecure_and_local_targets():
    import pytest

    with pytest.raises(ValueError, match="HTTPS"):
        procurement_sync.validate_procurement_base_url("http://procurement.example")
    with pytest.raises(ValueError, match="publicly routable"):
        procurement_sync.validate_procurement_base_url("https://localhost", resolve=False)
    with pytest.raises(procurement_sync.ProcurementSyncError, match="relative paths"):
        procurement_sync._endpoint_url("https://procurement.example/", "https://attacker.example/steal")


def test_deadline_explanation_api_and_procurement_workspace_are_available(tmp_path):
    store = Store(str(tmp_path / "api.db"))
    app = create_app(store, AuthRegistry([]))
    principal = Principal("reviewer", frozenset({"reviewer"}), tenant_id="acme")
    deadline_route = next(route.endpoint for route in app.routes if route.path == "/deadlines/calculate")
    result = deadline_route(DeadlineCalculationRequest(expiration_date=date(2026, 7, 10), notice_days=5,
                                 day_type="business", jurisdiction="US-FEDERAL"), principal)
    assert result["due_date"] == "2026-07-02"
    page = next(route.endpoint for route in app.routes if route.path == "/integrations/procurement")()
    assert b"Coupa" in page.body and b"SAP Ariba" in page.body
