from __future__ import annotations

import json
import os
import re
import sqlite3
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from uuid import uuid4
import calendar

from .schemas import ContractData


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value else None


class _PostgresConnection:
    """Small DB-API adapter for the SQLite-shaped queries used by Store."""

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self.connection.__exit__(exc_type, exc, traceback)

    def execute(self, sql: str, params=()):
        return self.connection.execute(sql.replace("?", "%s"), params)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("DATABASE_URL") or os.getenv("DATABASE_PATH", "renewal_radar.db")
        self.is_postgres = self.path.startswith(("postgres://", "postgresql://"))
        if not self.is_postgres and self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        if self.is_postgres:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL support requires the 'database' extra: pip install '.[database]'"
                ) from exc
            return _PostgresConnection(psycopg.connect(self.path, row_factory=dict_row))
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as db:
            schema = """
                CREATE TABLE IF NOT EXISTS contracts (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    extracted_json TEXT NOT NULL,
                    confirmed_json TEXT,
                    raw_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    supplier_id TEXT
                );
                CREATE TABLE IF NOT EXISTS suppliers (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, normalized_name)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    title TEXT NOT NULL,
                    owner_name TEXT,
                    owner_email TEXT,
                    due_date TEXT NOT NULL,
                    expiration_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    reminder_sent_at TEXT,
                    escalated_at TEXT,
                    created_at TEXT NOT NULL,
                    notice_day_type TEXT NOT NULL DEFAULT 'calendar',
                    notice_timezone TEXT NOT NULL DEFAULT 'UTC',
                    notice_holidays_json TEXT NOT NULL DEFAULT '[]',
                    workflow_state TEXT NOT NULL DEFAULT 'review',
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    task_kind TEXT NOT NULL DEFAULT 'renewal'
                );
                CREATE TABLE IF NOT EXISTS contract_obligations (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    evidence_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS renewal_outcomes (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    tenant_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    outcome TEXT NOT NULL,
                    actual_annual_value TEXT,
                    annual_savings TEXT,
                    currency TEXT,
                    successor_contract_id TEXT REFERENCES contracts(id),
                    actual_expiration_date TEXT,
                    notes TEXT,
                    recorded_by TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default'
                );
                CREATE TABLE IF NOT EXISTS calendar_events (
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    provider TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, provider, tenant_id)
                );
                CREATE TABLE IF NOT EXISTS integration_cursors (
                    tenant_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, provider, source_key)
                );
                CREATE TABLE IF NOT EXISTS external_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    source_url TEXT,
                    ingested_at TEXT NOT NULL,
                    UNIQUE (tenant_id, provider, source_key, external_id, revision)
                );
                CREATE TABLE IF NOT EXISTS task_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    tenant_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notices (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    tenant_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    delivery_method TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    approved_by TEXT,
                    approved_at TEXT,
                    dispatched_by TEXT,
                    dispatched_at TEXT,
                    delivered_at TEXT,
                    delivery_reference TEXT,
                    delivery_note TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    available_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    last_error TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE (tenant_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS legal_holds (
                    id TEXT PRIMARY KEY,
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    tenant_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    placed_by TEXT NOT NULL,
                    placed_at TEXT NOT NULL,
                    released_by TEXT,
                    released_at TEXT
                );
                CREATE TABLE IF NOT EXISTS access_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    accessed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    channel TEXT NOT NULL,
                    notification_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    error_message TEXT,
                    attempted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_feedback (
                    id TEXT PRIMARY KEY,
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    tenant_id TEXT NOT NULL,
                    extraction_provider TEXT NOT NULL,
                    proposed_json TEXT NOT NULL,
                    reviewed_json TEXT NOT NULL,
                    corrections_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'captured',
                    evaluation_case_json TEXT,
                    approval_reference TEXT,
                    clause_categories_json TEXT,
                    captured_by TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    approved_by TEXT,
                    approved_at TEXT,
                    UNIQUE (contract_id, tenant_id)
                );
                CREATE TABLE IF NOT EXISTS contract_versions (
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    prior_contract_id TEXT NOT NULL REFERENCES contracts(id),
                    tenant_id TEXT NOT NULL,
                    linked_by TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    PRIMARY KEY (contract_id, tenant_id),
                    UNIQUE (prior_contract_id, tenant_id),
                    CHECK (contract_id <> prior_contract_id)
                );
                CREATE TABLE IF NOT EXISTS tenant_policies (
                    tenant_id TEXT PRIMARY KEY,
                    policy_json TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tenant_integrations (
                    tenant_id TEXT NOT NULL,
                    integration_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    credentials_ciphertext TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'configured',
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    last_error TEXT,
                    PRIMARY KEY (tenant_id, integration_name)
                );
                CREATE TABLE IF NOT EXISTS integration_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    integration_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_notification_state (
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    tenant_id TEXT NOT NULL,
                    action_key TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    PRIMARY KEY (task_id, action_key)
                );
                CREATE INDEX IF NOT EXISTS idx_contract_status ON contracts(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, due_date);
                CREATE INDEX IF NOT EXISTS idx_audit_contract ON audit_events(contract_id, id);
                """
            if self.is_postgres:
                schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
            db.executescript(schema)
            # Upgrade databases created by earlier versions without dropping data.
            for table in ("contracts", "tasks", "audit_events"):
                if self.is_postgres:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'")
                    if table == "tasks":
                        db.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notice_day_type TEXT NOT NULL DEFAULT 'calendar'")
                        db.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notice_timezone TEXT NOT NULL DEFAULT 'UTC'")
                        db.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notice_holidays_json TEXT NOT NULL DEFAULT '[]'")
                        db.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS workflow_state TEXT NOT NULL DEFAULT 'review'")
                        db.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_kind TEXT NOT NULL DEFAULT 'renewal'")
                else:
                    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                    if "tenant_id" not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
                    if table == "contracts" and "supplier_id" not in columns:
                        db.execute("ALTER TABLE contracts ADD COLUMN supplier_id TEXT")
                    if table == "tasks" and "notice_day_type" not in columns:
                        db.execute("ALTER TABLE tasks ADD COLUMN notice_day_type TEXT NOT NULL DEFAULT 'calendar'")
                    if table == "tasks" and "notice_timezone" not in columns:
                        db.execute("ALTER TABLE tasks ADD COLUMN notice_timezone TEXT NOT NULL DEFAULT 'UTC'")
                    if table == "tasks" and "notice_holidays_json" not in columns:
                        db.execute("ALTER TABLE tasks ADD COLUMN notice_holidays_json TEXT NOT NULL DEFAULT '[]'")
                    if table == "tasks" and "workflow_state" not in columns:
                        db.execute("ALTER TABLE tasks ADD COLUMN workflow_state TEXT NOT NULL DEFAULT 'review'")
                    if table == "tasks" and "task_kind" not in columns:
                        db.execute("ALTER TABLE tasks ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'renewal'")
                if self.is_postgres and table == "contracts":
                    db.execute("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS supplier_id TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS idx_contract_tenant ON contracts(tenant_id, status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_tenant ON tasks(tenant_id, status, due_date)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_kind_due ON tasks(tenant_id, task_kind, status, due_date)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_obligations_contract ON contract_obligations(tenant_id, contract_id, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(status, available_at, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_access_tenant ON access_events(tenant_id, accessed_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_notification_tenant ON notification_deliveries(tenant_id, attempted_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON review_feedback(tenant_id, status, captured_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_contract_versions_prior ON contract_versions(tenant_id, prior_contract_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_notification_tenant ON task_notification_state(tenant_id, sent_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_supplier_tenant ON suppliers(tenant_id, normalized_name)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_outcome_tenant_date ON renewal_outcomes(tenant_id, recorded_at)")

    def save_contract(self, contract_id: str, filename: str, provider: str, data: ContractData, raw_text: str, actor: str, tenant_id: str = "default") -> None:
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO contracts (id, filename, status, provider, extracted_json, confirmed_json, raw_text, created_at, confirmed_at, tenant_id) VALUES (?, ?, 'pending_review', ?, ?, NULL, ?, ?, NULL, ?)",
                (contract_id, filename, provider, data.model_dump_json(), raw_text, now, tenant_id),
            )
            self.add_audit(db, contract_id, "contract.ingested", actor, {"filename": filename, "provider": provider}, tenant_id)

    def external_document_exists(self, tenant_id: str, provider: str, source_key: str, external_id: str, revision: str) -> bool:
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM external_documents WHERE tenant_id=? AND provider=? AND source_key=? AND external_id=? AND revision=?",
                (tenant_id, provider, source_key, external_id, revision),
            ).fetchone() is not None

    def save_external_contract(
        self, contract_id: str, filename: str, extractor_provider: str, data: ContractData, raw_text: str,
        tenant_id: str, provider: str, source_key: str, external_id: str, revision: str, source_url: str | None,
    ) -> tuple[str, bool]:
        now = utc_now().isoformat()
        with self.connect() as db:
            existing = db.execute(
                "SELECT contract_id FROM external_documents WHERE tenant_id=? AND provider=? AND source_key=? AND external_id=? AND revision=?",
                (tenant_id, provider, source_key, external_id, revision),
            ).fetchone()
            if existing:
                return existing["contract_id"], False
            db.execute(
                "INSERT INTO contracts (id, filename, status, provider, extracted_json, confirmed_json, raw_text, created_at, confirmed_at, tenant_id) VALUES (?, ?, 'pending_review', ?, ?, NULL, ?, ?, NULL, ?)",
                (contract_id, filename, f"{extractor_provider}+{provider}", data.model_dump_json(), raw_text, now, tenant_id),
            )
            db.execute(
                "INSERT INTO external_documents (tenant_id, provider, source_key, external_id, revision, contract_id, source_url, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, provider, source_key, external_id, revision, contract_id, source_url, now),
            )
            self.add_audit(db, contract_id, "document.source_ingested", f"integration:{provider}", {
                "source_key": source_key, "external_id": external_id, "revision": revision, "source_url": source_url,
            }, tenant_id)
            return contract_id, True

    def get_integration_cursor(self, tenant_id: str, provider: str, source_key: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT cursor FROM integration_cursors WHERE tenant_id=? AND provider=? AND source_key=?",
                (tenant_id, provider, source_key),
            ).fetchone()
            return row["cursor"] if row else None

    def save_integration_cursor(self, tenant_id: str, provider: str, source_key: str, cursor: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO integration_cursors (tenant_id, provider, source_key, cursor, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, provider, source_key) DO UPDATE SET cursor=excluded.cursor, updated_at=excluded.updated_at",
                (tenant_id, provider, source_key, cursor, utc_now().isoformat()),
            )

    def get_contract(self, contract_id: str, tenant_id: str | None = "default") -> sqlite3.Row | None:
        with self.connect() as db:
            if tenant_id is None:
                return db.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
            return db.execute("SELECT * FROM contracts WHERE id = ? AND tenant_id = ?", (contract_id, tenant_id)).fetchone()

    def list_contracts(self, status: str | None = None, tenant_id: str = "default") -> list[sqlite3.Row]:
        with self.connect() as db:
            if status:
                return db.execute("SELECT * FROM contracts WHERE tenant_id=? AND status = ? ORDER BY created_at DESC", (tenant_id, status)).fetchall()
            return db.execute("SELECT * FROM contracts WHERE tenant_id=? ORDER BY created_at DESC", (tenant_id,)).fetchall()

    @staticmethod
    def _supplier_key(name: str) -> str:
        return " ".join(name.casefold().split())

    def create_supplier(self, name: str, actor: str, tenant_id: str) -> dict[str, Any]:
        normalized = self._supplier_key(name)
        now = utc_now().isoformat()
        supplier_id = str(uuid4())
        with self.connect() as db:
            try:
                db.execute(
                    "INSERT INTO suppliers (id,tenant_id,name,normalized_name,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (supplier_id, tenant_id, name.strip(), normalized, actor, now, now),
                )
            except Exception as exc:
                if "unique" in str(exc).casefold() or "duplicate" in str(exc).casefold():
                    raise ValueError("A supplier with this name already exists in this organization") from exc
                raise
            return dict(db.execute("SELECT * FROM suppliers WHERE id=? AND tenant_id=?", (supplier_id, tenant_id)).fetchone())

    def assign_contract_supplier(self, contract_id: str, supplier_id: str, actor: str, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            contract = db.execute("SELECT id,supplier_id FROM contracts WHERE id=? AND tenant_id=?", (contract_id, tenant_id)).fetchone()
            if not contract:
                return None
            supplier = db.execute("SELECT id FROM suppliers WHERE id=? AND tenant_id=?", (supplier_id, tenant_id)).fetchone()
            if not supplier:
                raise ValueError("Supplier not found in this organization")
            db.execute("UPDATE contracts SET supplier_id=? WHERE id=? AND tenant_id=?", (supplier_id, contract_id, tenant_id))
            db.execute("UPDATE suppliers SET updated_at=? WHERE id=? AND tenant_id=?", (utc_now().isoformat(), supplier_id, tenant_id))
            self.add_audit(db, contract_id, "contract.supplier_assigned", actor, {"supplier_id": supplier_id}, tenant_id)
            return dict(db.execute("SELECT * FROM contracts WHERE id=? AND tenant_id=?", (contract_id, tenant_id)).fetchone())

    def list_suppliers(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            suppliers = db.execute("SELECT * FROM suppliers WHERE tenant_id=? ORDER BY normalized_name", (tenant_id,)).fetchall()
            rows = db.execute(
                "SELECT c.id,c.supplier_id,c.status,c.confirmed_json,c.extracted_json,t.id AS task_id,t.status AS task_status "
                "FROM contracts c LEFT JOIN tasks t ON t.contract_id=c.id AND t.tenant_id=c.tenant_id AND t.task_kind='renewal' "
                "WHERE c.tenant_id=? AND c.supplier_id IS NOT NULL",
                (tenant_id,),
            ).fetchall()
        by_supplier: dict[str, dict[str, Any]] = {}
        for supplier in suppliers:
            by_supplier[supplier["id"]] = {
                **dict(supplier), "contract_count": 0, "active_contract_count": 0,
                "open_renewal_count": 0, "annual_spend_at_risk": {},
            }
        seen_contracts: set[tuple[str, str]] = set()
        for row in rows:
            item = by_supplier.get(row["supplier_id"])
            if not item:
                continue
            key = (row["supplier_id"], row["id"])
            if key in seen_contracts:
                continue
            seen_contracts.add(key)
            item["contract_count"] += 1
            if row["status"] == "active":
                item["active_contract_count"] += 1
            if row["task_id"] and row["task_status"] == "open":
                item["open_renewal_count"] += 1
                terms = json.loads(row["confirmed_json"] or row["extracted_json"])
                amount, currency = terms.get("annual_contract_value"), terms.get("currency")
                if amount is not None and currency:
                    item["annual_spend_at_risk"][currency] = str(
                        Decimal(item["annual_spend_at_risk"].get(currency, "0")) + Decimal(str(amount))
                    )
        return list(by_supplier.values())

    def create_contract_obligation(
        self, contract_id: str, terms: dict[str, Any], owner_name: str | None,
        owner_email: str, actor: str, tenant_id: str,
    ) -> dict[str, Any] | None:
        contract = self.get_contract(contract_id, tenant_id)
        if not contract:
            return None
        if contract["status"] != "active":
            raise ValueError("Obligations can be scheduled only for active contracts")
        due_date = terms.get("due_date")
        if due_date is None:
            anchor_field = terms.get("due_offset_from")
            anchor = None
            confirmed = json.loads(contract["confirmed_json"] or "{}")
            if anchor_field:
                anchor = confirmed.get(anchor_field)
            if not anchor or terms.get("due_offset_days") is None:
                raise ValueError(f"Cannot resolve obligation due date without the contract {anchor_field or 'date'}")
            anchor_date = date.fromisoformat(anchor) if isinstance(anchor, str) else anchor
            delta = timedelta(days=int(terms["due_offset_days"]))
            due_date = anchor_date - delta if terms.get("due_offset_direction") == "before" else anchor_date + delta
        elif isinstance(due_date, str):
            due_date = date.fromisoformat(due_date)
        confirmed = json.loads(contract["confirmed_json"] or "{}")
        start_date = confirmed.get("start_date")
        if start_date and due_date < date.fromisoformat(start_date):
            raise ValueError("Obligation due date cannot precede the contract start date")
        obligation_id, task_id = str(uuid4()), str(uuid4())
        now = utc_now().isoformat()
        evidence = terms["evidence"]
        if hasattr(evidence, "model_dump"):
            evidence = evidence.model_dump(mode="json")
        if not isinstance(evidence, dict) or not evidence.get("quote"):
            raise ValueError("An evidence quote is required to schedule an obligation")
        with self.connect() as db:
            current = db.execute("SELECT status FROM contracts WHERE id=? AND tenant_id=?", (contract_id, tenant_id)).fetchone()
            if not current or current["status"] != "active":
                raise ValueError("Obligations can be scheduled only for active contracts")
            db.execute(
                "INSERT INTO tasks (id,contract_id,title,owner_name,owner_email,due_date,expiration_date,status,created_at,tenant_id,task_kind) "
                "VALUES (?,?,?,?,?,?,?,'open',?,?,'obligation')",
                (task_id, contract_id, terms["title"], owner_name, owner_email, due_date.isoformat(), due_date.isoformat(), now, tenant_id),
            )
            db.execute(
                "INSERT INTO contract_obligations (id,tenant_id,contract_id,task_id,category,title,description,evidence_json,created_by,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (obligation_id, tenant_id, contract_id, task_id, terms["category"], terms["title"],
                 terms.get("description"), json.dumps(evidence, default=str), actor, now),
            )
            self.add_audit(db, contract_id, "obligation.scheduled", actor, {
                "obligation_id": obligation_id, "task_id": task_id, "category": terms["category"],
                "title": terms["title"], "due_date": due_date.isoformat(), "owner_email": owner_email,
            }, tenant_id)
            return self._obligation_record(db.execute(
                "SELECT o.*,t.status AS task_status,t.owner_name,t.owner_email,t.due_date,t.reminder_sent_at,t.escalated_at "
                "FROM contract_obligations o JOIN tasks t ON t.id=o.task_id WHERE o.id=? AND o.tenant_id=?",
                (obligation_id, tenant_id),
            ).fetchone())

    @staticmethod
    def _obligation_record(row) -> dict[str, Any]:
        result = dict(row)
        result["evidence"] = json.loads(result.pop("evidence_json"))
        result["status"] = result.pop("task_status")
        return result

    def list_contract_obligations(
        self, tenant_id: str, status: str | None = None, owner_email: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses, params = ["o.tenant_id=?"], [tenant_id]
        if status:
            clauses.append("t.status=?")
            params.append(status)
        if owner_email is not None:
            clauses.append("lower(t.owner_email)=lower(?)")
            params.append(owner_email)
        with self.connect() as db:
            rows = db.execute(
                "SELECT o.*,t.status AS task_status,t.owner_name,t.owner_email,t.due_date,t.reminder_sent_at,t.escalated_at "
                "FROM contract_obligations o JOIN tasks t ON t.id=o.task_id WHERE " + " AND ".join(clauses) +
                " ORDER BY t.due_date,o.created_at",
                tuple(params),
            ).fetchall()
        return [self._obligation_record(row) for row in rows]

    def renewal_forecast(
        self, tenant_id: str, start_date: date, end_date: date,
        renewal_increase_pct: Decimal = Decimal(0), renegotiation_savings_pct: Decimal = Decimal(10),
    ) -> dict[str, Any]:
        if end_date < start_date or (end_date - start_date).days > 366 * 5:
            raise ValueError("Forecast period must be ordered and no longer than five years")
        increase = Decimal(renewal_increase_pct) / Decimal(100)
        savings = Decimal(renegotiation_savings_pct) / Decimal(100)
        with self.connect() as db:
            contracts = db.execute(
                "SELECT c.id,c.filename,c.status,c.confirmed_json,c.extracted_json,s.name AS supplier_name,"
                "t.id AS task_id,t.due_date AS notice_deadline "
                "FROM contracts c LEFT JOIN suppliers s ON s.id=c.supplier_id AND s.tenant_id=c.tenant_id "
                "LEFT JOIN tasks t ON t.contract_id=c.id AND t.tenant_id=c.tenant_id AND t.status='open' AND t.task_kind='renewal' "
                "WHERE c.tenant_id=? AND c.status='active' ORDER BY c.id",
                (tenant_id,),
            ).fetchall()
        scenarios = ("renew", "renegotiate", "terminate")
        totals: dict[str, dict[str, Decimal]] = {scenario: {} for scenario in scenarios}
        monthly: dict[str, dict[str, dict[str, Decimal]]] = {scenario: {} for scenario in scenarios}
        events: dict[str, dict[str, dict[str, int]]] = {"notice_deadlines": {}, "expirations": {}}
        items: list[dict[str, Any]] = []
        missing_values: list[str] = []
        considered = 0
        for row in contracts:
            terms = json.loads(row["confirmed_json"] or row["extracted_json"])
            expiration = date.fromisoformat(terms["expiration_date"]) if terms.get("expiration_date") else None
            agreement_start = date.fromisoformat(terms["start_date"]) if terms.get("start_date") else start_date
            if agreement_start > end_date or (expiration and expiration < start_date):
                continue
            considered += 1
            if row["notice_deadline"]:
                deadline = date.fromisoformat(row["notice_deadline"])
                if start_date <= deadline <= end_date:
                    month = deadline.strftime("%Y-%m")
                    events["notice_deadlines"].setdefault(month, {})["count"] = events["notice_deadlines"].setdefault(month, {}).get("count", 0) + 1
            if expiration and start_date <= expiration <= end_date:
                month = expiration.strftime("%Y-%m")
                events["expirations"].setdefault(month, {})["count"] = events["expirations"].setdefault(month, {}).get("count", 0) + 1
            value = terms.get("annual_contract_value")
            currency = terms.get("currency")
            if value is None or not currency:
                missing_values.append(row["id"])
                items.append({"contract_id": row["id"], "contract": terms.get("contract") or row["filename"],
                              "supplier": row["supplier_name"], "expiration_date": expiration.isoformat() if expiration else None,
                              "notice_deadline": row["notice_deadline"], "currency": currency, "annual_contract_value": None,
                              "projected_costs": None})
                continue
            annual = Decimal(str(value))
            base_start = max(start_date, agreement_start)
            current_end = min(end_date, expiration) if expiration else end_date
            base_monthly = _monthly_prorated_costs(annual, base_start, current_end)
            scenario_monthly: dict[str, dict[str, Decimal]] = {scenario: dict(base_monthly) for scenario in scenarios}
            if expiration and expiration < end_date:
                tail_start = max(start_date, expiration + timedelta(days=1))
                tails = {
                    "renew": _monthly_prorated_costs(annual * (Decimal(1) + increase), tail_start, end_date),
                    "renegotiate": _monthly_prorated_costs(annual * (Decimal(1) - savings), tail_start, end_date),
                    "terminate": {},
                }
                for scenario in scenarios:
                    for month, amount in tails[scenario].items():
                        scenario_monthly[scenario][month] = scenario_monthly[scenario].get(month, Decimal(0)) + amount
            scenario_totals = {}
            for scenario in scenarios:
                total = sum(scenario_monthly[scenario].values(), Decimal(0))
                totals[scenario][currency] = totals[scenario].get(currency, Decimal(0)) + total
                for month, amount in scenario_monthly[scenario].items():
                    monthly[scenario].setdefault(month, {})[currency] = monthly[scenario].setdefault(month, {}).get(currency, Decimal(0)) + amount
                scenario_totals[scenario] = _money_string(total)
            items.append({
                "contract_id": row["id"], "contract": terms.get("contract") or row["filename"],
                "supplier": row["supplier_name"], "expiration_date": expiration.isoformat() if expiration else None,
                "notice_deadline": row["notice_deadline"], "currency": currency,
                "annual_contract_value": str(annual), "projected_costs": scenario_totals,
            })
        return {
            "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            "assumptions": {"renewal_increase_pct": str(renewal_increase_pct), "renegotiation_savings_pct": str(renegotiation_savings_pct),
                            "proration": "daily by calendar-year length; termination ends spend after expiration"},
            "contracts_considered": considered, "contracts_missing_value": missing_values,
            "scenarios": {scenario: {
                "projected_cost_by_currency": {currency: _money_string(amount) for currency, amount in totals[scenario].items()},
                "monthly_projected_cost_by_currency": {month: {currency: _money_string(amount) for currency, amount in values.items()}
                                                       for month, values in sorted(monthly[scenario].items())},
                "notice_deadlines_by_month": {month: value["count"] for month, value in sorted(events["notice_deadlines"].items())},
                "expirations_by_month": {month: value["count"] for month, value in sorted(events["expirations"].items())},
            } for scenario in scenarios},
            "contracts": items,
        }

    def record_renewal_outcome(self, task_id: str, request: dict[str, Any], actor: str, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            if not task:
                return None
            if db.execute("SELECT 1 FROM renewal_outcomes WHERE task_id=?", (task_id,)).fetchone():
                raise ValueError("A renewal outcome has already been recorded for this task")
            if task["status"] != "open":
                raise ValueError("Only open tasks can have a renewal outcome recorded")
            successor_id = request.get("successor_contract_id")
            if successor_id and not db.execute(
                "SELECT 1 FROM contracts WHERE id=? AND tenant_id=?", (successor_id, tenant_id),
            ).fetchone():
                raise ValueError("Successor contract must belong to this organization")
            outcome = request["outcome"]
            workflow_state = "renewed" if outcome in {"renewed", "renegotiated"} else "cancelled" if outcome == "no_action" else "terminated"
            now = utc_now().isoformat()
            db.execute(
                "INSERT INTO renewal_outcomes (task_id,tenant_id,contract_id,outcome,actual_annual_value,annual_savings,currency,successor_contract_id,actual_expiration_date,notes,recorded_by,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, tenant_id, task["contract_id"], outcome,
                 str(request["actual_annual_value"]) if request.get("actual_annual_value") is not None else None,
                 str(request["annual_savings"]) if request.get("annual_savings") is not None else None,
                 request.get("currency"), successor_id, iso(request.get("actual_expiration_date")),
                 request.get("notes"), actor, now),
            )
            db.execute("UPDATE tasks SET status='resolved',workflow_state=? WHERE id=?", (workflow_state, task_id))
            details = {key: value for key, value in request.items() if key != "notes"}
            details["task_id"] = task_id
            self.add_audit(db, task["contract_id"], "renewal.outcome_recorded", actor, details, tenant_id)
            return dict(db.execute("SELECT * FROM renewal_outcomes WHERE task_id=?", (task_id,)).fetchone())

    def renewal_outcome_summary(self, tenant_id: str) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT outcome,currency,actual_annual_value,annual_savings,recorded_at FROM renewal_outcomes WHERE tenant_id=? ORDER BY recorded_at DESC",
                (tenant_id,),
            ).fetchall()
        counts: dict[str, int] = {}
        values: dict[str, Decimal] = {}
        savings: dict[str, Decimal] = {}
        for row in rows:
            counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
            currency = row["currency"]
            if currency:
                values[currency] = values.get(currency, Decimal(0)) + Decimal(row["actual_annual_value"] or "0")
                savings[currency] = savings.get(currency, Decimal(0)) + Decimal(row["annual_savings"] or "0")
        return {
            "total": len(rows), "by_outcome": counts,
            "actual_annual_value_by_currency": {key: str(value) for key, value in values.items()},
            "annual_savings_by_currency": {key: str(value) for key, value in savings.items()},
        }

    def confirm_contract(self, contract_id: str, data: ContractData, actor: str, tenant_id: str = "default") -> str:
        from uuid import uuid4

        with self.connect() as db:
            row = db.execute("SELECT * FROM contracts WHERE id = ? AND tenant_id = ?", (contract_id, tenant_id)).fetchone()
            if not row:
                raise KeyError(contract_id)
            if row["status"] != "pending_review":
                raise ValueError("Only contracts pending review can be confirmed")
            if not data.expiration_date:
                raise ValueError("expiration_date is required before a contract can be activated")
            if data.auto_renew and data.renewal_notice_days is None:
                raise ValueError("renewal_notice_days is required for an auto-renewing contract")
            due = calculate_notice_deadline(data.expiration_date, data.renewal_notice_days or 0, data.notice_day_type, set(data.notice_holidays))
            try:
                ZoneInfo(data.notice_timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"Unknown IANA timezone: {data.notice_timezone}") from exc
            superseded = None
            if data.supersedes_contract_id:
                superseded = db.execute(
                    "SELECT * FROM contracts WHERE id=? AND tenant_id=? AND status='active'",
                    (data.supersedes_contract_id, tenant_id),
                ).fetchone()
                if not superseded:
                    raise ValueError("supersedes_contract_id must reference an active contract in this organization")
            task_id = str(uuid4())
            now = utc_now().isoformat()
            if superseded:
                db.execute(
                    "INSERT INTO contract_versions (contract_id, prior_contract_id, tenant_id, linked_by, linked_at) VALUES (?, ?, ?, ?, ?)",
                    (contract_id, superseded["id"], tenant_id, actor, now),
                )
                db.execute("UPDATE contracts SET status='superseded' WHERE id=?", (superseded["id"],))
                db.execute("UPDATE tasks SET status='resolved', workflow_state='cancelled' WHERE contract_id=? AND status='open'", (superseded["id"],))
                self.add_audit(db, superseded["id"], "contract.superseded", actor, {"replacement_contract_id": contract_id}, tenant_id)
                self.add_audit(db, contract_id, "contract.version_linked", actor, {"prior_contract_id": superseded["id"]}, tenant_id)
            db.execute(
                "UPDATE contracts SET status='active', confirmed_json=?, confirmed_at=? WHERE id=?",
                (data.model_dump_json(), now, contract_id),
            )
            db.execute(
                "INSERT INTO tasks (id, contract_id, title, owner_name, owner_email, due_date, expiration_date, status, created_at, tenant_id, notice_day_type, notice_timezone, notice_holidays_json) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                (task_id, contract_id, f"Review renewal: {data.contract or row['filename']}", data.owner_name, data.owner_email, due.isoformat(), data.expiration_date.isoformat(), now, tenant_id, data.notice_day_type, data.notice_timezone, json.dumps([d.isoformat() for d in data.notice_holidays])),
            )
            scored_fields = ("start_date", "expiration_date", "renewal_notice_days", "notice_day_type", "auto_renew", "termination_notice")
            proposed_data = ContractData.model_validate_json(row["extracted_json"]).model_dump(mode="json")
            reviewed_data = data.model_dump(mode="json")
            proposed = {field: proposed_data.get(field) for field in scored_fields}
            reviewed = {field: reviewed_data.get(field) for field in scored_fields}
            corrections = {
                field: {"proposed": proposed.get(field), "reviewed": reviewed.get(field)}
                for field in scored_fields if proposed.get(field) != reviewed.get(field)
            }
            db.execute(
                "INSERT INTO review_feedback (id, contract_id, tenant_id, extraction_provider, proposed_json, reviewed_json, corrections_json, captured_by, captured_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), contract_id, tenant_id, row["provider"], json.dumps(proposed), json.dumps(reviewed), json.dumps(corrections), actor, now),
            )
            self.add_audit(db, contract_id, "contract.confirmed", actor, {"contract": data.model_dump(mode="json"), "task_id": task_id}, tenant_id)
            self.add_audit(db, contract_id, "task.created", "system", {"task_id": task_id, "due_date": due.isoformat(), "notice_day_type": data.notice_day_type, "notice_timezone": data.notice_timezone}, tenant_id)
            return task_id

    def amendment_candidates(self, contract_id: str, tenant_id: str = "default", limit: int = 5) -> list[dict[str, Any]]:
        """Rank active tenant contracts by deterministic name similarity to a pending upload."""
        with self.connect() as db:
            proposal = db.execute(
                "SELECT id,filename,extracted_json FROM contracts WHERE id=? AND tenant_id=? AND status='pending_review'",
                (contract_id, tenant_id),
            ).fetchone()
            if not proposal:
                return []
            active = db.execute(
                "SELECT id,filename,confirmed_json,extracted_json FROM contracts "
                "WHERE tenant_id=? AND status='active' AND id<>? ORDER BY created_at DESC",
                (tenant_id, contract_id),
            ).fetchall()
        proposed_terms = json.loads(proposal["extracted_json"])
        proposed_name = proposed_terms.get("contract") or Path(proposal["filename"]).stem
        proposed_key = _amendment_match_key(proposed_name)
        candidates = []
        for row in active:
            terms = json.loads(row["confirmed_json"] or row["extracted_json"])
            active_name = terms.get("contract") or Path(row["filename"]).stem
            active_key = _amendment_match_key(active_name)
            if not proposed_key or not active_key:
                continue
            proposed_tokens, active_tokens = set(proposed_key.split()), set(active_key.split())
            token_similarity = len(proposed_tokens & active_tokens) / max(1, len(proposed_tokens | active_tokens))
            name_similarity = SequenceMatcher(None, proposed_key, active_key).ratio()
            score = max(name_similarity, token_similarity)
            if score < 0.2:
                continue
            reasons = []
            if proposed_key == active_key:
                reasons.append("normalized_contract_name_match")
            shared = sorted(proposed_tokens & active_tokens)
            if shared:
                reasons.append("shared_name_terms: " + ", ".join(shared))
            candidates.append({
                "contract_id": row["id"], "contract": active_name, "filename": row["filename"],
                "expiration_date": terms.get("expiration_date"), "match_score": round(score, 3),
                "match_reasons": reasons,
            })
        return sorted(candidates, key=lambda item: (-item["match_score"], item["contract"].casefold()))[:limit]

    def preview_contract_confirmation(self, contract_id: str, data: ContractData, tenant_id: str) -> dict[str, Any]:
        """Validate a proposed confirmation and describe its task/version effects without writing."""
        with self.connect() as db:
            proposal = db.execute(
                "SELECT * FROM contracts WHERE id=? AND tenant_id=?", (contract_id, tenant_id),
            ).fetchone()
            if not proposal:
                raise KeyError(contract_id)
            if proposal["status"] != "pending_review":
                raise ValueError("Only contracts pending review can be previewed")
            if not data.expiration_date:
                raise ValueError("expiration_date is required before a contract can be activated")
            if data.auto_renew and data.renewal_notice_days is None:
                raise ValueError("renewal_notice_days is required for an auto-renewing contract")
            due = calculate_notice_deadline(
                data.expiration_date, data.renewal_notice_days or 0,
                data.notice_day_type, set(data.notice_holidays),
            )
            try:
                ZoneInfo(data.notice_timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"Unknown IANA timezone: {data.notice_timezone}") from exc
            previous = None
            previous_tasks = []
            if data.supersedes_contract_id:
                previous = db.execute(
                    "SELECT * FROM contracts WHERE id=? AND tenant_id=? AND status='active'",
                    (data.supersedes_contract_id, tenant_id),
                ).fetchone()
                if not previous:
                    raise ValueError("supersedes_contract_id must reference an active contract in this organization")
                previous_tasks = [dict(row) for row in db.execute(
                    "SELECT id,title,due_date,expiration_date,owner_email,workflow_state FROM tasks "
                    "WHERE contract_id=? AND tenant_id=? AND status='open' ORDER BY due_date,id",
                    (data.supersedes_contract_id, tenant_id),
                ).fetchall()]

        after = data.model_dump(mode="json")
        before = json.loads(previous["confirmed_json"] or previous["extracted_json"]) if previous else {}
        compared_fields = (
            "contract", "start_date", "expiration_date", "renewal_notice_days", "notice_day_type",
            "notice_timezone", "notice_holidays", "notice_method", "notice_recipient", "auto_renew",
            "termination_notice", "owner_name", "owner_email", "annual_contract_value", "currency",
        )
        changes = {
            field: {"before": before.get(field), "after": after.get(field)}
            for field in compared_fields if previous and before.get(field) != after.get(field)
        }
        return {
            "contract_id": contract_id,
            "amendment": previous is not None,
            "supersedes": ({
                "contract_id": previous["id"], "filename": previous["filename"],
                "contract": before.get("contract") or previous["filename"], "status_after_confirmation": "superseded",
            } if previous else None),
            "changes": changes,
            "impact": {
                "open_tasks_to_resolve": previous_tasks,
                "new_task": {
                    "title": f"Review renewal: {data.contract or proposal['filename']}",
                    "due_date": due.isoformat(), "expiration_date": data.expiration_date.isoformat(),
                    "owner_email": data.owner_email,
                },
            },
        }

    def list_contract_versions(self, contract_id: str, tenant_id: str = "default") -> list[dict[str, Any]]:
        """Return the complete amendment lineage with deterministic term deltas."""
        with self.connect() as db:
            target = db.execute(
                "SELECT id FROM contracts WHERE id=? AND tenant_id=?", (contract_id, tenant_id),
            ).fetchone()
            if not target:
                return []
            links = db.execute(
                "SELECT contract_id, prior_contract_id, linked_by, linked_at FROM contract_versions WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall()
            parent_by_child = {row["contract_id"]: row["prior_contract_id"] for row in links}
            child_by_parent = {row["prior_contract_id"]: row["contract_id"] for row in links}
            root = contract_id
            visited: set[str] = set()
            while root in parent_by_child and root not in visited:
                visited.add(root)
                root = parent_by_child[root]
            chain = [root]
            visited = {root}
            while chain[-1] in child_by_parent:
                child = child_by_parent[chain[-1]]
                if child in visited:
                    break
                visited.add(child)
                chain.append(child)
            placeholders = ",".join("?" for _ in chain)
            rows = db.execute(
                f"SELECT * FROM contracts WHERE tenant_id=? AND id IN ({placeholders})",
                (tenant_id, *chain),
            ).fetchall()
        by_id = {row["id"]: row for row in rows}
        link_by_child = {row["contract_id"]: row for row in links}
        result: list[dict[str, Any]] = []
        previous_terms: dict[str, Any] | None = None
        compared_fields = (
            "contract", "start_date", "expiration_date", "renewal_notice_days", "notice_day_type",
            "notice_timezone", "notice_holidays", "notice_method", "notice_recipient", "auto_renew",
            "termination_notice",
        )
        for version_id in chain:
            row = by_id.get(version_id)
            if not row:
                continue
            terms_json = row["confirmed_json"] or row["extracted_json"]
            terms = json.loads(terms_json)
            current_terms = {field: terms.get(field) for field in compared_fields}
            delta = {}
            if previous_terms is not None:
                delta = {
                    field: {"from": previous_terms.get(field), "to": current_terms.get(field)}
                    for field in compared_fields
                    if previous_terms.get(field) != current_terms.get(field)
                }
            link = link_by_child.get(version_id)
            result.append({
                "contract_id": version_id,
                "filename": row["filename"],
                "status": row["status"],
                "created_at": row["created_at"],
                "confirmed_at": row["confirmed_at"],
                "supersedes_contract_id": link["prior_contract_id"] if link else None,
                "linked_by": link["linked_by"] if link else None,
                "linked_at": link["linked_at"] if link else None,
                "terms": current_terms,
                "changes_from_previous": delta,
            })
            previous_terms = current_terms
        return result

    def get_review_feedback(self, contract_id: str, tenant_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM review_feedback WHERE contract_id=? AND tenant_id=?", (contract_id, tenant_id),
            ).fetchone()

    def list_review_feedback(self, tenant_id: str, status: str = "approved", limit: int = 1000) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM review_feedback WHERE tenant_id=? AND status=? ORDER BY captured_at LIMIT ?",
                (tenant_id, status, limit),
            ).fetchall()

    def approve_review_feedback(
        self, contract_id: str, tenant_id: str, actor: str, approval_reference: str,
        clause_categories: list[str], evaluation_case: dict[str, Any],
    ) -> sqlite3.Row | None:
        now = utc_now().isoformat()
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM review_feedback WHERE contract_id=? AND tenant_id=? AND status='captured'",
                (contract_id, tenant_id),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE review_feedback SET status='approved', evaluation_case_json=?, approval_reference=?, clause_categories_json=?, approved_by=?, approved_at=? "
                "WHERE id=? AND tenant_id=? AND status='captured'",
                (json.dumps(evaluation_case, ensure_ascii=False), approval_reference, json.dumps(clause_categories), actor, now, row["id"], tenant_id),
            )
            self.add_audit(db, contract_id, "evaluation_feedback.approved", actor, {
                "feedback_id": row["id"], "approval_reference": approval_reference,
                "clause_categories": clause_categories,
            }, tenant_id)
            return db.execute("SELECT * FROM review_feedback WHERE id=?", (row["id"],)).fetchone()

    def reject_contract(self, contract_id: str, actor: str, tenant_id: str = "default") -> None:
        with self.connect() as db:
            row = db.execute("SELECT status FROM contracts WHERE id=? AND tenant_id=?", (contract_id, tenant_id)).fetchone()
            if not row:
                raise KeyError(contract_id)
            if row["status"] != "pending_review":
                raise ValueError("Only contracts pending review can be rejected")
            db.execute("UPDATE contracts SET status='rejected' WHERE id=?", (contract_id,))
            self.add_audit(db, contract_id, "contract.rejected", actor, {}, tenant_id)

    @staticmethod
    def add_audit(db: sqlite3.Connection, contract_id: str, event_type: str, actor: str, details: dict[str, Any], tenant_id: str = "default") -> None:
        db.execute(
            "INSERT INTO audit_events (contract_id, event_type, actor, details_json, created_at, tenant_id) VALUES (?, ?, ?, ?, ?, ?)",
            (contract_id, event_type, actor, json.dumps(details, default=str), utc_now().isoformat(), tenant_id),
        )

    def audit(self, contract_id: str, tenant_id: str = "default") -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM audit_events WHERE contract_id=? AND tenant_id=? ORDER BY id", (contract_id, tenant_id)).fetchall()

    def list_tasks(
        self, status: str | None = None, owner_email: str | None = None,
        tenant_id: str = "default", workflow_state: str | None = None,
        due_before: date | None = None, unassigned: bool = False, kind: str | None = None,
    ) -> list[sqlite3.Row]:
        with self.connect() as db:
            clauses = ["tenant_id=?"]
            params: list[Any] = [tenant_id]
            if status:
                clauses.append("status=?")
                params.append(status)
            if owner_email is not None:
                clauses.append("lower(owner_email)=lower(?)")
                params.append(owner_email)
            if workflow_state:
                clauses.append("workflow_state=?")
                params.append(workflow_state)
            if due_before:
                clauses.append("due_date<=?")
                params.append(due_before.isoformat())
            if unassigned:
                clauses.append("(owner_email IS NULL OR trim(owner_email)='')")
            if kind:
                clauses.append("task_kind=?")
                params.append(kind)
            return db.execute(
                f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} ORDER BY due_date", tuple(params),
            ).fetchall()

    def get_task(self, task_id: str, tenant_id: str = "default") -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()

    def due_tasks(
        self, today: date | None = None, tenant_id: str | None = None, max_days_before: int = 0,
    ) -> list[sqlite3.Row]:
        through = (today + timedelta(days=max(0, max_days_before))).isoformat() if today else None
        with self.connect() as db:
            if through is None and tenant_id is None:
                return db.execute("SELECT * FROM tasks WHERE status='open' ORDER BY due_date").fetchall()
            if through is not None and tenant_id is None:
                return db.execute("SELECT * FROM tasks WHERE status='open' AND due_date<=? ORDER BY due_date", (through,)).fetchall()
            if through is None:
                return db.execute("SELECT * FROM tasks WHERE tenant_id=? AND status='open' ORDER BY due_date", (tenant_id,)).fetchall()
            return db.execute("SELECT * FROM tasks WHERE tenant_id=? AND status='open' AND due_date<=? ORDER BY due_date", (tenant_id, through)).fetchall()

    def mark_notified(self, task_id: str, escalation: bool, channel: str) -> None:
        stamp = utc_now().isoformat()
        field = "escalated_at" if escalation else "reminder_sent_at"
        with self.connect() as db:
            task = db.execute("SELECT contract_id, tenant_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                return
            cursor = db.execute(f"UPDATE tasks SET {field}=? WHERE id=? AND {field} IS NULL", (stamp, task_id))
            if cursor.rowcount:
                self.add_audit(db, task["contract_id"], "task.escalated" if escalation else "task.reminder_sent", "system", {"task_id": task_id, "channel": channel}, task["tenant_id"])

    def record_notification_delivery(
        self, task_id: str, channel: str, notification_type: str, status: str,
        duration_ms: int, error_message: str | None = None,
    ) -> None:
        stamp = utc_now().isoformat()
        with self.connect() as db:
            task = db.execute("SELECT contract_id, tenant_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                return
            db.execute(
                "INSERT INTO notification_deliveries (tenant_id, task_id, channel, notification_type, status, duration_ms, error_message, attempted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task["tenant_id"], task_id, channel, notification_type, status, max(0, duration_ms), error_message[:1000] if error_message else None, stamp),
            )
            self.add_audit(db, task["contract_id"], f"notification.{status}", "system", {
                "task_id": task_id, "channel": channel, "notification_type": notification_type,
                "duration_ms": max(0, duration_ms),
            }, task["tenant_id"])

    def operations_snapshot(self, tenant_id: str, stuck_after_minutes: int = 15) -> dict[str, Any]:
        now = utc_now()
        cutoff = (now - timedelta(minutes=stuck_after_minutes)).isoformat()
        hour_ago = (now - timedelta(hours=1)).isoformat()
        with self.connect() as db:
            counts = db.execute(
                "SELECT job_type, status, COUNT(*) AS count FROM jobs WHERE tenant_id=? GROUP BY job_type, status ORDER BY job_type, status",
                (tenant_id,),
            ).fetchall()
            queued_stuck = db.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE tenant_id=? AND status='queued' AND available_at<=?",
                (tenant_id, cutoff),
            ).fetchone()["count"]
            expired_leases = db.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE tenant_id=? AND status='running' AND lease_expires_at<=?",
                (tenant_id, now.isoformat()),
            ).fetchone()["count"]
            retrying = db.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE tenant_id=? AND status IN ('queued','running') AND attempts>1",
                (tenant_id,),
            ).fetchone()["count"]
            recent_jobs = db.execute(
                "SELECT id, job_type, status, attempts, max_attempts, last_error, result_json, created_at, updated_at, completed_at "
                "FROM jobs WHERE tenant_id=? ORDER BY updated_at DESC LIMIT 50", (tenant_id,),
            ).fetchall()
            deliveries = db.execute(
                "SELECT channel, notification_type, status, COUNT(*) AS count "
                "FROM notification_deliveries WHERE tenant_id=? GROUP BY channel, notification_type, status "
                "ORDER BY channel, notification_type, status", (tenant_id,),
            ).fetchall()
            failed_deliveries_hour = db.execute(
                "SELECT COUNT(*) AS count FROM notification_deliveries WHERE tenant_id=? AND status='failed' AND attempted_at>=?",
                (tenant_id, hour_ago),
            ).fetchone()["count"]
            recent_deliveries = db.execute(
                "SELECT task_id, channel, notification_type, status, duration_ms, error_message, attempted_at "
                "FROM notification_deliveries WHERE tenant_id=? ORDER BY attempted_at DESC LIMIT 50", (tenant_id,),
            ).fetchall()
        by_status: dict[str, int] = {}
        by_type: dict[str, dict[str, int]] = {}
        for row in counts:
            by_status[row["status"]] = by_status.get(row["status"], 0) + row["count"]
            by_type.setdefault(row["job_type"], {})[row["status"]] = row["count"]
        return {
            "generated_at": now.isoformat(), "tenant_id": tenant_id,
            "jobs": {"by_status": by_status, "by_type": by_type, "queued_stuck": queued_stuck,
                     "expired_leases": expired_leases, "retrying": retrying,
                     "recent": [dict(row) for row in recent_jobs]},
            "notifications": {"by_channel": [dict(row) for row in deliveries],
                               "failed_last_hour": failed_deliveries_hour,
                               "recent": [dict(row) for row in recent_deliveries]},
            "alerts": [],
        }

    def get_tenant_policy(self, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT policy_json, updated_by, updated_at FROM tenant_policies WHERE tenant_id=?",
                (tenant_id,),
            ).fetchone()
        if not row:
            return None
        return {"policy": json.loads(row["policy_json"]), "updated_by": row["updated_by"], "updated_at": row["updated_at"]}

    def save_tenant_policy(self, tenant_id: str, policy: dict[str, Any], actor: str) -> dict[str, Any]:
        updated_at = utc_now().isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO tenant_policies (tenant_id, policy_json, updated_by, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET policy_json=excluded.policy_json, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (tenant_id, json.dumps(policy, ensure_ascii=False), actor, updated_at),
            )
        return {"policy": policy, "updated_by": actor, "updated_at": updated_at}

    def task_notification_keys(self, task_id: str, tenant_id: str) -> set[str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT action_key FROM task_notification_state WHERE task_id=? AND tenant_id=?",
                (task_id, tenant_id),
            ).fetchall()
        return {row["action_key"] for row in rows}

    def record_task_notification(self, task_id: str, tenant_id: str, action_key: str, channel: str) -> bool:
        sent_at = utc_now().isoformat()
        with self.connect() as db:
            task = db.execute(
                "SELECT contract_id FROM tasks WHERE id=? AND tenant_id=? AND status='open'",
                (task_id, tenant_id),
            ).fetchone()
            if not task:
                return False
            inserted = db.execute(
                "INSERT INTO task_notification_state (task_id, tenant_id, action_key, sent_at, channel) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id, action_key) DO NOTHING",
                (task_id, tenant_id, action_key, sent_at, channel),
            )
            if inserted.rowcount:
                self.add_audit(db, task["contract_id"], "task.notification_schedule_completed", "system", {
                    "task_id": task_id, "action_key": action_key, "channel": channel,
                }, tenant_id)
                return True
            return False

    def save_integration_connection(
        self, tenant_id: str, integration_name: str, provider: str, settings: dict[str, str],
        credentials_ciphertext: str, actor: str,
    ) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO tenant_integrations "
                "(tenant_id,integration_name,provider,settings_json,credentials_ciphertext,status,updated_by,updated_at,last_checked_at,last_error) "
                "VALUES (?,?,?,?,?,'configured',?,?,NULL,NULL) "
                "ON CONFLICT(tenant_id,integration_name) DO UPDATE SET provider=excluded.provider, "
                "settings_json=excluded.settings_json,credentials_ciphertext=excluded.credentials_ciphertext, "
                "status='configured',updated_by=excluded.updated_by,updated_at=excluded.updated_at,last_error=NULL",
                (tenant_id, integration_name, provider, json.dumps(settings), credentials_ciphertext, actor, now),
            )
            db.execute(
                "INSERT INTO integration_audit (tenant_id,integration_name,action,actor,created_at) VALUES (?,?,?,?,?)",
                (tenant_id, integration_name, "connection.saved", actor, now),
            )
        return self.get_integration_connection(tenant_id, integration_name)

    def get_integration_connection(self, tenant_id: str, integration_name: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM tenant_integrations WHERE tenant_id=? AND integration_name=?",
                (tenant_id, integration_name),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["settings"] = json.loads(result.pop("settings_json"))
        return result

    def list_integration_connections(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT integration_name,provider,settings_json,status,updated_by,updated_at,last_checked_at,last_error "
                "FROM tenant_integrations WHERE tenant_id=? ORDER BY integration_name",
                (tenant_id,),
            ).fetchall()
        return [{**dict(row), "settings": json.loads(row["settings_json"])} for row in rows]

    def update_integration_health(
        self, tenant_id: str, integration_name: str, status: str, last_error: str | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE tenant_integrations SET status=?,last_checked_at=?,last_error=? "
                "WHERE tenant_id=? AND integration_name=?",
                (status, utc_now().isoformat(), last_error, tenant_id, integration_name),
            )

    def delete_integration_connection(self, tenant_id: str, integration_name: str, actor: str) -> bool:
        now = utc_now().isoformat()
        with self.connect() as db:
            deleted = db.execute(
                "DELETE FROM tenant_integrations WHERE tenant_id=? AND integration_name=?",
                (tenant_id, integration_name),
            )
            if deleted.rowcount:
                db.execute(
                    "INSERT INTO integration_audit (tenant_id,integration_name,action,actor,created_at) VALUES (?,?,?,?,?)",
                    (tenant_id, integration_name, "connection.deleted", actor, now),
                )
                return True
            return False

    def renewal_risk_snapshot(self, tenant_id: str, as_of: date | None = None) -> dict[str, Any]:
        today = as_of or date.today()
        with self.connect() as db:
            tasks = db.execute(
                "SELECT t.*, c.extracted_json, c.confirmed_json "
                "FROM tasks t JOIN contracts c ON c.id=t.contract_id "
                "WHERE t.tenant_id=? AND t.status='open' AND t.task_kind='renewal' ORDER BY t.due_date, t.id",
                (tenant_id,),
            ).fetchall()
            pending = db.execute(
                "SELECT id, filename, extracted_json, created_at FROM contracts "
                "WHERE tenant_id=? AND status='pending_review' ORDER BY created_at",
                (tenant_id,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        spend_at_risk: dict[str, Decimal] = {}
        high_risk_spend: dict[str, Decimal] = {}
        overdue = unassigned = due_30 = expiring_90 = high_risk = 0
        for task in tasks:
            due = date.fromisoformat(task["due_date"])
            expiration = date.fromisoformat(task["expiration_date"])
            days_to_due = (due - today).days
            days_to_expiration = (expiration - today).days
            factors: list[str] = []
            score = 0
            if days_to_due < 0:
                overdue += 1
                factors.append("overdue")
                score += 50
            elif days_to_due <= 7:
                factors.append("deadline_within_7_days")
                score += 30
            elif days_to_due <= 30:
                factors.append("deadline_within_30_days")
                score += 15
            if not (task["owner_email"] or "").strip():
                unassigned += 1
                factors.append("unassigned_owner")
                score += 25
            if task["workflow_state"] in {"needs_changes", "pending_approval"}:
                factors.append(f"workflow_{task['workflow_state']}")
                score += 15
            if 0 <= days_to_due <= 30:
                due_30 += 1
            if 0 <= days_to_expiration <= 90:
                expiring_90 += 1
                factors.append("expiration_within_90_days")
                score += 10
            score = min(score, 100)
            if score >= 50:
                high_risk += 1
            terms = json.loads(task["confirmed_json"] or task["extracted_json"])
            value = terms.get("annual_contract_value")
            currency = terms.get("currency")
            if value is not None and currency:
                amount = Decimal(str(value))
                spend_at_risk[currency] = spend_at_risk.get(currency, Decimal(0)) + amount
                if score >= 50:
                    high_risk_spend[currency] = high_risk_spend.get(currency, Decimal(0)) + amount
            items.append({
                "task_id": task["id"], "contract_id": task["contract_id"],
                "contract": terms.get("contract") or task["title"],
                "title": task["title"], "owner_name": task["owner_name"],
                "owner_email": task["owner_email"], "workflow_state": task["workflow_state"],
                "due_date": task["due_date"], "expiration_date": task["expiration_date"],
                "days_to_due": days_to_due, "risk_score": score,
                "annual_contract_value": terms.get("annual_contract_value"), "currency": currency,
                "risk_level": "high" if score >= 50 else "medium" if score >= 25 else "low",
                "risk_factors": factors,
            })
        pending_reviews = []
        for contract in pending:
            terms = json.loads(contract["extracted_json"])
            age_days = max(0, (today - date.fromisoformat(contract["created_at"][:10])).days)
            pending_reviews.append({
                "contract_id": contract["id"], "contract": terms.get("contract") or contract["filename"],
                "filename": contract["filename"], "created_at": contract["created_at"],
                "age_days": age_days, "risk_level": "high" if age_days >= 7 else "medium" if age_days >= 3 else "low",
            })
        items.sort(key=lambda item: (-item["risk_score"], item["days_to_due"], item["contract_id"]))
        pending_reviews.sort(key=lambda item: (-item["age_days"], item["created_at"]))
        return {
            "tenant_id": tenant_id, "as_of": today.isoformat(),
            "summary": {
                "pending_review_contracts": len(pending_reviews), "overdue_tasks": overdue,
                "unassigned_open_tasks": unassigned, "renewals_due_30_days": due_30,
                "expirations_next_90_days": expiring_90, "high_risk_tasks": high_risk,
                "annual_spend_at_risk": {key: f"{value:.2f}" for key, value in sorted(spend_at_risk.items())},
                "annual_spend_high_risk": {key: f"{value:.2f}" for key, value in sorted(high_risk_spend.items())},
            },
            "tasks": items, "pending_reviews": pending_reviews,
        }

    def open_for_escalation(self, cutoff: date | None, tenant_id: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as db:
            if cutoff is None and tenant_id is None:
                return db.execute("SELECT * FROM tasks WHERE status='open' AND reminder_sent_at IS NOT NULL AND escalated_at IS NULL").fetchall()
            if cutoff is None:
                return db.execute("SELECT * FROM tasks WHERE tenant_id=? AND status='open' AND reminder_sent_at IS NOT NULL AND escalated_at IS NULL", (tenant_id,)).fetchall()
            if tenant_id is None:
                return db.execute("SELECT * FROM tasks WHERE status='open' AND reminder_sent_at IS NOT NULL AND escalated_at IS NULL AND substr(reminder_sent_at,1,10) <= ?", (cutoff.isoformat(),)).fetchall()
            return db.execute("SELECT * FROM tasks WHERE tenant_id=? AND status='open' AND reminder_sent_at IS NOT NULL AND escalated_at IS NULL AND substr(reminder_sent_at,1,10) <= ?", (tenant_id, cutoff.isoformat())).fetchall()

    def resolve_task(self, task_id: str, actor: str, comment: str | None, tenant_id: str = "default") -> sqlite3.Row | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            if not row:
                return None
            if row["status"] != "open":
                raise ValueError("Task is already resolved")
            db.execute("UPDATE tasks SET status='resolved', workflow_state='resolved' WHERE id=?", (task_id,))
            self.add_audit(db, row["contract_id"], "task.resolved", actor, {"task_id": task_id, "comment": comment}, tenant_id)
            return row

    def transition_task(self, task_id: str, workflow_state: str, actor: str, comment: str | None, tenant_id: str) -> sqlite3.Row | None:
        allowed = {
            "review": {"needs_changes", "pending_approval", "renewed", "terminated", "cancelled"},
            "needs_changes": {"review", "pending_approval", "cancelled"},
            "pending_approval": {"review", "notice_in_progress", "renewed", "terminated", "cancelled"},
            "notice_in_progress": {"pending_approval", "renewed", "terminated", "cancelled"},
        }
        terminal = workflow_state in {"renewed", "terminated", "cancelled"}
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            if not row:
                return None
            if row["status"] != "open":
                raise ValueError("Only open tasks can change workflow state")
            if workflow_state not in allowed.get(row["workflow_state"], set()):
                raise ValueError(f"Workflow transition {row['workflow_state']} -> {workflow_state} is not allowed")
            status = "resolved" if terminal else "open"
            db.execute("UPDATE tasks SET workflow_state=?, status=? WHERE id=?", (workflow_state, status, task_id))
            self.add_audit(db, row["contract_id"], "task.workflow_transition", actor, {
                "task_id": task_id, "from": row["workflow_state"], "to": workflow_state, "comment": comment,
            }, tenant_id)
            return db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def assign_task(self, task_id: str, owner_name: str | None, owner_email: str | None, actor: str, tenant_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            if not row:
                return None
            if row["status"] != "open":
                raise ValueError("Only open tasks can be reassigned")
            db.execute("UPDATE tasks SET owner_name=?, owner_email=? WHERE id=?", (owner_name, owner_email, task_id))
            self.add_audit(db, row["contract_id"], "task.assigned", actor, {
                "task_id": task_id, "owner_name": owner_name, "owner_email": owner_email,
            }, tenant_id)
            return db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def add_task_comment(self, task_id: str, body: str, actor: str, tenant_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            task = db.execute("SELECT contract_id FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            if not task:
                return None
            cursor = db.execute(
                "INSERT INTO task_comments (task_id, tenant_id, actor, body, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
                (task_id, tenant_id, actor, body, utc_now().isoformat()),
            )
            comment_id = cursor.fetchone()["id"]
            self.add_audit(db, task["contract_id"], "task.comment_added", actor, {"task_id": task_id, "comment_id": comment_id}, tenant_id)
            return db.execute("SELECT * FROM task_comments WHERE id=?", (comment_id,)).fetchone()

    def list_task_comments(self, task_id: str, tenant_id: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM task_comments WHERE task_id=? AND tenant_id=? ORDER BY id", (task_id, tenant_id)).fetchall()

    def create_notice(self, task_id: str, tenant_id: str, actor: str, subject: str, body: str, recipient: str, delivery_method: str) -> sqlite3.Row:
        notice_id = str(uuid4())
        now = utc_now().isoformat()
        with self.connect() as db:
            task = db.execute("SELECT contract_id, status FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            if not task:
                raise KeyError(task_id)
            if task["status"] != "open":
                raise ValueError("A notice can only be prepared for an open renewal task")
            db.execute(
                "INSERT INTO notices (id, task_id, tenant_id, status, subject, body, recipient, delivery_method, created_by, created_at) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?)",
                (notice_id, task_id, tenant_id, subject, body, recipient, delivery_method, actor, now),
            )
            self.add_audit(db, task["contract_id"], "notice.drafted", actor, {"notice_id": notice_id, "task_id": task_id, "delivery_method": delivery_method}, tenant_id)
            return db.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()

    def get_notice(self, notice_id: str, tenant_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM notices WHERE id=? AND tenant_id=?", (notice_id, tenant_id)).fetchone()

    def list_notices(self, task_id: str, tenant_id: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM notices WHERE task_id=? AND tenant_id=? ORDER BY created_at DESC", (task_id, tenant_id)).fetchall()

    def approve_notice(self, notice_id: str, tenant_id: str, actor: str) -> sqlite3.Row | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT n.*, t.contract_id, t.status AS task_status FROM notices n JOIN tasks t ON t.id=n.task_id AND t.tenant_id=n.tenant_id WHERE n.id=? AND n.tenant_id=?",
                (notice_id, tenant_id),
            ).fetchone()
            if not row:
                return None
            if row["status"] != "draft" or row["task_status"] != "open":
                raise ValueError("Only a draft notice on an open task can be approved")
            stamp = utc_now().isoformat()
            db.execute("UPDATE notices SET status='approved', approved_by=?, approved_at=? WHERE id=?", (actor, stamp, notice_id))
            self.add_audit(db, row["contract_id"], "notice.approved", actor, {"notice_id": notice_id}, tenant_id)
            return db.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()

    def dispatch_notice(self, notice_id: str, tenant_id: str, actor: str, sent_at: datetime, reference: str, note: str | None) -> sqlite3.Row | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT n.*, t.contract_id, t.status AS task_status FROM notices n JOIN tasks t ON t.id=n.task_id AND t.tenant_id=n.tenant_id WHERE n.id=? AND n.tenant_id=?",
                (notice_id, tenant_id),
            ).fetchone()
            if not row:
                return None
            if row["status"] != "approved" or row["task_status"] != "open":
                raise ValueError("Only an approved notice on an open task can be marked as sent")
            stamp = sent_at.astimezone(timezone.utc).isoformat()
            db.execute(
                "UPDATE notices SET status='dispatched', dispatched_by=?, dispatched_at=?, delivery_reference=?, delivery_note=? WHERE id=?",
                (actor, stamp, reference, note, notice_id),
            )
            self.add_audit(db, row["contract_id"], "notice.dispatched", actor, {"notice_id": notice_id, "reference": reference, "method": row["delivery_method"]}, tenant_id)
            return db.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()

    def mark_notice_delivered(self, notice_id: str, tenant_id: str, actor: str, delivered_at: datetime, reference: str | None, evidence_note: str) -> sqlite3.Row | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT n.*, t.contract_id FROM notices n JOIN tasks t ON t.id=n.task_id AND t.tenant_id=n.tenant_id WHERE n.id=? AND n.tenant_id=?",
                (notice_id, tenant_id),
            ).fetchone()
            if not row:
                return None
            if row["status"] != "dispatched":
                raise ValueError("Only a dispatched notice can be marked delivered")
            stamp = delivered_at.astimezone(timezone.utc).isoformat()
            db.execute(
                "UPDATE notices SET status='delivered', delivered_at=?, delivery_reference=COALESCE(?, delivery_reference), delivery_note=? WHERE id=?",
                (stamp, reference, evidence_note, notice_id),
            )
            self.add_audit(db, row["contract_id"], "notice.delivered", actor, {"notice_id": notice_id, "reference": reference, "evidence_note": evidence_note}, tenant_id)
            return db.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()

    def get_calendar_event(self, task_id: str, provider: str, tenant_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM calendar_events WHERE task_id=? AND provider=? AND tenant_id=?",
                (task_id, provider, tenant_id),
            ).fetchone()

    def save_calendar_event(self, task_id: str, provider: str, event_id: str, tenant_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO calendar_events (task_id, provider, event_id, tenant_id, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id, provider, tenant_id) DO UPDATE SET event_id=excluded.event_id, updated_at=excluded.updated_at",
                (task_id, provider, event_id, tenant_id, utc_now().isoformat()),
            )
            task = db.execute("SELECT contract_id FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            if task:
                self.add_audit(db, task["contract_id"], "calendar.event_synced", "system", {"task_id": task_id, "provider": provider, "event_id": event_id}, tenant_id)

    def list_calendar_events(self, provider: str, tenant_id: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM calendar_events WHERE provider=? AND tenant_id=?",
                (provider, tenant_id),
            ).fetchall()

    def delete_calendar_event(self, task_id: str, provider: str, tenant_id: str) -> None:
        with self.connect() as db:
            task = db.execute("SELECT contract_id FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            db.execute(
                "DELETE FROM calendar_events WHERE task_id=? AND provider=? AND tenant_id=?",
                (task_id, provider, tenant_id),
            )
            if task:
                self.add_audit(db, task["contract_id"], "calendar.event_removed", "system", {"task_id": task_id, "provider": provider}, tenant_id)

    def enqueue_job(
        self, tenant_id: str, job_type: str, payload: dict[str, Any], idempotency_key: str,
        *, max_attempts: int = 5, available_at: datetime | None = None,
    ) -> sqlite3.Row:
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs (id, tenant_id, job_type, payload_json, idempotency_key, max_attempts, available_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id, idempotency_key) DO NOTHING",
                (str(uuid4()), tenant_id, job_type, json.dumps(payload), idempotency_key,
                 max_attempts, (available_at or utc_now()).isoformat(), now, now),
            )
            return db.execute(
                "SELECT * FROM jobs WHERE tenant_id=? AND idempotency_key=?", (tenant_id, idempotency_key),
            ).fetchone()

    def get_job(self, job_id: str, tenant_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE id=? AND tenant_id=?", (job_id, tenant_id)).fetchone()

    def list_jobs(self, tenant_id: str, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM jobs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?", (tenant_id, limit),
            ).fetchall()

    def claim_job(self, worker_id: str, lease_seconds: int = 120, job_id: str | None = None) -> sqlite3.Row | None:
        now = utc_now()
        stamp = now.isoformat()
        lease_until = datetime.fromtimestamp(now.timestamp() + lease_seconds, timezone.utc).isoformat()
        with self.connect() as db:
            if self.is_postgres:
                db.execute("BEGIN")
            else:
                db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE jobs SET status='dead', last_error=COALESCE(last_error, 'Worker lease expired after max attempts'), "
                "lease_owner=NULL, lease_expires_at=NULL, updated_at=?, completed_at=? "
                "WHERE status='running' AND lease_expires_at<=? AND attempts>=max_attempts",
                (stamp, stamp, stamp),
            )
            ready = "((status='queued' AND available_at<=?) OR (status='running' AND lease_expires_at<=? AND attempts<max_attempts))"
            if job_id:
                row = db.execute(
                    f"SELECT * FROM jobs WHERE id=? AND {ready} ORDER BY available_at, created_at LIMIT 1"
                    + (" FOR UPDATE SKIP LOCKED" if self.is_postgres else ""),
                    (job_id, stamp, stamp),
                ).fetchone()
            else:
                row = db.execute(
                    f"SELECT * FROM jobs WHERE {ready} ORDER BY available_at, created_at LIMIT 1"
                    + (" FOR UPDATE SKIP LOCKED" if self.is_postgres else ""), (stamp, stamp),
                ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, lease_owner=?, lease_expires_at=?, updated_at=? WHERE id=?",
                (worker_id, lease_until, stamp, row["id"]),
            )
            return db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()

    def complete_job(self, job_id: str, worker_id: str, result: dict[str, Any]) -> bool:
        stamp = utc_now().isoformat()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET status='succeeded', result_json=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=?, completed_at=? "
                "WHERE id=? AND status='running' AND lease_owner=?",
                (json.dumps(result, default=str), stamp, stamp, job_id, worker_id),
            )
            return cursor.rowcount == 1

    def fail_job(self, job_id: str, worker_id: str, error: str, retry_delay_seconds: int | None = None) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT attempts, max_attempts FROM jobs WHERE id=? AND status='running' AND lease_owner=?",
                (job_id, worker_id),
            ).fetchone()
            if not row:
                return False
            now = utc_now()
            exhausted = row["attempts"] >= row["max_attempts"]
            base_delay = max(1, int(os.getenv("JOB_RETRY_BASE_SECONDS", "30")))
            delay = retry_delay_seconds if retry_delay_seconds is not None else min(base_delay * (2 ** (row["attempts"] - 1)), 3600)
            next_state = "dead" if exhausted else "queued"
            updated = now.isoformat()
            available = (now + timedelta(seconds=max(0, delay))).isoformat()
            db.execute(
                "UPDATE jobs SET status=?, available_at=?, last_error=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=?, completed_at=? WHERE id=?",
                (next_state, available, error[:4000], updated, updated if exhausted else None, job_id),
            )
            return True

    def retry_dead_job(self, job_id: str, tenant_id: str) -> sqlite3.Row | None:
        stamp = utc_now().isoformat()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET status='queued', attempts=0, last_error=NULL, available_at=?, updated_at=?, completed_at=NULL "
                "WHERE id=? AND tenant_id=? AND status='dead'",
                (stamp, stamp, job_id, tenant_id),
            )
            return db.execute("SELECT * FROM jobs WHERE id=? AND tenant_id=?", (job_id, tenant_id)).fetchone() if cursor.rowcount else None

    def record_access(self, tenant_id: str, actor: str, action: str, entity_type: str, entity_id: str | None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO access_events (tenant_id, actor, action, entity_type, entity_id, accessed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (tenant_id, actor, action, entity_type, entity_id, utc_now().isoformat()),
            )

    def access_history(self, tenant_id: str, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM access_events WHERE tenant_id=? ORDER BY id DESC LIMIT ?", (tenant_id, limit),
            ).fetchall()

    def place_legal_hold(self, contract_id: str, tenant_id: str, actor: str, reason: str) -> sqlite3.Row | None:
        hold_id = str(uuid4())
        stamp = utc_now().isoformat()
        with self.connect() as db:
            contract = db.execute(
                "SELECT id FROM contracts WHERE id=? AND tenant_id=? AND status!='redacted'", (contract_id, tenant_id),
            ).fetchone()
            if not contract:
                return None
            cursor = db.execute(
                "INSERT INTO legal_holds (id, contract_id, tenant_id, reason, placed_by, placed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (hold_id, contract_id, tenant_id, reason, actor, stamp),
            )
            self.add_audit(db, contract_id, "contract.legal_hold_placed", actor, {"hold_id": hold_id, "reason": reason}, tenant_id)
            return db.execute("SELECT * FROM legal_holds WHERE id=?", (hold_id,)).fetchone()

    def release_legal_hold(self, hold_id: str, tenant_id: str, actor: str) -> sqlite3.Row | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM legal_holds WHERE id=? AND tenant_id=?", (hold_id, tenant_id)).fetchone()
            if not row:
                return None
            if row["released_at"]:
                raise ValueError("Legal hold is already released")
            stamp = utc_now().isoformat()
            db.execute("UPDATE legal_holds SET released_by=?, released_at=? WHERE id=?", (actor, stamp, hold_id))
            self.add_audit(db, row["contract_id"], "contract.legal_hold_released", actor, {"hold_id": hold_id}, tenant_id)
            return db.execute("SELECT * FROM legal_holds WHERE id=?", (hold_id,)).fetchone()

    def active_legal_holds(self, contract_id: str, tenant_id: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM legal_holds WHERE contract_id=? AND tenant_id=? AND released_at IS NULL ORDER BY placed_at",
                (contract_id, tenant_id),
            ).fetchall()

    def list_legal_holds(self, contract_id: str, tenant_id: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM legal_holds WHERE contract_id=? AND tenant_id=? ORDER BY placed_at",
                (contract_id, tenant_id),
            ).fetchall()

    def retention_candidates(self, cutoff: datetime, tenant_id: str | None = None) -> list[sqlite3.Row]:
        stamp = cutoff.isoformat()
        with self.connect() as db:
            query = (
                "SELECT c.id, c.tenant_id FROM contracts c "
                "WHERE c.created_at<=? AND (c.status IN ('rejected','superseded') OR "
                "(c.status='active' AND EXISTS (SELECT 1 FROM tasks t WHERE t.contract_id=c.id) "
                "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.contract_id=c.id AND t.status='open') "
                "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.contract_id=c.id AND t.expiration_date>?))) "
                "AND NOT EXISTS (SELECT 1 FROM legal_holds h WHERE h.contract_id=c.id AND h.tenant_id=c.tenant_id AND h.released_at IS NULL)"
            )
            parameters: tuple = (stamp, cutoff.date().isoformat())
            if tenant_id is not None:
                query += " AND c.tenant_id=?"
                parameters += (tenant_id,)
            return db.execute(query, parameters).fetchall()

    def redact_contract(self, contract_id: str, tenant_id: str, actor: str) -> bool:
        with self.connect() as db:
            contract = db.execute(
                "SELECT * FROM contracts WHERE id=? AND tenant_id=?", (contract_id, tenant_id),
            ).fetchone()
            if not contract:
                return False
            hold = db.execute(
                "SELECT 1 FROM legal_holds WHERE contract_id=? AND tenant_id=? AND released_at IS NULL LIMIT 1",
                (contract_id, tenant_id),
            ).fetchone()
            if hold:
                raise ValueError("Contract is protected by an active legal hold")
            tasks = db.execute("SELECT id FROM tasks WHERE contract_id=?", (contract_id,)).fetchall()
            for task in tasks:
                db.execute("DELETE FROM calendar_events WHERE task_id=?", (task["id"],))
                db.execute("DELETE FROM notification_deliveries WHERE task_id=?", (task["id"],))
                db.execute("DELETE FROM task_notification_state WHERE task_id=?", (task["id"],))
                db.execute("DELETE FROM task_comments WHERE task_id=?", (task["id"],))
                db.execute("DELETE FROM notices WHERE task_id=?", (task["id"],))
                db.execute("DELETE FROM contract_obligations WHERE task_id=?", (task["id"],))
                db.execute("DELETE FROM renewal_outcomes WHERE task_id=?", (task["id"],))
            db.execute("DELETE FROM tasks WHERE contract_id=?", (contract_id,))
            db.execute("DELETE FROM external_documents WHERE contract_id=?", (contract_id,))
            db.execute("DELETE FROM review_feedback WHERE contract_id=? AND tenant_id=?", (contract_id, tenant_id))
            db.execute("DELETE FROM audit_events WHERE contract_id=? AND tenant_id=?", (contract_id, tenant_id))
            db.execute(
                "UPDATE contracts SET filename='redacted.pdf', status='redacted', provider='redacted', extracted_json='{}', "
                "confirmed_json=NULL, raw_text='', confirmed_at=NULL WHERE id=? AND tenant_id=?",
                (contract_id, tenant_id),
            )
            self.add_audit(db, contract_id, "contract.data_redacted", actor, {}, tenant_id)
            return True

    def redact_expired_contracts(self, retention_days: int, actor: str = "system", tenant_id: str | None = None) -> int:
        if retention_days <= 0:
            return 0
        cutoff = utc_now() - timedelta(days=retention_days)
        candidates = self.retention_candidates(cutoff, tenant_id)
        redacted = 0
        for row in candidates:
            redacted += int(self.redact_contract(row["id"], row["tenant_id"], actor))
        return redacted


def _amendment_match_key(value: str) -> str:
    ignored = {"agreement", "contract", "amendment", "addendum", "the", "and", "of"}
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    return " ".join(token for token in tokens if token not in ignored)


def _monthly_prorated_costs(annual_value: Decimal, start: date, end: date) -> dict[str, Decimal]:
    if end < start:
        return {}
    result: dict[str, Decimal] = {}
    cursor = start
    while cursor <= end:
        last_of_month = date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1])
        segment_end = min(end, last_of_month)
        days = (segment_end - cursor).days + 1
        year_days = 366 if calendar.isleap(cursor.year) else 365
        key = cursor.strftime("%Y-%m")
        result[key] = result.get(key, Decimal(0)) + annual_value * Decimal(days) / Decimal(year_days)
        cursor = segment_end + timedelta(days=1)
    return result


def _money_string(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate_notice_deadline(expiration: date, notice_days: int, day_type: str, holidays: set[date] | None = None) -> date:
    """Calculate the action deadline using the explicit contract counting convention."""
    if day_type == "calendar":
        return date.fromordinal(expiration.toordinal() - notice_days)
    if day_type != "business":
        raise ValueError("notice_day_type must be 'calendar' or 'business'")
    current = expiration
    holidays = holidays or set()
    remaining = notice_days
    while remaining:
        current = date.fromordinal(current.toordinal() - 1)
        if current.weekday() < 5 and current not in holidays:
            remaining -= 1
    return current
