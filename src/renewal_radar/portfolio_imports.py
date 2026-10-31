from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from uuid import uuid4
from xml.etree import ElementTree

from pydantic import ValidationError

from .schemas import ContractData, ObligationCreateRequest, SupplierCreateRequest


MAX_ROWS = 5000
MAX_UNPACKED_BYTES = 25 * 1024 * 1024

FIELDS = {
    "contracts": {"contract", "start_date", "expiration_date", "renewal_notice_days", "auto_renew",
                  "termination_notice", "owner_name", "owner_email", "annual_contract_value", "currency",
                  "supplier_id", "supplier_name"},
    "suppliers": {"name", "risk_level"},
    "obligations": {"contract_id", "category", "title", "description", "due_date",
                    "recurrence_interval_months", "owner_name", "owner_email", "evidence_quote"},
}

ALIASES = {
    "contract": ("contract", "contract_name", "agreement", "agreement_name", "vendor_agreement"),
    "start_date": ("start_date", "effective_date", "effective", "commencement_date"),
    "expiration_date": ("expiration_date", "end_date", "expires", "expiry_date", "expiration", "expiry"),
    "renewal_notice_days": ("renewal_notice_days", "notice", "notice_days", "renewal_notice_period_days"),
    "auto_renew": ("auto_renew", "auto_renewal", "automatic_renewal"),
    "termination_notice": ("termination_notice", "termination_notice_period"),
    "owner_name": ("owner_name", "owner", "assignee"),
    "owner_email": ("owner_email", "owner_email_address", "assignee_email"),
    "annual_contract_value": ("annual_contract_value", "annual_value", "annual_spend", "contract_value"),
    "currency": ("currency", "currency_code"),
    "supplier_id": ("supplier_id",),
    "supplier_name": ("supplier_name", "vendor", "vendor_name"),
    "name": ("name", "supplier", "supplier_name", "vendor", "vendor_name"),
    "risk_level": ("risk_level", "risk", "supplier_risk"),
    "contract_id": ("contract_id", "contract_identifier"),
    "category": ("category", "obligation_category", "type"),
    "title": ("title", "obligation", "obligation_title", "action"),
    "description": ("description", "details"),
    "due_date": ("due_date", "deadline", "obligation_date"),
    "recurrence_interval_months": ("recurrence_interval_months", "recurrence_months", "cadence_months"),
    "evidence_quote": ("evidence_quote", "source_quote", "evidence", "contract_clause"),
}


def parse_upload(filename: str, content: bytes) -> list[dict[str, str]]:
    if len(content) > 10 * 1024 * 1024:
        raise ValueError("Import files must be 10 MB or smaller")
    suffix = PurePosixPath(filename).suffix.casefold()
    if suffix == ".csv":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("CSV files must use UTF-8 encoding") from exc
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("The CSV file must include a header row")
        rows = [{str(key or "").strip(): str(value or "").strip() for key, value in row.items() if key is not None}
                for row in reader]
    elif suffix == ".xlsx":
        rows = _parse_xlsx(content)
    else:
        raise ValueError("Upload a CSV or XLSX file")
    if not rows:
        raise ValueError("The import file contains no data rows")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"Import files may contain at most {MAX_ROWS} data rows")
    return rows


def _parse_xlsx(content: bytes) -> list[dict[str, str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("The XLSX file is invalid") from exc
    with archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_UNPACKED_BYTES:
            raise ValueError("The XLSX file expands beyond the 25 MB safety limit")
        names = set(archive.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("The XLSX workbook is missing workbook metadata")
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
              "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
              "p": "http://schemas.openxmlformats.org/package/2006/relationships"}
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        sheet = workbook.find("m:sheets/m:sheet", ns)
        if sheet is None:
            raise ValueError("The XLSX workbook has no worksheets")
        rel_id = sheet.attrib.get("{" + ns["r"] + "}id")
        target = "worksheets/sheet1.xml"
        if "xl/_rels/workbook.xml.rels" in names:
            rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            relationship = next((item for item in rels.findall("p:Relationship", ns) if item.attrib.get("Id") == rel_id), None)
            if relationship is not None:
                target = relationship.attrib["Target"]
        sheet_path = str(PurePosixPath("xl") / target.lstrip("/")) if not target.startswith("/xl/") else target.lstrip("/")
        if sheet_path not in names:
            raise ValueError("The first XLSX worksheet could not be found")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in item.findall(".//m:t", ns)) for item in shared_root.findall("m:si", ns)]
        root = ElementTree.fromstring(archive.read(sheet_path))
        data_rows = []
        for row in root.findall("m:sheetData/m:row", ns):
            cells: dict[int, str] = {}
            for cell in row.findall("m:c", ns):
                match = re.match(r"([A-Z]+)", cell.attrib.get("r", ""))
                if not match:
                    continue
                col = 0
                for char in match.group(1):
                    col = col * 26 + ord(char) - 64
                col -= 1
                value_node = cell.find("m:v", ns)
                if cell.attrib.get("t") == "inlineStr":
                    value = "".join(node.text or "" for node in cell.findall(".//m:t", ns))
                elif value_node is None:
                    value = ""
                else:
                    value = value_node.text or ""
                    if cell.attrib.get("t") == "s":
                        try:
                            value = shared[int(value)]
                        except (ValueError, IndexError):
                            raise ValueError("The XLSX workbook contains an invalid shared string reference")
                cells[col] = value.strip()
            if cells:
                data_rows.append(cells)
        if len(data_rows) < 2:
            raise ValueError("The XLSX worksheet must contain a header and at least one data row")
        headers = [data_rows[0].get(index, "").strip() for index in range(max(data_rows[0]) + 1)]
        if not any(headers):
            raise ValueError("The XLSX worksheet must include a header row")
        return [{header: row.get(index, "") for index, header in enumerate(headers) if header}
                for row in data_rows[1:] if any(value for value in row.values())]


