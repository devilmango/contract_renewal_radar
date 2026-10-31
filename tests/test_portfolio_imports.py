import asyncio
from datetime import date
from decimal import Decimal
from io import BytesIO

from fastapi import HTTPException
from starlette.datastructures import UploadFile

from renewal_radar.api import _portfolio_xlsx, create_app
from renewal_radar.auth import AuthRegistry, Principal
from renewal_radar.schemas import ContractData
from renewal_radar.store import Store


def _route(app, path, method):
    return next(route.endpoint for route in app.routes if route.path == path and method in route.methods)


def _upload(filename: str, content: bytes) -> UploadFile:
    return UploadFile(filename=filename, file=BytesIO(content))


def test_csv_import_preview_duplicate_detection_commit_and_report(tmp_path):
    store = Store(str(tmp_path / "imports.db"))
    app = create_app(store, AuthRegistry([]))
    preview = _route(app, "/portfolio/import/preview", "POST")
    commit = _route(app, "/portfolio/import/{batch_id}/commit", "POST")
    get_report = _route(app, "/portfolio/import/{batch_id}", "GET")
    reviewer = Principal("reviewer", frozenset({"reviewer"}), "legal@example.com", "acme")

    suppliers = asyncio.run(preview(
        "suppliers", _upload("suppliers.csv", b"Supplier Name,Risk\nAcme Cloud,high\nAcme Cloud,medium\n"),
        '{"name":"Supplier Name","risk_level":"Risk"}', reviewer,
    ))
    assert (suppliers["ready_count"], suppliers["duplicate_count"]) == (1, 1)
    supplier_import = commit(suppliers["batch_id"], reviewer)
    assert supplier_import["imported_count"] == 1
    assert supplier_import["rows"][0]["status"] == "imported"

    contracts = asyncio.run(preview(
        "contracts", _upload("contracts.csv", (
            "Agreement,Effective,Expires,Notice,Auto renew,Annual value,Currency,Vendor,Owner email\n"
            "Cloud Hosting,2026-01-01,2027-01-01,90,true,12000,USD,Acme Cloud,owner@example.com\n"
            "Missing expiry,2026-01-01,,,,100,USD,Acme Cloud,owner@example.com\n"
        ).encode()), "{}", reviewer,
    ))
    assert contracts["ready_count"] == 1 and contracts["invalid_count"] == 1
    committed = commit(contracts["batch_id"], reviewer)
    contract_id = committed["rows"][0]["record_id"]
    record = store.get_contract(contract_id, "acme")
    assert record["status"] == "active"
    assert store.list_suppliers("acme")[0]["contract_count"] == 1

    obligations = asyncio.run(preview(
        "obligations", _upload("obligations.csv", (
            "Contract,Category,Title,Due,Owner,Quote\n"
            f"{contract_id},insurance_certificate,Send certificate,2026-08-01,owner@example.com,Provide an annual certificate.\n"
        ).encode()),
        '{"contract_id":"Contract","category":"Category","title":"Title","due_date":"Due","owner_email":"Owner","evidence_quote":"Quote"}',
        reviewer,
    ))
    imported_obligation = commit(obligations["batch_id"], reviewer)
    assert imported_obligation["imported_count"] == 1
    obligation = store.get_contract_obligation(imported_obligation["rows"][0]["record_id"], "acme")
    assert obligation["evidence"]["quote"] == "Provide an annual certificate."
    report = get_report(contracts["batch_id"], reviewer)
    assert report["status"] == "committed"
    assert report["events"][0]["status"] == "imported"


def test_xlsx_import_parser_and_tenant_scope(tmp_path):
    store = Store(str(tmp_path / "xlsx-import.db"))
    app = create_app(store, AuthRegistry([]))
    preview = _route(app, "/portfolio/import/preview", "POST")
    xlsx = _portfolio_xlsx([{"name": "Example Supplier", "risk_level": "medium"}], "suppliers")
    reviewer = Principal("reviewer", frozenset({"reviewer"}), tenant_id="acme")
    result = asyncio.run(preview("suppliers", _upload("suppliers.xlsx", xlsx), "{}", reviewer))
    assert result["ready_count"] == 1
    outsider = Principal("reviewer", frozenset({"reviewer"}), tenant_id="other")
    get_report = _route(app, "/portfolio/import/{batch_id}", "GET")
    with __import__("pytest").raises(HTTPException) as missing:
        get_report(result["batch_id"], outsider)
    assert missing.value.status_code == 404


def test_renewal_decision_tracks_review_milestones_quotes_and_linked_forecast(tmp_path):
    store = Store(str(tmp_path / "decisions.db"))
    terms = ContractData(contract="Hosted service", expiration_date=date(2027, 1, 1),
                         annual_contract_value=Decimal("1000"), currency="USD")
    store.save_contract("contract", "agreement.pdf", "rules", terms, "redacted source", "reviewer", "acme")
    task_id = store.confirm_contract("contract", terms, "reviewer", "acme")
    forecast = store.renewal_forecast("acme", date(2026, 1, 1), date(2027, 1, 1), Decimal("10"), Decimal("10"))
    forecast_id = store.record_renewal_forecast("acme", forecast, "reviewer")
    decision = store.create_renewal_decision({"task_id": task_id, "strategy": "negotiate", "reason": "Reduce renewal cost",
                                              "forecast_id": forecast_id, "owner_email": "owner@example.com"}, "reviewer", "acme")
    assert decision["forecast_run_id"] == forecast_id
    milestone = store.add_renewal_decision_milestone(decision["id"], "acme", "reviewer", {"title": "Request revised quote", "due_date": date(2026, 11, 1)})
    store.update_renewal_decision_milestone(decision["id"], milestone["id"], "acme", "reviewer", True, "Sent request")
    quote = store.add_renewal_decision_quote(decision["id"], "acme", "reviewer", {"supplier": "CloudCo", "amount": Decimal("900"), "currency": "USD"})
    assert quote["amount"] == "900"
    store.submit_renewal_decision(decision["id"], "acme", "reviewer")
    store.review_renewal_decision(decision["id"], "acme", "legal", True, "Approved negotiation")
    outcome = store.complete_renewal_decision(decision["id"], "acme", {
        "outcome": "renegotiated", "actual_annual_value": Decimal("900"), "annual_savings": Decimal("100"),
        "currency": "USD", "actual_expiration_date": date(2028, 1, 1), "notes": "Renewed at lower rate",
    }, "reviewer")
    assert outcome["task_id"] == task_id
    saved = store.get_renewal_decision(decision["id"], "acme")
    assert saved["status"] == "completed"
    assert saved["milestones"][0]["completed"]
    assert saved["quotes"][0]["id"] == quote["id"]
    assert saved["events"][-1]["event_type"] == "completed"
    accuracy = store.renewal_forecast_accuracy("acme")
    renegotiation = next(row for row in accuracy["by_supplier_and_scenario"] if row["scenario"] == "renegotiate")
    assert renegotiation["mean_predicted_annual_value"] == "900.00"
