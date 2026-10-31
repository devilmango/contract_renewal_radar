"""Tenant-scoped procurement portfolio reconciliation for Coupa and SAP Ariba APIs."""
from __future__ import annotations

import hashlib
import json
import socket
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .integrations import decrypt_credentials
from .schemas import ContractData
from .store import Store


class ProcurementSyncError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_procurement_base_url(value: str, *, resolve: bool = True) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Procurement API base_url must be an HTTPS origin or path without credentials, query, or fragment")
    hostname = parsed.hostname.casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Procurement API host must be publicly routable")
    if resolve:
        try:
            addresses = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            if not addresses or any(not _public_ip(item[4][0]) for item in addresses):
                raise ValueError("Procurement API host must resolve only to public addresses")
        except socket.gaierror as exc:
            raise ValueError("Procurement API host could not be resolved") from exc
    return value.rstrip("/") + "/"


def sync_procurement(store: Store, tenant_id: str, *, provider: str | None = None) -> dict:
    connection = store.get_integration_connection(tenant_id, "procurement")
    if not connection:
        raise ProcurementSyncError("Configure a Coupa or SAP Ariba procurement connection first")
    configured_provider = connection["provider"]
    if provider and configured_provider != provider:
        raise ProcurementSyncError("Requested procurement provider does not match the tenant connection")
    if configured_provider not in {"coupa", "sap_ariba"}:
        raise ProcurementSyncError(f"Unsupported procurement provider: {configured_provider}")
    run_id = str(uuid4())
    store.start_procurement_sync(run_id, tenant_id, configured_provider)
    summary = {"run_id": run_id, "provider": configured_provider, "suppliers_seen": 0,
               "contracts_seen": 0, "imported": 0, "duplicates": 0, "conflicts": []}
    try:
        settings = connection["settings"]
        base_url = validate_procurement_base_url(settings.get("base_url", ""))
        credentials = decrypt_credentials(tenant_id, "procurement", connection["credentials_ciphertext"])
        credential = credentials.get("api_key" if configured_provider == "coupa" else "access_token")
        if not credential:
            raise ProcurementSyncError("The procurement connection is missing its API credential")
        headers = {"Accept": "application/json"}
        if configured_provider == "coupa":
            headers["X-Coupa-API-Key"] = credential
        else:
            headers["Authorization"] = f"Bearer {credential}"
        supplier_path = settings.get("suppliers_path", "api/suppliers")
        contract_path = settings.get("contracts_path", "api/contracts")
        suppliers = _fetch_records(_endpoint_url(base_url, supplier_path), headers)
        contracts = _fetch_records(_endpoint_url(base_url, contract_path), headers)
        summary["suppliers_seen"], summary["contracts_seen"] = len(suppliers), len(contracts)
        supplier_map = {}
        for raw in suppliers:
            external_id = _field(raw, "id", "supplier_id", "SupplierId", "supplierNumber", "number")
            name = _field(raw, "name", "displayName", "supplier_name", "SupplierName", "organizationName")
            if not name:
                summary["conflicts"].append({"record_type": "supplier", "external_id": str(external_id or ""), "reason": "missing_supplier_name"})
                continue
            supplier = store.find_or_create_procurement_supplier(tenant_id, configured_provider, str(external_id or name), str(name))
            supplier_map[str(external_id or name)] = supplier["id"]
        for raw in contracts:
            external_id = _field(raw, "id", "contract_id", "ContractId", "contractNumber", "number")
            name = _field(raw, "name", "title", "contract_name", "ContractName", "description")
            if not external_id or not name:
                summary["conflicts"].append({"record_type": "contract", "external_id": str(external_id or ""), "reason": "missing_id_or_name"})
                continue
            revision = str(_field(raw, "revision", "version", "updated_at", "updatedAt", "lastModifiedDateTime") or
                           hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:20])
            state = store.procurement_contract_revision(tenant_id, configured_provider, str(external_id))
            if state and state["revision"] == revision:
                summary["duplicates"] += 1
                continue
            if state:
                summary["conflicts"].append({"record_type": "contract", "external_id": str(external_id),
                                             "reason": "new_revision_requires_review", "existing_revision": state["revision"],
                                             "incoming_revision": revision, "existing_contract_id": state["contract_id"]})
                continue
            try:
                terms = _contract_terms(raw, str(name))
            except ValueError as exc:
                summary["conflicts"].append({"record_type": "contract", "external_id": str(external_id), "reason": str(exc)})
                continue
            supplier_external_id = _field(raw, "supplier_id", "supplierId", "SupplierId", "supplier_number", "supplierNumber")
            supplier_name = _field(raw, "supplier_name", "supplierName", "SupplierName", "supplier")
            supplier_id = supplier_map.get(str(supplier_external_id)) if supplier_external_id else None
            if not supplier_id and supplier_name:
                supplier = store.find_or_create_procurement_supplier(tenant_id, configured_provider,
                                                                      str(supplier_external_id or supplier_name), str(supplier_name))
                supplier_id = supplier["id"]
            source_url = _field(raw, "url", "source_url", "sourceUrl", "selfLink")
            contract_id, created = store.save_external_contract(
                str(uuid4()), f"{configured_provider}-{external_id}.json", "structured_import", terms,
                json.dumps(raw, sort_keys=True, default=str), tenant_id, configured_provider, "contracts",
                str(external_id), revision, str(source_url) if source_url else None,
            )
            if created:
                summary["imported"] += 1
                if supplier_id:
                    store.assign_contract_supplier(contract_id, supplier_id, f"integration:{configured_provider}", tenant_id)
        store.finish_procurement_sync(run_id, tenant_id, "completed", summary)
        store.update_integration_health(tenant_id, "procurement", "connected")
        return summary
    except Exception as exc:
        store.finish_procurement_sync(run_id, tenant_id, "failed", summary, f"{type(exc).__name__}: {exc}")
        store.update_integration_health(tenant_id, "procurement", "error", str(exc)[:1000])
        raise