def normalize_mapping(headers: list[str], dataset: str, requested: dict[str, str] | None) -> dict[str, str]:
    if dataset not in FIELDS:
        raise ValueError("dataset must be contracts, suppliers, or obligations")
    if requested:
        if set(requested) - FIELDS[dataset]:
            raise ValueError("Field mapping contains fields not supported by the selected dataset")
        if any(source not in headers for source in requested.values()):
            raise ValueError("Every mapped source column must exist in the uploaded file")
        return requested
    by_normalized = {re.sub(r"[^a-z0-9]+", "_", header.casefold()).strip("_"): header for header in headers}
    mapping = {}
    for field in FIELDS[dataset]:
        for alias in ALIASES.get(field, (field,)):
            if alias in by_normalized:
                mapping[field] = by_normalized[alias]
                break
    return mapping


def validate_rows(dataset: str, raw_rows: list[dict[str, str]], mapping: dict[str, str], store,
                  tenant_id: str) -> tuple[list[dict], dict]:
    existing_keys = _existing_keys(dataset, store, tenant_id)
    seen: set[tuple] = set()
    prepared: list[dict] = []
    report_rows = []
    valid_count = duplicate_count = invalid_count = 0
    for number, raw in enumerate(raw_rows, 2):
        row = {field: raw.get(source, "").strip() for field, source in mapping.items()}
        errors = []
        normalized: dict = {}
        try:
            if dataset == "contracts":
                payload = {key: row[key] for key in ("contract", "start_date", "expiration_date", "renewal_notice_days", "auto_renew", "termination_notice", "owner_name", "owner_email", "annual_contract_value", "currency") if row.get(key, "") != ""}
                if not payload.get("contract") or not payload.get("expiration_date"):
                    raise ValueError("contract and expiration_date are required")
                if payload.get("start_date"):
                    payload["start_date"] = parse_date(payload["start_date"])
                payload["expiration_date"] = parse_date(payload["expiration_date"])
                if payload.get("renewal_notice_days"):
                    payload["renewal_notice_days"] = int(payload["renewal_notice_days"])
                if payload.get("auto_renew"):
                    payload["auto_renew"] = parse_bool(payload["auto_renew"])
                if payload.get("annual_contract_value"):
                    payload["annual_contract_value"] = Decimal(payload["annual_contract_value"].replace(",", ""))
                normalized = ContractData.model_validate(payload).model_dump(mode="json")
                normalized["supplier_id"] = row.get("supplier_id") or None
                normalized["supplier_name"] = row.get("supplier_name") or None
                key = (normalized["contract"].casefold(), normalized.get("expiration_date"))
            elif dataset == "suppliers":
                normalized = SupplierCreateRequest.model_validate({key: value for key, value in row.items() if value != ""}).model_dump()
                key = (" ".join(normalized["name"].casefold().split()),)
            else:
                payload = {key: value for key, value in row.items() if value != ""}
                for required in ("contract_id", "category", "title", "due_date", "owner_email", "evidence_quote"):
                    if not payload.get(required):
                        raise ValueError(f"{required} is required")
                contract_id = payload.pop("contract_id")
                payload["due_date"] = parse_date(payload["due_date"])
                if payload.get("recurrence_interval_months"):
                    payload["recurrence_interval_months"] = int(payload["recurrence_interval_months"])
                payload["evidence"] = {"field": "portfolio_import", "quote": payload.pop("evidence_quote")}
                normalized = ObligationCreateRequest.model_validate(payload).model_dump(mode="json")
                normalized["contract_id"] = contract_id
                contract = store.get_contract(contract_id, tenant_id)
                if not contract or contract["status"] != "active":
                    raise ValueError("contract_id must refer to an active contract in this organization")
                key = (normalized["contract_id"], normalized["category"], normalized["title"].casefold(), normalized["due_date"])
        except (ValueError, TypeError, InvalidOperation, ValidationError) as exc:
            errors.append(str(exc))
            key = None
        duplicate = bool(key is not None and (key in existing_keys or key in seen))
        if key is not None:
            seen.add(key)
        if errors:
            invalid_count += 1
            status = "invalid"
        elif duplicate:
            duplicate_count += 1
            status = "duplicate"
        else:
            valid_count += 1
            status = "ready"
            prepared.append({"row_number": number, "data": normalized})
        report_rows.append({"row_number": number, "status": status, "errors": errors,
                            "preview": normalized if normalized else row})
    return prepared, {"dataset": dataset, "row_count": len(raw_rows), "ready_count": valid_count,
                      "duplicate_count": duplicate_count, "invalid_count": invalid_count, "rows": report_rows}


def _existing_keys(dataset: str, store, tenant_id: str) -> set[tuple]:
    keys = set()
    if dataset == "contracts":
        for row in store.list_contracts(tenant_id=tenant_id):
            terms = json.loads(row["confirmed_json"] or row["extracted_json"] or "{}")
            if terms.get("contract") and terms.get("expiration_date"):
                keys.add((terms["contract"].casefold(), terms["expiration_date"]))
    elif dataset == "suppliers":
        keys.update((row["normalized_name"],) for row in store.list_suppliers(tenant_id))
    else:
        for row in store.list_contract_obligations(tenant_id):
            keys.add((row["contract_id"], row["category"], row["title"].casefold(), row["due_date"]))
    return keys


def parse_date(value: str) -> date:
    value = value.strip()
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        try:
            serial = Decimal(value)
            if 1 <= serial <= 2_958_465:
                return date(1899, 12, 30) + timedelta(days=int(serial))
        except InvalidOperation:
            pass
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date value: {value}")


def parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "yes", "y", "1"}:
        return True
    if normalized in {"false", "no", "n", "0"}:
        return False
    raise ValueError("auto_renew must be true/false, yes/no, or 1/0")


def duplicate_key(dataset: str, data: dict) -> tuple:
    if dataset == "contracts":
        return (str(data.get("contract") or "").casefold(), data.get("expiration_date"))
    if dataset == "suppliers":
        return (" ".join(str(data["name"]).casefold().split()),)
    return (data["contract_id"], data["category"], data["title"].casefold(), data["due_date"])


def create_imported_record(store, dataset: str, data: dict, filename: str, row_number: int,
                          tenant_id: str, actor: str) -> tuple[str, str]:
    if dataset == "suppliers":
        supplier = store.create_supplier(data["name"], actor, tenant_id, data.get("risk_level", "medium"))
        return supplier["id"], "supplier.created"
    if dataset == "contracts":
        supplier_id, supplier_name = data.get("supplier_id"), data.get("supplier_name")
        terms = ContractData.model_validate({key: value for key, value in data.items() if key not in {"supplier_id", "supplier_name"}})
        if supplier_name:
            match = next((item for item in store.list_suppliers(tenant_id)
                          if item["normalized_name"] == " ".join(supplier_name.casefold().split())), None)
            if not match:
                raise ValueError(f"Supplier '{supplier_name}' was not found; import the supplier first")
            supplier_id = match["id"]
        if supplier_id and not any(item["id"] == supplier_id for item in store.list_suppliers(tenant_id)):
            raise ValueError("supplier_id does not belong to this organization")
        contract_id = uuid4().hex
        store.save_contract(contract_id, f"import-{filename}-row-{row_number}", "portfolio_import", terms,
                            f"Portfolio import from {filename}, row {row_number}.", actor, tenant_id)
        store.confirm_contract(contract_id, terms, actor, tenant_id)
        if supplier_id:
            store.assign_contract_supplier(contract_id, supplier_id, actor, tenant_id)
        return contract_id, "contract.created_and_confirmed"
    obligation = store.create_contract_obligation(data["contract_id"], data, data.get("owner_name"),
                                                   data["owner_email"], actor, tenant_id)
    if not obligation:
        raise ValueError("contract_id was not found in this organization")
    return obligation["id"], "obligation.created"