def check_procurement_connection(tenant_id: str, connection: dict) -> dict:
    provider = connection["provider"]
    settings = connection["settings"]
    base_url = validate_procurement_base_url(settings.get("base_url", ""))
    credentials = decrypt_credentials(tenant_id, "procurement", connection["credentials_ciphertext"])
    credential = credentials.get("api_key" if provider == "coupa" else "access_token")
    if not credential:
        raise ProcurementSyncError("The procurement connection is missing its API credential")
    headers = {"Accept": "application/json"}
    headers["X-Coupa-API-Key" if provider == "coupa" else "Authorization"] = credential if provider == "coupa" else f"Bearer {credential}"
    path = settings.get("suppliers_path", "api/suppliers")
    count = len(_fetch_records(_endpoint_url(base_url, path), headers))
    return {"message": f"Connection passed; read {count} supplier record(s)."}


def _fetch_records(url: str, headers: dict[str, str]) -> list[dict]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ProcurementSyncError("Procurement endpoint paths must resolve to HTTPS")
    request = Request(url, headers=headers)
    try:
        with build_opener(_NoRedirect).open(request, timeout=20) as response:
            payload = json.loads(response.read(5_000_001))
    except HTTPError as exc:
        raise ProcurementSyncError(f"Procurement API returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ProcurementSyncError(f"Procurement API request failed: {type(exc).__name__}") from exc
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict):
        values = next((payload[key] for key in ("items", "value", "data", "results", "records") if isinstance(payload.get(key), list)), None)
    else:
        values = None
    if values is None or any(not isinstance(item, dict) for item in values):
        raise ProcurementSyncError("Procurement response must contain an array of JSON records")
    return values


def _endpoint_url(base_url: str, path: str) -> str:
    relative = urlsplit(path)
    if relative.scheme or relative.netloc or relative.query or relative.fragment:
        raise ProcurementSyncError("Procurement endpoint paths must be relative paths without query strings")
    url = urljoin(base_url, path.lstrip("/"))
    if urlsplit(url).hostname != urlsplit(base_url).hostname:
        raise ProcurementSyncError("Procurement endpoint must use the configured API host")
    return url


def _contract_terms(raw: dict, name: str) -> ContractData:
    start = _date(_field(raw, "start_date", "startDate", "effectiveDate", "effective_date"))
    expiration = _date(_field(raw, "expiration_date", "expirationDate", "endDate", "end_date"))
    notice = _field(raw, "renewal_notice_days", "renewalNoticeDays", "noticePeriodDays", "notice_period_days")
    value = _field(raw, "annual_contract_value", "annualValue", "annual_value", "contractValue", "amount")
    currency = _field(raw, "currency", "currencyCode", "Currency")
    auto_renew = _field(raw, "auto_renew", "autoRenew", "auto_renewal")
    if isinstance(auto_renew, str):
        auto_renew = auto_renew.strip().casefold() in {"true", "yes", "1", "auto-renew", "automatic"}
    try:
        return ContractData(contract=name, start_date=start, expiration_date=expiration,
                            renewal_notice_days=int(notice) if notice not in (None, "") else None,
                            annual_contract_value=Decimal(str(value)) if value not in (None, "") else None,
                            currency=str(currency).upper() if currency else None, auto_renew=auto_renew,
                            owner_email=_field(raw, "owner_email", "ownerEmail"),
                            owner_name=_field(raw, "owner_name", "ownerName"))
    except (ValueError, InvalidOperation, TypeError) as exc:
        raise ValueError(f"invalid_contract_terms: {exc}") from exc


def _date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"invalid_date:{value}") from exc


def _field(record: dict, *names: str):
    folded = {str(key).casefold(): value for key, value in record.items()}
    for name in names:
        if name.casefold() in folded:
            return folded[name.casefold()]
    return None


def _public_ip(value: str) -> bool:
    import ipaddress
    address = ipaddress.ip_address(value)
    return address.is_global
