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
from uuid import uuid4
import calendar
from statistics import median

from .schemas import ContractData
from .deadlines import calculate_deadline


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value else None


def _approval_age_days(created_at: str, as_of: date) -> int:
    created = datetime.fromisoformat(created_at)
    if as_of == date.today():
        now = datetime.now(timezone.utc)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0, int((now - created).total_seconds() // 86400))
    return max(0, (as_of - created.date()).days)


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
                    risk_level TEXT NOT NULL DEFAULT 'medium',
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
                    notice_jurisdiction TEXT,
                    deadline_calculation_json TEXT NOT NULL DEFAULT '{}',
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
                    created_at TEXT NOT NULL,
                    recurrence_interval_months INTEGER,
                    recurrence_group_id TEXT NOT NULL,
                    schedule_status TEXT NOT NULL DEFAULT 'active'
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
                CREATE TABLE IF NOT EXISTS renewal_forecast_runs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    renewal_increase_pct TEXT NOT NULL,
                    renegotiation_savings_pct TEXT NOT NULL,
                    recorded_by TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS renewal_forecast_lines (
                    run_id TEXT NOT NULL REFERENCES renewal_forecast_runs(id),
                    tenant_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    supplier TEXT,
                    currency TEXT,
                    annual_contract_value TEXT NOT NULL,
                    PRIMARY KEY (run_id, contract_id)
                );
                CREATE TABLE IF NOT EXISTS renewal_decisions (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
                    forecast_run_id TEXT REFERENCES renewal_forecast_runs(id),
                    strategy TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    reason TEXT NOT NULL,
                    owner_name TEXT,
                    owner_email TEXT,
                    target_date TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    review_comment TEXT
                );
                CREATE TABLE IF NOT EXISTS renewal_decision_milestones (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL REFERENCES renewal_decisions(id),
                    title TEXT NOT NULL,
                    due_date TEXT,
                    owner_email TEXT,
                    completed INTEGER NOT NULL DEFAULT 0,
                    completed_by TEXT,
                    completed_at TEXT,
                    comment TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS renewal_decision_quotes (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL REFERENCES renewal_decisions(id),
                    supplier TEXT,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    quote_date TEXT,
                    valid_until TEXT,
                    reference TEXT,
                    note TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS renewal_decision_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL REFERENCES renewal_decisions(id),
                    event_type TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS renewal_scenario_plans (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    forecast_run_id TEXT REFERENCES renewal_forecast_runs(id),
                    assumptions_json TEXT NOT NULL,
                    results_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS data_retention_policies (
                    tenant_id TEXT PRIMARY KEY,
                    contract_days INTEGER NOT NULL DEFAULT 0,
                    evidence_days INTEGER NOT NULL DEFAULT 0,
                    provider_evaluation_days INTEGER NOT NULL DEFAULT 0,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS governance_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
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
                    attempted_at TEXT NOT NULL,
                    recipient TEXT
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
                CREATE TABLE IF NOT EXISTS contract_relationships (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    related_contract_id TEXT NOT NULL REFERENCES contracts(id),
                    relationship_type TEXT NOT NULL,
                    note TEXT,
                    linked_by TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    CHECK (contract_id <> related_contract_id),
                    UNIQUE (tenant_id, contract_id, related_contract_id, relationship_type)
                );
                CREATE TABLE IF NOT EXISTS procurement_sync_runs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    suppliers_seen INTEGER NOT NULL DEFAULT 0,
                    contracts_seen INTEGER NOT NULL DEFAULT 0,
                    imported INTEGER NOT NULL DEFAULT 0,
                    duplicates INTEGER NOT NULL DEFAULT 0,
                    conflicts_json TEXT NOT NULL DEFAULT '[]',
                    error_message TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS procurement_supplier_records (
                    tenant_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
                    PRIMARY KEY (tenant_id, provider, external_id)
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
                CREATE TABLE IF NOT EXISTS obligation_evidence (
                    id TEXT PRIMARY KEY,
                    obligation_id TEXT NOT NULL REFERENCES contract_obligations(id),
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    tenant_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    content_base64 TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    comment TEXT,
                    status TEXT NOT NULL DEFAULT 'pending_signoff',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    review_comment TEXT
                );
                CREATE TABLE IF NOT EXISTS obligation_compliance_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    obligation_id TEXT NOT NULL REFERENCES contract_obligations(id),
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    tenant_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    evidence_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS renewal_playbooks (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    min_contract_value TEXT,
                    currency TEXT,
                    supplier_risk_levels_json TEXT NOT NULL,
                    changed_clause_categories_json TEXT NOT NULL,
                    required_approvers_json TEXT NOT NULL,
                    checklist_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'active',
                    UNIQUE (tenant_id, name)
                );
                CREATE TABLE IF NOT EXISTS renewal_playbook_history (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    playbook_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_playbook_reviews (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    playbook_id TEXT NOT NULL REFERENCES renewal_playbooks(id),
                    required_approvers_json TEXT NOT NULL,
                    approvals_json TEXT NOT NULL,
                    checklist_json TEXT NOT NULL,
                    changed_clause_categories_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'blocked',
                    applied_by TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    playbook_snapshot_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS approval_notification_state (
                    tenant_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    action_key TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, target_type, target_id, action_key)
                );
                CREATE TABLE IF NOT EXISTS approval_notification_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    recipient TEXT,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    error_message TEXT,
                    attempted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_evaluation_runs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT,
                    dataset_fingerprint TEXT NOT NULL,
                    cases_scored INTEGER NOT NULL,
                    exact_accuracy REAL,
                    evidence_support_accuracy REAL,
                    clause_accuracy REAL,
                    amendment_comparison_accuracy REAL,
                    report_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portfolio_import_batches (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    field_mapping_json TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'previewed',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    committed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS portfolio_import_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL REFERENCES portfolio_import_batches(id),
                    tenant_id TEXT NOT NULL,
                    row_number INTEGER NOT NULL,
                    dataset TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_id TEXT,
                    message TEXT,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_evaluation_schedules (
                    tenant_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    interval_hours INTEGER NOT NULL DEFAULT 24,
                    run_on_provider_change INTEGER NOT NULL DEFAULT 1,
                    alert_emails_json TEXT NOT NULL DEFAULT '[]',
                    last_run_at TEXT,
                    provider_signature TEXT,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluation_alert_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    recipient TEXT,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    error_message TEXT,
                    attempted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portfolio_saved_filters (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    filters_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_contract_status ON contracts(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, due_date);
                CREATE INDEX IF NOT EXISTS idx_audit_contract ON audit_events(contract_id, id);
                """
            if self.is_postgres:
                schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
            db.executescript(schema)
            if self.is_postgres:
                db.execute("ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS recipient TEXT")
                db.execute("ALTER TABLE renewal_playbooks ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1")
                db.execute("ALTER TABLE renewal_playbooks ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
                db.execute("ALTER TABLE task_playbook_reviews ADD COLUMN IF NOT EXISTS playbook_snapshot_json TEXT NOT NULL DEFAULT '{}'")
            else:
                for table, column, declaration in (
                    ("notification_deliveries", "recipient", "TEXT"),
                    ("renewal_playbooks", "version", "INTEGER NOT NULL DEFAULT 1"),
                    ("renewal_playbooks", "status", "TEXT NOT NULL DEFAULT 'active'"),
                    ("task_playbook_reviews", "playbook_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
                ):
                    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                    if column not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            # Upgrade databases created by earlier versions without dropping data.
            for table in ("contracts", "tasks", "audit_events"):
                if self.is_postgres:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'")
                    if table == "tasks":
                        db.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notice_day_type TEXT NOT NULL DEFAULT 'calendar'")
                        db.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notice_timezone TEXT NOT NULL DEFAULT 'UTC'")
                        db.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notice_holidays_json TEXT NOT NULL DEFAULT '[]'")
                        db.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notice_jurisdiction TEXT")
                        db.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deadline_calculation_json TEXT NOT NULL DEFAULT '{}'")
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
                    if table == "tasks" and "notice_jurisdiction" not in columns:
                        db.execute("ALTER TABLE tasks ADD COLUMN notice_jurisdiction TEXT")
                    if table == "tasks" and "deadline_calculation_json" not in columns:
                        db.execute("ALTER TABLE tasks ADD COLUMN deadline_calculation_json TEXT NOT NULL DEFAULT '{}'")
                    if table == "tasks" and "workflow_state" not in columns:
                        db.execute("ALTER TABLE tasks ADD COLUMN workflow_state TEXT NOT NULL DEFAULT 'review'")
                    if table == "tasks" and "task_kind" not in columns:
                        db.execute("ALTER TABLE tasks ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'renewal'")
                if self.is_postgres and table == "contracts":
                    db.execute("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS supplier_id TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS idx_contract_tenant ON contracts(tenant_id, status)")
            obligation_columns = {row["name"] for row in db.execute("PRAGMA table_info(contract_obligations)")} if not self.is_postgres else {
                row["column_name"] for row in db.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='contract_obligations'"
                )
            }
            if "recurrence_interval_months" not in obligation_columns:
                db.execute("ALTER TABLE contract_obligations ADD COLUMN recurrence_interval_months INTEGER")
            if "recurrence_group_id" not in obligation_columns:
                db.execute("ALTER TABLE contract_obligations ADD COLUMN recurrence_group_id TEXT")
                db.execute("UPDATE contract_obligations SET recurrence_group_id=id WHERE recurrence_group_id IS NULL")
            if "schedule_status" not in obligation_columns:
                db.execute("ALTER TABLE contract_obligations ADD COLUMN schedule_status TEXT NOT NULL DEFAULT 'active'")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_tenant ON tasks(tenant_id, status, due_date)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_kind_due ON tasks(tenant_id, task_kind, status, due_date)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_obligations_contract ON contract_obligations(tenant_id, contract_id, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(status, available_at, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_access_tenant ON access_events(tenant_id, accessed_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_notification_tenant ON notification_deliveries(tenant_id, attempted_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON review_feedback(tenant_id, status, captured_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_contract_versions_prior ON contract_versions(tenant_id, prior_contract_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_contract_relationships_tenant ON contract_relationships(tenant_id,contract_id,related_contract_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_notification_tenant ON task_notification_state(tenant_id, sent_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_supplier_tenant ON suppliers(tenant_id, normalized_name)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_outcome_tenant_date ON renewal_outcomes(tenant_id, recorded_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_forecast_tenant_date ON renewal_forecast_runs(tenant_id, recorded_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_forecast_lines_contract ON renewal_forecast_lines(tenant_id, contract_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_scenario_tenant_task ON renewal_scenario_plans(tenant_id,task_id,created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_governance_audit_tenant_time ON governance_audit_events(tenant_id,created_at)")
            if self.is_postgres:
                db.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS risk_level TEXT NOT NULL DEFAULT 'medium'")
            else:
                supplier_columns = {row["name"] for row in db.execute("PRAGMA table_info(suppliers)")}
                if "risk_level" not in supplier_columns:
                    db.execute("ALTER TABLE suppliers ADD COLUMN risk_level TEXT NOT NULL DEFAULT 'medium'")
            db.execute("CREATE INDEX IF NOT EXISTS idx_obligation_evidence_tenant ON obligation_evidence(tenant_id,obligation_id,created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_provider_eval_tenant ON provider_evaluation_runs(tenant_id,provider,created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_playbook_tenant ON task_playbook_reviews(tenant_id,task_id)")
            if self.is_postgres:
                db.execute("ALTER TABLE provider_evaluation_runs ADD COLUMN IF NOT EXISTS amendment_comparison_accuracy REAL")
            else:
                eval_columns = {row["name"] for row in db.execute("PRAGMA table_info(provider_evaluation_runs)")}
                if "amendment_comparison_accuracy" not in eval_columns:
                    db.execute("ALTER TABLE provider_evaluation_runs ADD COLUMN amendment_comparison_accuracy REAL")

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

    def find_or_create_procurement_supplier(self, tenant_id: str, provider: str, external_id: str, name: str) -> dict[str, Any]:
        normalized = self._supplier_key(name)
        with self.connect() as db:
            linked = db.execute(
                "SELECT s.* FROM procurement_supplier_records p JOIN suppliers s ON s.id=p.supplier_id "
                "WHERE p.tenant_id=? AND p.provider=? AND p.external_id=?",
                (tenant_id, provider, external_id),
            ).fetchone()
            if linked:
                return dict(linked)
            existing = db.execute("SELECT * FROM suppliers WHERE tenant_id=? AND normalized_name=?", (tenant_id, normalized)).fetchone()
        supplier = dict(existing) if existing else self.create_supplier(name, f"integration:{provider}", tenant_id)
        with self.connect() as db:
            db.execute(
                "INSERT INTO procurement_supplier_records (tenant_id,provider,external_id,supplier_id) VALUES (?,?,?,?) "
                "ON CONFLICT(tenant_id,provider,external_id) DO NOTHING",
                (tenant_id, provider, external_id, supplier["id"]),
            )
            row = db.execute(
                "SELECT s.* FROM procurement_supplier_records p JOIN suppliers s ON s.id=p.supplier_id "
                "WHERE p.tenant_id=? AND p.provider=? AND p.external_id=?",
                (tenant_id, provider, external_id),
            ).fetchone()
            return dict(row)

    def procurement_contract_revision(self, tenant_id: str, provider: str, external_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT revision,contract_id,ingested_at FROM external_documents "
                "WHERE tenant_id=? AND provider=? AND source_key='contracts' AND external_id=? "
                "ORDER BY ingested_at DESC LIMIT 1",
                (tenant_id, provider, external_id),
            ).fetchone()
            return dict(row) if row else None

    def start_procurement_sync(self, run_id: str, tenant_id: str, provider: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO procurement_sync_runs (id,tenant_id,provider,status,started_at) VALUES (?,?,?,'running',?)",
                (run_id, tenant_id, provider, utc_now().isoformat()),
            )

    def finish_procurement_sync(self, run_id: str, tenant_id: str, status: str,
                                summary: dict[str, Any], error_message: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE procurement_sync_runs SET status=?,suppliers_seen=?,contracts_seen=?,imported=?,duplicates=?,"
                "conflicts_json=?,error_message=?,finished_at=? WHERE id=? AND tenant_id=?",
                (status, summary.get("suppliers_seen", 0), summary.get("contracts_seen", 0),
                 summary.get("imported", 0), summary.get("duplicates", 0), json.dumps(summary.get("conflicts", [])),
                 error_message, utc_now().isoformat(), run_id, tenant_id),
            )

    def procurement_sync_history(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM procurement_sync_runs WHERE tenant_id=? ORDER BY started_at DESC LIMIT ?",
                (tenant_id, max(1, min(200, limit))),
            ).fetchall()
        return [{**dict(row), "conflicts": json.loads(row["conflicts_json"])} for row in rows]

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

    def create_supplier(self, name: str, actor: str, tenant_id: str, risk_level: str = "medium") -> dict[str, Any]:
        if risk_level not in {"low", "medium", "high", "critical"}:
            raise ValueError("Supplier risk level must be low, medium, high, or critical")
        normalized = self._supplier_key(name)
        now = utc_now().isoformat()
        supplier_id = str(uuid4())
        with self.connect() as db:
            try:
                db.execute(
                    "INSERT INTO suppliers (id,tenant_id,name,normalized_name,created_by,created_at,updated_at,risk_level) VALUES (?,?,?,?,?,?,?,?)",
                    (supplier_id, tenant_id, name.strip(), normalized, actor, now, now, risk_level),
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

    def update_supplier_risk(self, supplier_id: str, risk_level: str, tenant_id: str, actor: str) -> dict[str, Any] | None:
        if risk_level not in {"low", "medium", "high", "critical"}:
            raise ValueError("Supplier risk level must be low, medium, high, or critical")
        with self.connect() as db:
            supplier = db.execute("SELECT * FROM suppliers WHERE id=? AND tenant_id=?", (supplier_id, tenant_id)).fetchone()
            if not supplier:
                return None
            db.execute("UPDATE suppliers SET risk_level=?,updated_at=? WHERE id=? AND tenant_id=?", (risk_level, utc_now().isoformat(), supplier_id, tenant_id))
            contracts = db.execute("SELECT id FROM contracts WHERE supplier_id=? AND tenant_id=?", (supplier_id, tenant_id)).fetchall()
            for contract in contracts:
                self.add_audit(db, contract["id"], "supplier.risk_updated", actor, {"supplier_id": supplier_id, "risk_level": risk_level}, tenant_id)
            return dict(db.execute("SELECT * FROM suppliers WHERE id=? AND tenant_id=?", (supplier_id, tenant_id)).fetchone())

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
        recurrence_months = terms.get("recurrence_interval_months")
        recurrence_group = str(uuid4())
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
                "INSERT INTO contract_obligations "
                "(id,tenant_id,contract_id,task_id,category,title,description,evidence_json,created_by,created_at,recurrence_interval_months,recurrence_group_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (obligation_id, tenant_id, contract_id, task_id, terms["category"], terms["title"],
                 terms.get("description"), json.dumps(evidence, default=str), actor, now,
                 recurrence_months, recurrence_group),
            )
            self.add_audit(db, contract_id, "obligation.scheduled", actor, {
                "obligation_id": obligation_id, "task_id": task_id, "category": terms["category"],
                "title": terms["title"], "due_date": due_date.isoformat(), "owner_email": owner_email,
                "recurrence_interval_months": recurrence_months,
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

    def _create_next_obligation_occurrence(self, db, task, actor: str) -> str | None:
        obligation = db.execute(
            "SELECT * FROM contract_obligations WHERE task_id=? AND tenant_id=?",
            (task["id"], task["tenant_id"]),
        ).fetchone()
        if not obligation or not obligation["recurrence_interval_months"]:
            return None
        if obligation["schedule_status"] != "active":
            return None
        contract = db.execute(
            "SELECT status,confirmed_json FROM contracts WHERE id=? AND tenant_id=?",
            (task["contract_id"], task["tenant_id"]),
        ).fetchone()
        if not contract or contract["status"] != "active":
            return None
        terms = json.loads(contract["confirmed_json"] or "{}")
        expiration = date.fromisoformat(terms["expiration_date"]) if terms.get("expiration_date") else None
        due = _add_months(date.fromisoformat(task["due_date"]), obligation["recurrence_interval_months"])
        if expiration and due > expiration:
            return None
        task_id, obligation_id = str(uuid4()), str(uuid4())
        now = utc_now().isoformat()
        db.execute(
            "INSERT INTO tasks (id,contract_id,title,owner_name,owner_email,due_date,expiration_date,status,created_at,tenant_id,task_kind) "
            "VALUES (?,?,?,?,?,?,?,'open',?,?,'obligation')",
            (task_id, task["contract_id"], task["title"], task["owner_name"], task["owner_email"],
             due.isoformat(), due.isoformat(), now, task["tenant_id"]),
        )
        db.execute(
            "INSERT INTO contract_obligations "
            "(id,tenant_id,contract_id,task_id,category,title,description,evidence_json,created_by,created_at,recurrence_interval_months,recurrence_group_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (obligation_id, obligation["tenant_id"], obligation["contract_id"], task_id,
             obligation["category"], obligation["title"], obligation["description"],
             obligation["evidence_json"], "system:recurrence", now,
             obligation["recurrence_interval_months"], obligation["recurrence_group_id"] or obligation["id"]),
        )
        self.add_audit(db, task["contract_id"], "obligation.occurrence_created", actor, {
            "obligation_id": obligation_id, "task_id": task_id, "previous_task_id": task["id"],
            "due_date": due.isoformat(), "recurrence_interval_months": obligation["recurrence_interval_months"],
        }, task["tenant_id"])
        return task_id

    def update_obligation_schedule(
        self, obligation_id: str, tenant_id: str, actor: str, *,
        status: str | None = None, recurrence_interval_months: int | None = None,
    ) -> dict[str, Any] | None:
        if status is not None and status not in {"active", "paused", "ended"}:
            raise ValueError("Schedule status must be active, paused, or ended")
        if recurrence_interval_months is not None and not 1 <= recurrence_interval_months <= 120:
            raise ValueError("Recurrence interval must be between 1 and 120 months")
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM contract_obligations WHERE id=? AND tenant_id=?", (obligation_id, tenant_id),
            ).fetchone()
            if not row:
                return None
            new_status = status or row["schedule_status"]
            if row["schedule_status"] == "ended" and new_status != "ended":
                raise ValueError("An ended schedule cannot be reactivated")
            new_interval = recurrence_interval_months if recurrence_interval_months is not None else row["recurrence_interval_months"]
            if new_status == "active" and not new_interval:
                raise ValueError("An active recurring schedule requires a cadence")
            group = row["recurrence_group_id"] or obligation_id
            db.execute(
                "UPDATE contract_obligations SET schedule_status=?,recurrence_interval_months=? "
                "WHERE tenant_id=? AND recurrence_group_id=?",
                (new_status, new_interval, tenant_id, group),
            )
            self.add_audit(db, row["contract_id"], "obligation.schedule_updated", actor, {
                "obligation_id": obligation_id, "recurrence_group_id": group,
                "schedule_status": new_status, "recurrence_interval_months": new_interval,
            }, tenant_id)
            result = db.execute(
                "SELECT o.*,t.status AS task_status,t.owner_name,t.owner_email,t.due_date,t.reminder_sent_at,t.escalated_at "
                "FROM contract_obligations o JOIN tasks t ON t.id=o.task_id WHERE o.id=? AND o.tenant_id=?",
                (obligation_id, tenant_id),
            ).fetchone()
        return self._obligation_record(result)

    def reschedule_obligation(
        self, obligation_id: str, due_date: date, tenant_id: str, actor: str,
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT o.*,t.status AS task_status,t.due_date,t.owner_name,t.owner_email,t.reminder_sent_at,t.escalated_at "
                "FROM contract_obligations o JOIN tasks t ON t.id=o.task_id WHERE o.id=? AND o.tenant_id=?",
                (obligation_id, tenant_id),
            ).fetchone()
            if not row:
                return None
            if row["task_status"] != "open":
                raise ValueError("Only open obligation occurrences can be rescheduled")
            contract = db.execute(
                "SELECT confirmed_json FROM contracts WHERE id=? AND tenant_id=?", (row["contract_id"], tenant_id),
            ).fetchone()
            terms = json.loads(contract["confirmed_json"] or "{}")
            if terms.get("start_date") and due_date < date.fromisoformat(terms["start_date"]):
                raise ValueError("Obligation due date cannot precede the contract start date")
            if terms.get("expiration_date") and due_date > date.fromisoformat(terms["expiration_date"]):
                raise ValueError("Obligation due date cannot follow the contract expiration date")
            db.execute("UPDATE tasks SET due_date=?,expiration_date=? WHERE id=?", (due_date.isoformat(), due_date.isoformat(), row["task_id"]))
            self.add_audit(db, row["contract_id"], "obligation.rescheduled", actor, {
                "obligation_id": obligation_id, "task_id": row["task_id"],
                "previous_due_date": row["due_date"], "due_date": due_date.isoformat(),
            }, tenant_id)
            result = db.execute(
                "SELECT o.*,t.status AS task_status,t.owner_name,t.owner_email,t.due_date,t.reminder_sent_at,t.escalated_at "
                "FROM contract_obligations o JOIN tasks t ON t.id=o.task_id WHERE o.id=? AND o.tenant_id=?",
                (obligation_id, tenant_id),
            ).fetchone()
        return self._obligation_record(result)

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

    def get_contract_obligation(self, obligation_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT o.*,t.status AS task_status,t.owner_name,t.owner_email,t.due_date,t.reminder_sent_at,t.escalated_at "
                "FROM contract_obligations o JOIN tasks t ON t.id=o.task_id WHERE o.id=? AND o.tenant_id=?",
                (obligation_id, tenant_id),
            ).fetchone()
        return self._obligation_record(row) if row else None

    def bulk_portfolio_action(self, action: str, ids: list[str], tenant_id: str, actor: str, **values) -> dict[str, Any]:
        """Apply a validated tenant-scoped portfolio action atomically."""
        with self.connect() as db:
            if action == "assign_tasks":
                rows = db.execute(
                    f"SELECT * FROM tasks WHERE tenant_id=? AND id IN ({','.join('?' for _ in ids)})",
                    (tenant_id, *ids),
                ).fetchall()
                if len(rows) != len(ids) or any(row["status"] != "open" for row in rows):
                    raise ValueError("All selected tasks must exist in this organization and be open")
                for row in rows:
                    db.execute("UPDATE tasks SET owner_name=?,owner_email=? WHERE id=?", (values.get("owner_name"), values.get("owner_email"), row["id"]))
                    self.add_audit(db, row["contract_id"], "task.assigned", actor, {
                        "task_id": row["id"], "owner_name": values.get("owner_name"), "owner_email": values.get("owner_email"), "bulk": True,
                    }, tenant_id)
            elif action == "transition_tasks":
                rows = db.execute(
                    f"SELECT * FROM tasks WHERE tenant_id=? AND id IN ({','.join('?' for _ in ids)})",
                    (tenant_id, *ids),
                ).fetchall()
                target = values.get("workflow_state")
                allowed = {"review": {"needs_changes", "pending_approval"}, "needs_changes": {"review", "pending_approval"},
                           "pending_approval": {"review"}, "notice_in_progress": {"pending_approval"}}
                if len(rows) != len(ids) or any(row["status"] != "open" or target not in allowed.get(row["workflow_state"], set()) for row in rows):
                    raise ValueError("All selected tasks must exist, be open, and support the requested transition")
                for row in rows:
                    db.execute("UPDATE tasks SET workflow_state=? WHERE id=?", (target, row["id"]))
                    self.add_audit(db, row["contract_id"], "task.workflow_transition", actor, {
                        "task_id": row["id"], "from": row["workflow_state"], "to": target,
                        "comment": values.get("comment"), "bulk": True,
                    }, tenant_id)
            elif action == "assign_supplier":
                supplier_id = values.get("supplier_id")
                supplier = db.execute("SELECT id FROM suppliers WHERE id=? AND tenant_id=?", (supplier_id, tenant_id)).fetchone()
                rows = db.execute(
                    f"SELECT * FROM contracts WHERE tenant_id=? AND id IN ({','.join('?' for _ in ids)})", (tenant_id, *ids),
                ).fetchall()
                if not supplier or len(rows) != len(ids):
                    raise ValueError("All selected contracts and the supplier must belong to this organization")
                for row in rows:
                    db.execute("UPDATE contracts SET supplier_id=? WHERE id=?", (supplier_id, row["id"]))
                    self.add_audit(db, row["id"], "contract.supplier_assigned", actor, {"supplier_id": supplier_id, "bulk": True}, tenant_id)
                db.execute("UPDATE suppliers SET updated_at=? WHERE id=?", (utc_now().isoformat(), supplier_id))
            elif action == "assign_obligations":
                rows = db.execute(
                    f"SELECT o.id,o.contract_id,o.task_id,t.status,t.owner_name,t.owner_email FROM contract_obligations o "
                    f"JOIN tasks t ON t.id=o.task_id AND t.tenant_id=o.tenant_id WHERE o.tenant_id=? AND o.id IN ({','.join('?' for _ in ids)})",
                    (tenant_id, *ids),
                ).fetchall()
                if len(rows) != len(ids) or any(row["status"] != "open" for row in rows):
                    raise ValueError("All selected obligations must exist in this organization and be open")
                for row in rows:
                    db.execute("UPDATE tasks SET owner_name=?,owner_email=? WHERE id=? AND tenant_id=?",
                               (values.get("owner_name"), values.get("owner_email"), row["task_id"], tenant_id))
                    self.add_audit(db, row["contract_id"], "obligation.assigned", actor, {
                        "obligation_id": row["id"], "task_id": row["task_id"],
                        "owner_name": values.get("owner_name"), "owner_email": values.get("owner_email"), "bulk": True,
                    }, tenant_id)
            elif action == "review_obligations":
                decision = values.get("decision")
                if decision not in {"approved", "rejected"}:
                    raise ValueError("Evidence decision must be approved or rejected")
                rows = db.execute(
                    f"SELECT e.*,o.contract_id,t.status AS task_status FROM obligation_evidence e "
                    f"JOIN contract_obligations o ON o.id=e.obligation_id AND o.tenant_id=e.tenant_id "
                    f"JOIN tasks t ON t.id=e.task_id AND t.tenant_id=e.tenant_id "
                    f"WHERE e.tenant_id=? AND e.id IN ({','.join('?' for _ in ids)})",
                    (tenant_id, *ids),
                ).fetchall()
                if len(rows) != len(ids) or any(row["status"] != "pending_signoff" or row["task_status"] != "open" for row in rows):
                    raise ValueError("All selected evidence must be pending sign-off for open obligations in this organization")
                now = utc_now().isoformat()
                for row in rows:
                    db.execute("UPDATE obligation_evidence SET status=?,reviewed_by=?,reviewed_at=?,review_comment=? WHERE id=? AND tenant_id=?",
                               (decision, actor, now, values.get("comment"), row["id"], tenant_id))
                    db.execute("INSERT INTO obligation_compliance_history (obligation_id,task_id,tenant_id,outcome,comment,actor,evidence_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                               (row["obligation_id"], row["task_id"], tenant_id,
                                "completed" if decision == "approved" else "evidence_rejected",
                                values.get("comment") or f"Evidence {decision} (bulk review)", actor, row["id"], now))
                    if decision == "approved":
                        db.execute("UPDATE tasks SET status='resolved',workflow_state='resolved' WHERE id=? AND tenant_id=?", (row["task_id"], tenant_id))
                        task = db.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (row["task_id"], tenant_id)).fetchone()
                        self._create_next_obligation_occurrence(db, task, actor)
                    self.add_audit(db, row["contract_id"], f"obligation.evidence_{decision}", actor, {
                        "obligation_id": row["obligation_id"], "task_id": row["task_id"],
                        "evidence_id": row["id"], "comment": values.get("comment"), "bulk": True,
                    }, tenant_id)
            else:
                raise ValueError("Unsupported bulk portfolio action")
        return {"action": action, "updated": ids, "count": len(ids)}

    def submit_obligation_evidence(
        self, obligation_id: str, tenant_id: str, actor: str, filename: str,
        content_type: str, content_base64: str, sha256: str, comment: str | None,
    ) -> dict[str, Any] | None:
        evidence_id, now = str(uuid4()), utc_now().isoformat()
        with self.connect() as db:
            obligation = db.execute(
                "SELECT o.*,t.status AS task_status FROM contract_obligations o JOIN tasks t ON t.id=o.task_id "
                "WHERE o.id=? AND o.tenant_id=?", (obligation_id, tenant_id),
            ).fetchone()
            if not obligation:
                return None
            if obligation["task_status"] != "open":
                raise ValueError("Evidence can be submitted only for an open occurrence")
            db.execute(
                "INSERT INTO obligation_evidence (id,obligation_id,task_id,tenant_id,filename,content_type,content_base64,sha256,comment,status,created_by,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'pending_signoff',?,?)",
                (evidence_id, obligation_id, obligation["task_id"], tenant_id, filename, content_type,
                 content_base64, sha256, comment, actor, now),
            )
            db.execute(
                "INSERT INTO obligation_compliance_history (obligation_id,task_id,tenant_id,outcome,comment,actor,evidence_id,created_at) "
                "VALUES (?,?,?,'submitted_for_signoff',?,?,?,?)",
                (obligation_id, obligation["task_id"], tenant_id, comment or "Evidence submitted for reviewer sign-off", actor, evidence_id, now),
            )
            self.add_audit(db, obligation["contract_id"], "obligation.evidence_submitted", actor, {
                "obligation_id": obligation_id, "task_id": obligation["task_id"], "evidence_id": evidence_id,
                "filename": filename, "sha256": sha256,
            }, tenant_id)
            return self._obligation_evidence_record(db.execute(
                "SELECT * FROM obligation_evidence WHERE id=? AND tenant_id=?", (evidence_id, tenant_id),
            ).fetchone())

    @staticmethod
    def _obligation_evidence_record(row) -> dict[str, Any]:
        record = dict(row)
        record.pop("content_base64", None)
        return record

    def list_obligation_evidence(self, obligation_id: str, tenant_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM obligation_evidence WHERE obligation_id=? AND tenant_id=? ORDER BY created_at",
                (obligation_id, tenant_id),
            ).fetchall()
        return [self._obligation_evidence_record(row) for row in rows]

    def get_obligation_evidence_content(self, evidence_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM obligation_evidence WHERE id=? AND tenant_id=?", (evidence_id, tenant_id),
            ).fetchone()
        return dict(row) if row else None

    def review_obligation_evidence(
        self, evidence_id: str, tenant_id: str, actor: str, decision: str, comment: str | None,
    ) -> dict[str, Any] | None:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Evidence decision must be approved or rejected")
        now = utc_now().isoformat()
        with self.connect() as db:
            evidence = db.execute(
                "SELECT e.*,o.contract_id,t.status AS task_status FROM obligation_evidence e "
                "JOIN contract_obligations o ON o.id=e.obligation_id JOIN tasks t ON t.id=e.task_id "
                "WHERE e.id=? AND e.tenant_id=?", (evidence_id, tenant_id),
            ).fetchone()
            if not evidence:
                return None
            if evidence["status"] != "pending_signoff":
                raise ValueError("Only evidence awaiting sign-off can be reviewed")
            if evidence["task_status"] != "open":
                raise ValueError("Evidence can be reviewed only while its obligation is open")
            db.execute(
                "UPDATE obligation_evidence SET status=?,reviewed_by=?,reviewed_at=?,review_comment=? WHERE id=? AND tenant_id=?",
                (decision, actor, now, comment, evidence_id, tenant_id),
            )
            outcome = "completed" if decision == "approved" else "evidence_rejected"
            db.execute(
                "INSERT INTO obligation_compliance_history (obligation_id,task_id,tenant_id,outcome,comment,actor,evidence_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (evidence["obligation_id"], evidence["task_id"], tenant_id, outcome,
                 comment or f"Evidence {decision}", actor, evidence_id, now),
            )
            if decision == "approved":
                db.execute("UPDATE tasks SET status='resolved',workflow_state='resolved' WHERE id=?", (evidence["task_id"],))
                task = db.execute("SELECT * FROM tasks WHERE id=?", (evidence["task_id"],)).fetchone()
                self._create_next_obligation_occurrence(db, task, actor)
            self.add_audit(db, evidence["contract_id"], f"obligation.evidence_{decision}", actor, {
                "obligation_id": evidence["obligation_id"], "task_id": evidence["task_id"],
                "evidence_id": evidence_id, "comment": comment,
            }, tenant_id)
            return self._obligation_evidence_record(db.execute(
                "SELECT * FROM obligation_evidence WHERE id=? AND tenant_id=?", (evidence_id, tenant_id),
            ).fetchone())

    def record_obligation_compliance(
        self, obligation_id: str, tenant_id: str, actor: str, outcome: str, comment: str,
    ) -> dict[str, Any] | None:
        if outcome not in {"failed", "missed"}:
            raise ValueError("Compliance outcome must be failed or missed")
        now = utc_now().isoformat()
        with self.connect() as db:
            row = db.execute(
                "SELECT o.*,t.status AS task_status,t.due_date FROM contract_obligations o "
                "JOIN tasks t ON t.id=o.task_id WHERE o.id=? AND o.tenant_id=?", (obligation_id, tenant_id),
            ).fetchone()
            if not row:
                return None
            if row["task_status"] != "open":
                raise ValueError("Compliance can be recorded only for open occurrences")
            if outcome == "missed" and date.fromisoformat(row["due_date"]) >= date.today():
                raise ValueError("An occurrence can be marked missed only after its due date")
            db.execute(
                "INSERT INTO obligation_compliance_history (obligation_id,task_id,tenant_id,outcome,comment,actor,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (obligation_id, row["task_id"], tenant_id, outcome, comment, actor, now),
            )
            self.add_audit(db, row["contract_id"], f"obligation.{outcome}", actor, {
                "obligation_id": obligation_id, "task_id": row["task_id"], "comment": comment,
            }, tenant_id)
            return {"obligation_id": obligation_id, "task_id": row["task_id"], "outcome": outcome, "comment": comment, "actor": actor, "created_at": now}

    def obligation_compliance_history(self, obligation_id: str, tenant_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM obligation_compliance_history WHERE obligation_id=? AND tenant_id=? ORDER BY id",
                (obligation_id, tenant_id),
            ).fetchall()]

    def approval_inbox(self, tenant_id: str, sla_days: int = 3, today: date | None = None) -> dict[str, Any]:
        """Return pending evidence sign-offs and playbook decisions with SLA age."""
        as_of = today or date.today()
        with self.connect() as db:
            evidence_rows = db.execute(
                "SELECT e.id AS target_id,e.created_at,e.task_id,e.obligation_id,o.title AS subject,c.id AS contract_id,"
                "COALESCE(t.owner_email,'') AS owner_email,c.confirmed_json,c.extracted_json "
                "FROM obligation_evidence e JOIN contract_obligations o ON o.id=e.obligation_id "
                "JOIN tasks t ON t.id=e.task_id JOIN contracts c ON c.id=o.contract_id "
                "WHERE e.tenant_id=? AND e.status='pending_signoff' AND t.status='open' ORDER BY e.created_at,e.id",
                (tenant_id,),
            ).fetchall()
            playbook_rows = db.execute(
                "SELECT r.id AS target_id,r.applied_at AS created_at,r.task_id,t.contract_id,t.title AS subject,"
                "r.required_approvers_json,r.approvals_json,c.confirmed_json,c.extracted_json "
                "FROM task_playbook_reviews r JOIN tasks t ON t.id=r.task_id JOIN contracts c ON c.id=t.contract_id "
                "WHERE r.tenant_id=? AND r.status='blocked' AND t.status='open' ORDER BY r.applied_at,r.id",
                (tenant_id,),
            ).fetchall()
            deliveries = db.execute(
                "SELECT target_type,target_id,recipient,channel,status,error_message,attempted_at "
                "FROM approval_notification_deliveries WHERE tenant_id=? ORDER BY attempted_at DESC LIMIT 100",
                (tenant_id,),
            ).fetchall()
        entries = []
        for row in evidence_rows:
            terms = json.loads(row["confirmed_json"] or row["extracted_json"] or "{}")
            entries.append({"type": "obligation_signoff", "target_id": row["target_id"], "obligation_id": row["obligation_id"], "task_id": row["task_id"],
                            "contract_id": row["contract_id"], "subject": row["subject"], "pending_since": row["created_at"],
                            "age_days": _approval_age_days(row["created_at"], as_of),
                            "sla_days": sla_days, "sla_breached": _approval_age_days(row["created_at"], as_of) >= sla_days,
                            "reviewers": _configured_approval_reviewers(), "contract": terms.get("contract")})
        for row in playbook_rows:
            terms = json.loads(row["confirmed_json"] or row["extracted_json"] or "{}")
            approvals = json.loads(row["approvals_json"])
            waiting = [email for email, decision in approvals.items() if decision is None]
            entries.append({"type": "playbook_decision", "target_id": row["target_id"], "task_id": row["task_id"],
                            "contract_id": row["contract_id"], "subject": row["subject"], "pending_since": row["created_at"],
                            "age_days": _approval_age_days(row["created_at"], as_of),
                            "sla_days": sla_days, "sla_breached": _approval_age_days(row["created_at"], as_of) >= sla_days,
                            "reviewers": waiting or json.loads(row["required_approvers_json"]), "contract": terms.get("contract")})
        entries.sort(key=lambda item: (not item["sla_breached"], -item["age_days"], item["pending_since"]))
        return {"as_of": as_of.isoformat(), "sla_days": sla_days, "items": entries,
                "deliveries": [dict(row) for row in deliveries]}

    def record_approval_notification(
        self, tenant_id: str, target_type: str, target_id: str, task_id: str,
        action_key: str, recipient: str | None, channel: str, status: str,
        duration_ms: int, error_message: str | None = None,
    ) -> bool:
        now = utc_now().isoformat()
        with self.connect() as db:
            inserted = db.execute(
                "INSERT INTO approval_notification_state (tenant_id,target_type,target_id,action_key,sent_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT (tenant_id,target_type,target_id,action_key) DO NOTHING",
                (tenant_id, target_type, target_id, action_key, now),
            )
            if not inserted.rowcount:
                return False
            db.execute(
                "INSERT INTO approval_notification_deliveries (tenant_id,target_type,target_id,task_id,recipient,channel,status,duration_ms,error_message,attempted_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, target_type, target_id, task_id, recipient, channel, status, max(0, duration_ms),
                 error_message[:1000] if error_message else None, now),
            )
            contract = db.execute("SELECT contract_id FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            if contract:
                self.add_audit(db, contract["contract_id"], "approval.notification_" + status, "system", {
                    "target_type": target_type, "target_id": target_id, "task_id": task_id,
                    "recipient": recipient, "channel": channel, "action_key": action_key,
                    "error": error_message[:500] if error_message else None,
                }, tenant_id)
            return True

    def create_renewal_playbook(self, request: dict[str, Any], actor: str, tenant_id: str) -> dict[str, Any]:
        playbook_id, now = str(uuid4()), utc_now().isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO renewal_playbooks (id,tenant_id,name,min_contract_value,currency,supplier_risk_levels_json,changed_clause_categories_json,required_approvers_json,checklist_json,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (playbook_id, tenant_id, request["name"], str(request["min_contract_value"]) if request.get("min_contract_value") is not None else None,
                 request.get("currency"), json.dumps(request.get("supplier_risk_levels", [])),
                 json.dumps(request.get("changed_clause_categories", [])), json.dumps(request["required_approvers"]),
                 json.dumps(request.get("checklist", [])), actor, now, now),
            )
            result = self._playbook_record(db.execute("SELECT * FROM renewal_playbooks WHERE id=?", (playbook_id,)).fetchone())
            self.add_playbook_history_entry(db, tenant_id, playbook_id, 1, result, actor, "created", now)
            return result

    @staticmethod
    def _playbook_record(row) -> dict[str, Any]:
        result = dict(row)
        for column, key in (("supplier_risk_levels_json", "supplier_risk_levels"),
                            ("changed_clause_categories_json", "changed_clause_categories"),
                            ("required_approvers_json", "required_approvers"), ("checklist_json", "checklist")):
            result[key] = json.loads(result.pop(column))
        return result

    def list_renewal_playbooks(self, tenant_id: str, include_retired: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            if include_retired:
                rows = db.execute("SELECT * FROM renewal_playbooks WHERE tenant_id=? ORDER BY name,version DESC", (tenant_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM renewal_playbooks WHERE tenant_id=? AND status='active' ORDER BY name", (tenant_id,)).fetchall()
        return [self._playbook_record(row) for row in rows]

    def edit_renewal_playbook(
        self, playbook_id: str, request: dict[str, Any], actor: str, tenant_id: str, retire: bool = False,
    ) -> dict[str, Any] | None:
        now = utc_now().isoformat()
        with self.connect() as db:
            row = db.execute("SELECT * FROM renewal_playbooks WHERE id=? AND tenant_id=?", (playbook_id, tenant_id)).fetchone()
            if not row:
                return None
            if row["status"] != "active":
                raise ValueError("Only an active playbook can be edited or retired")
            prior = self._playbook_record(row)
            db.execute(
                "INSERT INTO renewal_playbook_history (id,tenant_id,playbook_id,version,snapshot_json,actor,action,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid4()), tenant_id, playbook_id, row["version"], json.dumps(prior), actor,
                 "retired" if retire else "superseded", now),
            )
            if retire:
                db.execute("UPDATE renewal_playbooks SET status='retired',version=version+1,updated_at=? WHERE id=? AND tenant_id=?",
                           (now, playbook_id, tenant_id))
            else:
                db.execute(
                    "UPDATE renewal_playbooks SET name=?,min_contract_value=?,currency=?,supplier_risk_levels_json=?,"
                    "changed_clause_categories_json=?,required_approvers_json=?,checklist_json=?,version=version+1,updated_at=? "
                    "WHERE id=? AND tenant_id=?",
                    (request["name"], str(request["min_contract_value"]) if request.get("min_contract_value") is not None else None,
                     request.get("currency"), json.dumps(request.get("supplier_risk_levels", [])),
                     json.dumps(request.get("changed_clause_categories", [])), json.dumps(request["required_approvers"]),
                     json.dumps(request.get("checklist", [])), now, playbook_id, tenant_id),
                )
            self.add_playbook_history_entry(db, tenant_id, playbook_id, row["version"] + 1,
                                            self._playbook_record(db.execute("SELECT * FROM renewal_playbooks WHERE id=?", (playbook_id,)).fetchone()),
                                            actor, "retired" if retire else "updated", now)
            return self._playbook_record(db.execute("SELECT * FROM renewal_playbooks WHERE id=?", (playbook_id,)).fetchone())

    @staticmethod
    def add_playbook_history_entry(db, tenant_id: str, playbook_id: str, version: int, snapshot: dict[str, Any], actor: str, action: str, now: str) -> None:
        db.execute(
            "INSERT INTO renewal_playbook_history (id,tenant_id,playbook_id,version,snapshot_json,actor,action,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid4()), tenant_id, playbook_id, version, json.dumps(snapshot), actor, action, now),
        )

    def renewal_playbook_history(self, playbook_id: str, tenant_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM renewal_playbook_history WHERE playbook_id=? AND tenant_id=? ORDER BY version,id", (playbook_id, tenant_id)).fetchall()
        return [{**dict(row), "snapshot": json.loads(row["snapshot_json"])} for row in rows]

    def simulate_renewal_playbooks(self, tenant_id: str) -> dict[str, Any]:
        playbooks = self.list_renewal_playbooks(tenant_id)
        with self.connect() as db:
            tasks = db.execute(
                "SELECT t.id AS task_id,t.contract_id,t.title,t.due_date,t.status,c.confirmed_json,c.extracted_json,c.supplier_id,s.risk_level "
                "FROM tasks t JOIN contracts c ON c.id=t.contract_id LEFT JOIN suppliers s ON s.id=c.supplier_id AND s.tenant_id=c.tenant_id "
                "WHERE t.tenant_id=? AND t.status='open' AND t.task_kind='renewal' ORDER BY t.due_date,t.id", (tenant_id,),
            ).fetchall()
        routes = []
        for task in tasks:
            categories = self.detect_task_changed_clause_categories(task["task_id"], tenant_id)
            terms = json.loads(task["confirmed_json"] or task["extracted_json"])
            matched, decisions = [], []
            for playbook in playbooks:
                reasons = []
                misses = []
                risks = playbook["supplier_risk_levels"]
                if risks and task["risk_level"] not in risks:
                    misses.append("supplier risk does not match")
                clauses = playbook["changed_clause_categories"]
                if clauses and not set(clauses).intersection(categories):
                    misses.append("changed clauses do not match")
                minimum = playbook["min_contract_value"]
                if minimum is not None:
                    if terms.get("currency") != playbook["currency"] or Decimal(str(terms.get("annual_contract_value") or 0)) < Decimal(minimum):
                        misses.append("contract value is below the configured threshold or currency differs")
                if risks and task["risk_level"] in risks:
                    reasons.append("supplier risk matched")
                if clauses and set(clauses).intersection(categories):
                    reasons.append("changed clause matched: " + ", ".join(sorted(set(clauses).intersection(categories))))
                if minimum is not None and not any("contract value" in miss for miss in misses):
                    reasons.append(f"annual value meets {playbook['currency']} {minimum}")
                if not misses:
                    if not reasons:
                        reasons.append("playbook has no conditional rules")
                    matched.append(playbook)
                decisions.append({"playbook_id": playbook["id"], "name": playbook["name"],
                                  "version": playbook["version"], "matched": not misses,
                                  "reasons": reasons, "unmatched_reasons": misses,
                                  "approvers": playbook["required_approvers"] if not misses else [],
                                  "checklist": playbook["checklist"] if not misses else []})
            route = matched[0] if matched else None
            routes.append({"task_id": task["task_id"], "contract_id": task["contract_id"], "title": task["title"],
                           "due_date": task["due_date"], "detected_clause_categories": categories,
                           "route": (route["required_approvers"] if route else []),
                           "route_name": route["name"] if route else None, "matches": decisions})
        return {"tenant_id": tenant_id, "tasks_considered": len(routes), "routes": routes}

    def detect_task_changed_clause_categories(self, task_id: str, tenant_id: str) -> list[str]:
        with self.connect() as db:
            row = db.execute(
                "SELECT current.raw_text AS current_text,current.confirmed_json,prior.raw_text AS prior_text,prior.confirmed_json AS prior_terms "
                "FROM tasks t JOIN contracts current ON current.id=t.contract_id AND current.tenant_id=t.tenant_id "
                "JOIN contract_versions v ON v.contract_id=current.id AND v.tenant_id=current.tenant_id "
                "JOIN contracts prior ON prior.id=v.prior_contract_id AND prior.tenant_id=v.tenant_id "
                "WHERE t.id=? AND t.tenant_id=? ORDER BY v.linked_at DESC LIMIT 1", (task_id, tenant_id),
            ).fetchone()
        if not row:
            return []
        detected = set()
        for change in _compare_source_clauses(row["prior_text"], row["current_text"])["changes"]:
            detected.update(change["categories"])
        current = json.loads(row["confirmed_json"] or "{}")
        prior = json.loads(row["prior_terms"] or "{}")
        for field in ("start_date", "expiration_date"):
            if current.get(field) != prior.get(field):
                detected.add("expiration" if field == "expiration_date" else "effective_term")
        for field in ("renewal_notice_days", "notice_day_type", "notice_method", "notice_recipient"):
            if current.get(field) != prior.get(field):
                detected.add("notice_period")
        if current.get("auto_renew") != prior.get("auto_renew"):
            detected.add("auto_renewal")
        if current.get("termination_notice") != prior.get("termination_notice"):
            detected.add("termination")
        if current.get("annual_contract_value") != prior.get("annual_contract_value") or current.get("currency") != prior.get("currency"):
            detected.add("pricing")
        return sorted(detected)

    def apply_renewal_playbook(
        self, playbook_id: str, task_id: str, tenant_id: str, actor: str, changed_clause_categories: list[str],
    ) -> dict[str, Any] | None:
        if not changed_clause_categories:
            changed_clause_categories = self.detect_task_changed_clause_categories(task_id, tenant_id)
        review_id, now = str(uuid4()), utc_now().isoformat()
        with self.connect() as db:
            playbook = db.execute("SELECT * FROM renewal_playbooks WHERE id=? AND tenant_id=?", (playbook_id, tenant_id)).fetchone()
            task = db.execute(
                "SELECT t.*,c.confirmed_json,c.extracted_json,c.supplier_id,s.risk_level FROM tasks t "
                "JOIN contracts c ON c.id=t.contract_id LEFT JOIN suppliers s ON s.id=c.supplier_id AND s.tenant_id=c.tenant_id "
                "WHERE t.id=? AND t.tenant_id=?", (task_id, tenant_id),
            ).fetchone()
            if not playbook or not task:
                return None
            if playbook["status"] != "active":
                raise ValueError("Retired playbooks cannot be applied")
            if task["task_kind"] != "renewal" or task["status"] != "open":
                raise ValueError("Playbooks can be applied only to open renewal tasks")
            criteria_risks = json.loads(playbook["supplier_risk_levels_json"])
            criteria_clauses = json.loads(playbook["changed_clause_categories_json"])
            if criteria_risks and task["risk_level"] not in criteria_risks:
                raise ValueError("Renewal does not match the playbook supplier-risk criteria")
            if criteria_clauses and not set(criteria_clauses).intersection(changed_clause_categories):
                raise ValueError("Renewal does not match the playbook changed-clause criteria")
            if playbook["min_contract_value"] is not None:
                terms = json.loads(task["confirmed_json"] or task["extracted_json"])
                if terms.get("currency") != playbook["currency"] or Decimal(str(terms.get("annual_contract_value") or 0)) < Decimal(playbook["min_contract_value"]):
                    raise ValueError("Renewal does not meet the playbook contract-value threshold")
            existing = db.execute("SELECT status FROM task_playbook_reviews WHERE tenant_id=? AND task_id=? ORDER BY applied_at DESC LIMIT 1", (tenant_id, task_id)).fetchone()
            if existing and existing["status"] != "rejected":
                raise ValueError("A playbook review is already active for this task")
            approvals = {email: None for email in json.loads(playbook["required_approvers_json"])}
            checklist = [{"item": item, "completed": False, "completed_by": None, "completed_at": None}
                         for item in json.loads(playbook["checklist_json"])]
            db.execute(
                "INSERT INTO task_playbook_reviews (id,tenant_id,task_id,playbook_id,required_approvers_json,approvals_json,checklist_json,changed_clause_categories_json,status,applied_by,applied_at,playbook_snapshot_json) "
                "VALUES (?,?,?,?,?,?,?,?, 'blocked',?,?,?)",
                (review_id, tenant_id, task_id, playbook_id, json.dumps(list(approvals)), json.dumps(approvals),
                 json.dumps(checklist), json.dumps(changed_clause_categories), actor, now, json.dumps(self._playbook_record(playbook))),
            )
            db.execute("UPDATE tasks SET workflow_state='pending_approval' WHERE id=?", (task_id,))
            self.add_audit(db, task["contract_id"], "playbook.applied", actor, {
                "playbook_id": playbook_id, "task_id": task_id, "review_id": review_id,
                "required_approvers": list(approvals), "changed_clause_categories": changed_clause_categories,
            }, tenant_id)
            return self._playbook_review_record(db.execute("SELECT * FROM task_playbook_reviews WHERE id=?", (review_id,)).fetchone())

    @staticmethod
    def _playbook_review_record(row) -> dict[str, Any]:
        result = dict(row)
        for column, key in (("required_approvers_json", "required_approvers"), ("approvals_json", "approvals"),
                            ("checklist_json", "checklist"), ("changed_clause_categories_json", "changed_clause_categories")):
            result[key] = json.loads(result.pop(column))
        result["playbook_snapshot"] = json.loads(result.pop("playbook_snapshot_json", "{}") or "{}")
        return result

    def get_task_playbook_review(self, review_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM task_playbook_reviews WHERE id=? AND tenant_id=?", (review_id, tenant_id)).fetchone()
        return self._playbook_review_record(row) if row else None

    def task_playbook_review(self, task_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM task_playbook_reviews WHERE task_id=? AND tenant_id=? ORDER BY applied_at DESC LIMIT 1", (task_id, tenant_id)).fetchone()
        return self._playbook_review_record(row) if row else None

    def update_playbook_approval(self, task_id: str, tenant_id: str, email: str, approved: bool, actor: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM task_playbook_reviews WHERE task_id=? AND tenant_id=? ORDER BY applied_at DESC LIMIT 1", (task_id, tenant_id)).fetchone()
            if not row:
                return None
            if row["status"] == "rejected":
                raise ValueError("A rejected playbook decision cannot be changed")
            approvals = json.loads(row["approvals_json"])
            key = email.casefold()
            if key not in approvals:
                raise ValueError("Your email is not a required approver for this playbook")
            if approvals[key] is not None:
                raise ValueError("This approver has already recorded a decision")
            approvals[key] = {"approved": approved, "actor": actor, "at": utc_now().isoformat()}
            checklist = json.loads(row["checklist_json"])
            rejected = any(item is not None and not item["approved"] for item in approvals.values())
            all_approved = all(item is not None and item["approved"] for item in approvals.values())
            status = "rejected" if rejected else "approved" if all_approved and all(item["completed"] for item in checklist) else "blocked"
            db.execute("UPDATE task_playbook_reviews SET approvals_json=?,status=? WHERE id=?", (json.dumps(approvals), status, row["id"]))
            task = db.execute("SELECT contract_id FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            if rejected:
                db.execute("UPDATE tasks SET workflow_state='needs_changes' WHERE id=? AND tenant_id=?", (task_id, tenant_id))
            self.add_audit(db, task["contract_id"], "playbook.approval_recorded", actor, {
                "task_id": task_id, "approved": approved, "status": status,
            }, tenant_id)
            return self._playbook_review_record(db.execute("SELECT * FROM task_playbook_reviews WHERE id=?", (row["id"],)).fetchone())

    def update_playbook_checklist(self, task_id: str, tenant_id: str, index: int, completed: bool, actor: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM task_playbook_reviews WHERE task_id=? AND tenant_id=? ORDER BY applied_at DESC LIMIT 1", (task_id, tenant_id)).fetchone()
            if not row:
                return None
            if row["status"] == "rejected":
                raise ValueError("A rejected playbook decision cannot be changed")
            checklist = json.loads(row["checklist_json"])
            if index < 0 or index >= len(checklist):
                raise ValueError("Checklist item index is out of range")
            checklist[index].update({"completed": completed, "completed_by": actor if completed else None,
                                     "completed_at": utc_now().isoformat() if completed else None})
            approvals = json.loads(row["approvals_json"])
            rejected = any(v is not None and not v["approved"] for v in approvals.values())
            status = "rejected" if rejected else "approved" if all(v is not None and v["approved"] for v in approvals.values()) and all(x["completed"] for x in checklist) else "blocked"
            db.execute("UPDATE task_playbook_reviews SET checklist_json=?,status=? WHERE id=?", (json.dumps(checklist), status, row["id"]))
            task = db.execute("SELECT contract_id FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            self.add_audit(db, task["contract_id"], "playbook.checklist_updated", actor, {"task_id": task_id, "index": index, "completed": completed, "status": status}, tenant_id)
            return self._playbook_review_record(db.execute("SELECT * FROM task_playbook_reviews WHERE id=?", (row["id"],)).fetchone())

    def record_provider_evaluation(self, tenant_id: str, provider: str, report: dict[str, Any], fingerprint: str, actor: str) -> dict[str, Any]:
        run_id, now = str(uuid4()), utc_now().isoformat()
        clauses = report.get("clause_categories", {})
        clause_accuracy = clauses.get("accuracy") if isinstance(clauses, dict) else None
        amendment_accuracy = (report.get("amendment_comparison") or {}).get("accuracy")
        with self.connect() as db:
            previous = db.execute(
                "SELECT * FROM provider_evaluation_runs WHERE tenant_id=? AND provider=? AND dataset_fingerprint=? ORDER BY created_at DESC LIMIT 1",
                (tenant_id, provider, fingerprint),
            ).fetchone()
            current_exact = report.get("exact_case_match", {}).get("accuracy")
            current_evidence = report.get("field_evidence_support", {}).get("accuracy")
            alerts = []
            if previous:
                for metric, current, field in (("exact_accuracy", current_exact, "exact_accuracy"),
                                               ("evidence_support_accuracy", current_evidence, "evidence_support_accuracy"),
                                               ("clause_accuracy", clause_accuracy, "clause_accuracy"),
                                               ("amendment_comparison_accuracy", amendment_accuracy, "amendment_comparison_accuracy")):
                    prior = previous[field]
                    if current is not None and prior is not None and prior - current >= 0.10 - 1e-9:
                        alerts.append({"metric": metric, "previous": prior, "current": current, "drop": round(prior-current, 4)})
            db.execute(
                "INSERT INTO provider_evaluation_runs (id,tenant_id,provider,model,dataset_fingerprint,cases_scored,exact_accuracy,evidence_support_accuracy,clause_accuracy,amendment_comparison_accuracy,report_json,created_by,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, tenant_id, provider, report.get("model"), fingerprint, int(report.get("cases_scored", 0)), current_exact,
                 current_evidence, clause_accuracy, amendment_accuracy, json.dumps(report), actor, now),
            )
            return {"id": run_id, "provider": provider, "model": report.get("model"), "dataset_fingerprint": fingerprint,
                    "cases_scored": int(report.get("cases_scored", 0)), "exact_accuracy": current_exact,
                    "evidence_support_accuracy": current_evidence, "clause_accuracy": clause_accuracy,
                    "amendment_comparison_accuracy": amendment_accuracy,
                    "alerts": alerts, "created_at": now}

    def provider_evaluation_history(self, tenant_id: str, provider: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            if provider:
                rows = db.execute("SELECT * FROM provider_evaluation_runs WHERE tenant_id=? AND provider=? ORDER BY created_at DESC LIMIT ?", (tenant_id, provider, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM provider_evaluation_runs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?", (tenant_id, limit)).fetchall()
        return [{**dict(row), "report": json.loads(row["report_json"]), "alerts": self._evaluation_drift_alerts(row, tenant_id)} for row in rows]

    def _evaluation_drift_alerts(self, row, tenant_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            previous = db.execute("SELECT * FROM provider_evaluation_runs WHERE tenant_id=? AND provider=? AND dataset_fingerprint=? AND created_at<? ORDER BY created_at DESC LIMIT 1",
                                  (tenant_id, row["provider"], row["dataset_fingerprint"], row["created_at"])).fetchone()
        if not previous:
            return []
        alerts = []
        for metric in ("exact_accuracy", "evidence_support_accuracy", "clause_accuracy", "amendment_comparison_accuracy"):
            old, new = previous[metric], row[metric]
            if old is not None and new is not None and old - new >= 0.10 - 1e-9:
                alerts.append({"metric": metric, "previous": old, "current": new, "drop": round(old-new, 4)})
        return alerts

    def save_provider_evaluation_schedule(self, tenant_id: str, config: dict[str, Any], actor: str) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO provider_evaluation_schedules (tenant_id,enabled,interval_hours,run_on_provider_change,alert_emails_json,updated_by,updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET enabled=excluded.enabled,interval_hours=excluded.interval_hours,"
                "run_on_provider_change=excluded.run_on_provider_change,alert_emails_json=excluded.alert_emails_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (tenant_id, int(config["enabled"]), config["interval_hours"], int(config["run_on_provider_change"]),
                 json.dumps(config.get("alert_emails", [])), actor, now),
            )
        return self.get_provider_evaluation_schedule(tenant_id) or {}

    def get_provider_evaluation_schedule(self, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM provider_evaluation_schedules WHERE tenant_id=?", (tenant_id,)).fetchone()
        if not row:
            return None
        return {**dict(row), "enabled": bool(row["enabled"]), "run_on_provider_change": bool(row["run_on_provider_change"]),
                "alert_emails": json.loads(row["alert_emails_json"])}

    def provider_evaluation_schedule_tenants(self) -> list[str]:
        with self.connect() as db:
            rows = db.execute("SELECT tenant_id FROM provider_evaluation_schedules WHERE enabled=1 ORDER BY tenant_id").fetchall()
        return [row["tenant_id"] for row in rows]

    def provider_evaluation_due(self, tenant_id: str, signature: str, now: datetime | None = None) -> tuple[bool, dict[str, Any] | None]:
        schedule = self.get_provider_evaluation_schedule(tenant_id)
        if not schedule or not schedule["enabled"]:
            return False, schedule
        current = now or utc_now()
        changed = bool(schedule["run_on_provider_change"] and schedule["provider_signature"] and schedule["provider_signature"] != signature)
        due = not schedule["last_run_at"]
        if schedule["last_run_at"]:
            last = datetime.fromisoformat(schedule["last_run_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            due = current - last >= timedelta(hours=schedule["interval_hours"])
        return changed or due, schedule

    def complete_provider_evaluation_schedule(self, tenant_id: str, signature: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE provider_evaluation_schedules SET last_run_at=?,provider_signature=? WHERE tenant_id=?",
                       (utc_now().isoformat(), signature, tenant_id))

    def record_evaluation_alert_delivery(self, tenant_id: str, run_id: str, provider: str, metric: str,
                                        recipient: str | None, channel: str, status: str,
                                        duration_ms: int, error_message: str | None = None) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO evaluation_alert_deliveries (tenant_id,run_id,provider,metric,recipient,channel,status,duration_ms,error_message,attempted_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)", (tenant_id,run_id,provider,metric,recipient,channel,status,duration_ms,error_message,utc_now().isoformat()))

    def evaluation_alert_delivery_history(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM evaluation_alert_deliveries WHERE tenant_id=? ORDER BY attempted_at DESC LIMIT ?", (tenant_id,limit)).fetchall()
        return [dict(row) for row in rows]

    def save_portfolio_filter(self, tenant_id: str, name: str, dataset: str, filters: dict[str, Any], actor: str) -> dict[str, Any]:
        now, filter_id = utc_now().isoformat(), str(uuid4())
        try:
            with self.connect() as db:
                db.execute("INSERT INTO portfolio_saved_filters (id,tenant_id,name,dataset,filters_json,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                           (filter_id,tenant_id,name,dataset,json.dumps(filters),actor,now,now))
                row = db.execute("SELECT * FROM portfolio_saved_filters WHERE id=?",(filter_id,)).fetchone()
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise ValueError("A saved filter with this name already exists") from exc
            raise
        return self._portfolio_filter_record(row)

    @staticmethod
    def _portfolio_filter_record(row) -> dict[str, Any]:
        return {**dict(row), "filters": json.loads(row["filters_json"])}

    def list_portfolio_filters(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM portfolio_saved_filters WHERE tenant_id=? ORDER BY name",(tenant_id,)).fetchall()
        return [self._portfolio_filter_record(row) for row in rows]

    def get_portfolio_filter(self, filter_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM portfolio_saved_filters WHERE id=? AND tenant_id=?",(filter_id,tenant_id)).fetchone()
        return self._portfolio_filter_record(row) if row else None

    def delete_portfolio_filter(self, filter_id: str, tenant_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM portfolio_saved_filters WHERE id=? AND tenant_id=?",(filter_id,tenant_id))
        return cursor.rowcount > 0

    def create_portfolio_import_batch(self, tenant_id: str, dataset: str, filename: str, mapping: dict,
                                      rows: list[dict], report: dict, actor: str) -> dict[str, Any]:
        batch_id, now = str(uuid4()), utc_now().isoformat()
        with self.connect() as db:
            db.execute("INSERT INTO portfolio_import_batches (id,tenant_id,dataset,filename,field_mapping_json,rows_json,report_json,status,created_by,created_at) "
                       "VALUES (?,?,?,?,?,?,?,'previewed',?,?)",
                       (batch_id,tenant_id,dataset,filename,json.dumps(mapping),json.dumps(rows,default=str),json.dumps(report),actor,now))
        return self.get_portfolio_import_batch(batch_id, tenant_id)

    def get_portfolio_import_batch(self, batch_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM portfolio_import_batches WHERE id=? AND tenant_id=?",(batch_id,tenant_id)).fetchone()
            if not row:
                return None
            events = db.execute("SELECT * FROM portfolio_import_events WHERE batch_id=? AND tenant_id=? ORDER BY row_number,id",(batch_id,tenant_id)).fetchall()
        return {**dict(row), "field_mapping": json.loads(row["field_mapping_json"]),
                "rows": json.loads(row["rows_json"]), "report": json.loads(row["report_json"]),
                "events": [dict(event) for event in events]}

    def record_portfolio_import_event(self, batch_id: str, tenant_id: str, row_number: int, dataset: str,
                                     action: str, status: str, record_id: str | None, message: str | None,
                                     actor: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO portfolio_import_events (batch_id,tenant_id,row_number,dataset,action,status,record_id,message,actor,created_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",(batch_id,tenant_id,row_number,dataset,action,status,record_id,message,actor,utc_now().isoformat()))

    def finish_portfolio_import_batch(self, batch_id: str, tenant_id: str, report: dict, status: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE portfolio_import_batches SET report_json=?,status=?,committed_at=? WHERE id=? AND tenant_id=?",
                       (json.dumps(report),status,utc_now().isoformat(),batch_id,tenant_id))

    def latest_portfolio_import_batches(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT id,tenant_id,dataset,filename,status,report_json,created_by,created_at,committed_at "
                               "FROM portfolio_import_batches WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",(tenant_id,limit)).fetchall()
        return [{**dict(row), "report": json.loads(row["report_json"])} for row in rows]

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

    def create_renewal_decision(self, request: dict[str, Any], actor: str, tenant_id: str) -> dict[str, Any] | None:
        decision_id, now = str(uuid4()), utc_now().isoformat()
        with self.connect() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?",(request["task_id"],tenant_id)).fetchone()
            if not task:
                return None
            if task["status"] != "open" or task["task_kind"] != "renewal":
                raise ValueError("Renewal decisions require an open renewal task")
            if db.execute("SELECT 1 FROM renewal_decisions WHERE task_id=?",(task["id"],)).fetchone():
                raise ValueError("A renewal decision already exists for this task")
            forecast_id = request.get("forecast_id")
            if forecast_id:
                linked = db.execute("SELECT 1 FROM renewal_forecast_lines WHERE run_id=? AND contract_id=? AND tenant_id=?",
                                    (forecast_id,task["contract_id"],tenant_id)).fetchone()
                if not linked:
                    raise ValueError("Forecast must be a saved tenant forecast that includes this contract")
            db.execute("INSERT INTO renewal_decisions (id,tenant_id,contract_id,task_id,forecast_run_id,strategy,status,reason,owner_name,owner_email,target_date,created_by,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,'draft',?,?,?,?,?,?,?)",
                       (decision_id,tenant_id,task["contract_id"],task["id"],forecast_id,request["strategy"],request["reason"],
                        request.get("owner_name"),request.get("owner_email"),iso(request.get("target_date")),actor,now,now))
            self._renewal_decision_event(db,tenant_id,decision_id,"created",{key:value for key,value in request.items() if key!="task_id"},actor,now)
            self.add_audit(db,task["contract_id"],"renewal.decision_created",actor,{"decision_id":decision_id,"task_id":task["id"],"strategy":request["strategy"],"forecast_id":forecast_id},tenant_id)
        return self.get_renewal_decision(decision_id,tenant_id)

    def create_renewal_scenario_plan(self, task_id: str, tenant_id: str, actor: str,
                                     assumptions: dict[str, Any]) -> dict[str, Any] | None:
        """Save a deterministic, auditable comparison of renewal strategies for one task."""
        as_of = assumptions.get("as_of_date") or date.today()
        if isinstance(as_of, str):
            as_of = date.fromisoformat(as_of)
        horizon_months = int(assumptions.get("horizon_months", 12))
        end_month = as_of.month - 1 + horizon_months
        end_year = as_of.year + end_month // 12
        end_month = end_month % 12 + 1
        horizon_end = date(end_year, end_month, min(as_of.day, calendar.monthrange(end_year, end_month)[1])) - timedelta(days=1)
        increase = Decimal(str(assumptions.get("renewal_increase_pct", 0))) / Decimal(100)
        savings = Decimal(str(assumptions.get("renegotiation_savings_pct", 10))) / Decimal(100)
        transition = Decimal(str(assumptions.get("termination_transition_cost", 0)))
        plan_id, now = str(uuid4()), utc_now().isoformat()
        with self.connect() as db:
            row = db.execute(
                "SELECT t.id AS task_id,t.contract_id,t.due_date,t.expiration_date,c.filename,c.confirmed_json,c.extracted_json,"
                "c.supplier_id,s.name AS supplier_name,s.risk_level FROM tasks t JOIN contracts c ON c.id=t.contract_id "
                "LEFT JOIN suppliers s ON s.id=c.supplier_id AND s.tenant_id=c.tenant_id "
                "WHERE t.id=? AND t.tenant_id=? AND t.task_kind='renewal'", (task_id, tenant_id),
            ).fetchone()
            if not row:
                return None
            terms = json.loads(row["confirmed_json"] or row["extracted_json"] or "{}")
            annual_value = Decimal(str(terms["annual_contract_value"])) if terms.get("annual_contract_value") is not None else None
            currency = terms.get("currency")
            contract_start = date.fromisoformat(terms["start_date"]) if terms.get("start_date") else as_of
            expiration = date.fromisoformat(terms["expiration_date"]) if terms.get("expiration_date") else date.fromisoformat(row["expiration_date"])
            forecast_id = assumptions.get("forecast_id")
            forecast_value = None
            if forecast_id:
                forecast = db.execute(
                    "SELECT r.id,l.annual_contract_value,l.currency FROM renewal_forecast_runs r "
                    "JOIN renewal_forecast_lines l ON l.run_id=r.id AND l.tenant_id=r.tenant_id "
                    "WHERE r.id=? AND r.tenant_id=? AND l.contract_id=?",
                    (forecast_id, tenant_id, row["contract_id"]),
                ).fetchone()
                if not forecast:
                    raise ValueError("Forecast must be a saved tenant forecast that includes this contract")
                forecast_value = {"annual_contract_value": forecast["annual_contract_value"], "currency": forecast["currency"]}
            history = db.execute(
                "SELECT r.id,r.period_start,r.period_end,l.annual_contract_value,l.currency,r.renewal_increase_pct,"
                "r.renegotiation_savings_pct FROM renewal_forecast_runs r JOIN renewal_forecast_lines l "
                "ON l.run_id=r.id AND l.tenant_id=r.tenant_id WHERE r.tenant_id=? AND l.contract_id=? "
                "ORDER BY r.recorded_at DESC LIMIT 10", (tenant_id, row["contract_id"]),
            ).fetchall()
            outcomes = db.execute(
                "SELECT outcome,actual_annual_value,annual_savings,currency,recorded_at FROM renewal_outcomes "
                "WHERE tenant_id=? AND task_id IN (SELECT id FROM tasks WHERE contract_id=? AND tenant_id=?) "
                "ORDER BY recorded_at DESC LIMIT 10", (tenant_id, row["contract_id"], tenant_id),
            ).fetchall()
            decision = db.execute("SELECT id FROM renewal_decisions WHERE task_id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            milestones = db.execute(
                "SELECT title,due_date,completed FROM renewal_decision_milestones WHERE decision_id=? AND tenant_id=? ORDER BY due_date",
                (decision["id"], tenant_id),
            ).fetchall() if decision else []
            baseline_cost = Decimal(0)
            if annual_value is not None:
                current_end = min(expiration, horizon_end)
                current_start = max(as_of, contract_start)
                if current_end >= current_start:
                    current_days = (current_end - current_start).days + 1
                    current_year_days = 366 if calendar.isleap(current_start.year) else 365
                    baseline_cost = annual_value * Decimal(current_days) / Decimal(current_year_days)
            renewal_start = max(as_of, expiration + timedelta(days=1))
            tail_cost = Decimal(0)
            if annual_value is not None and renewal_start <= horizon_end:
                tail_days = (horizon_end - renewal_start).days + 1
                tail_cost = annual_value * Decimal(tail_days) / Decimal(366 if calendar.isleap(renewal_start.year) else 365)
            scenario_costs = {
                "renew": baseline_cost + tail_cost * (Decimal(1) + increase),
                "renegotiate": baseline_cost + tail_cost * (Decimal(1) - savings),
                "terminate": baseline_cost + transition,
            }
            risk = row["risk_level"] or "unrated"
            risk_implications = {
                "renew": f"Continues supplier dependency at current risk rating ({risk}).",
                "renegotiate": f"Continues supplier dependency at current risk rating ({risk}) while seeking lower spend.",
                "terminate": f"Reduces supplier dependency; validate transition and continuity risks (current supplier rating: {risk}).",
            }
            deadline = date.fromisoformat(row["due_date"])
            schedule = [{"title": item["title"], "due_date": item["due_date"], "completed": bool(item["completed"])} for item in milestones]
            late_milestones = [item["title"] for item in schedule if item["due_date"] and date.fromisoformat(item["due_date"]) > deadline and not item["completed"]]
            results = {
                "contract_id": row["contract_id"], "contract": terms.get("contract") or row["filename"],
                "supplier": row["supplier_name"], "supplier_risk": risk, "currency": currency,
                "annual_contract_value": str(annual_value) if annual_value is not None else None,
                "start_date": contract_start.isoformat(), "as_of_date": as_of.isoformat(), "horizon_end": horizon_end.isoformat(),
                "expiration_date": expiration.isoformat(), "notice_deadline": deadline.isoformat(),
                "scenarios": {key: {"projected_cost": (str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if annual_value is not None else None),
                                    "cost_complete": annual_value is not None,
                                    "transition_cost": (str(transition.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if key == "terminate" else "0.00"),
                                    "risk_implication": risk_implications[key]} for key, value in scenario_costs.items()},
                "inputs": {"baseline_current_term_cost": str(baseline_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                           "post_expiration_baseline_cost": str(tail_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                           "forecast": forecast_value,
                           "forecast_history": [dict(item) for item in history],
                           "recorded_outcomes": [dict(item) for item in outcomes],
                           "milestones": schedule,
                           "milestone_warnings": ([f"Uncompleted milestone(s) fall after notice deadline: {', '.join(late_milestones)}"] if late_milestones else [])},
                "method": "Calendar-day proration over an inclusive planning horizon; renewal increase and renegotiation savings apply after expiration; termination adds transition cost (assumed in the contract currency) and ends post-expiration contract spend. Projected cost is incomplete when annual contract value is missing.",
            }
            stored_assumptions = {**assumptions, "as_of_date": as_of.isoformat(),
                                  "renewal_increase_pct": str(assumptions.get("renewal_increase_pct", 0)),
                                  "renegotiation_savings_pct": str(assumptions.get("renegotiation_savings_pct", 10)),
                                  "termination_transition_cost": str(transition), "horizon_months": horizon_months}
            db.execute("INSERT INTO renewal_scenario_plans (id,tenant_id,contract_id,task_id,forecast_run_id,assumptions_json,results_json,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (plan_id,tenant_id,row["contract_id"],task_id,forecast_id,json.dumps(stored_assumptions),json.dumps(results,default=str),actor,now))
            self.add_audit(db,row["contract_id"],"renewal.scenario_plan_created",actor,{"plan_id":plan_id,"task_id":task_id,"forecast_id":forecast_id,"assumptions":stored_assumptions},tenant_id)
            self._governance_event(db,tenant_id,"renewal.scenario_plan_created","renewal_scenario_plan",plan_id,actor,{"contract_id":row["contract_id"],"task_id":task_id},now)
        return self.get_renewal_scenario_plan(plan_id, tenant_id)

    @staticmethod
    def _scenario_plan_record(row) -> dict[str, Any]:
        record = dict(row)
        record["assumptions"] = json.loads(record.pop("assumptions_json"))
        record["results"] = json.loads(record.pop("results_json"))
        return record

    def get_renewal_scenario_plan(self, plan_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM renewal_scenario_plans WHERE id=? AND tenant_id=?", (plan_id,tenant_id)).fetchone()
        return self._scenario_plan_record(row) if row else None

    def list_renewal_scenario_plans(self, task_id: str, tenant_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM renewal_scenario_plans WHERE task_id=? AND tenant_id=? ORDER BY created_at DESC",(task_id,tenant_id)).fetchall()
        return [self._scenario_plan_record(row) for row in rows]

    @staticmethod
    def _renewal_decision_event(db, tenant_id: str, decision_id: str, event_type: str,
                                details: dict[str, Any], actor: str, now: str) -> None:
        db.execute("INSERT INTO renewal_decision_events (tenant_id,decision_id,event_type,details_json,actor,created_at) VALUES (?,?,?,?,?,?)",
                   (tenant_id,decision_id,event_type,json.dumps(details,default=str),actor,now))

    def _renewal_decision_record(self, row, tenant_id: str) -> dict[str, Any]:
        decision = dict(row)
        with self.connect() as db:
            milestones = db.execute("SELECT * FROM renewal_decision_milestones WHERE decision_id=? AND tenant_id=? ORDER BY due_date,created_at",
                                    (row["id"],tenant_id)).fetchall()
            quotes = db.execute("SELECT * FROM renewal_decision_quotes WHERE decision_id=? AND tenant_id=? ORDER BY created_at DESC",
                                (row["id"],tenant_id)).fetchall()
            events = db.execute("SELECT * FROM renewal_decision_events WHERE decision_id=? AND tenant_id=? ORDER BY id",
                                (row["id"],tenant_id)).fetchall()
            task = db.execute("SELECT title,due_date,expiration_date FROM tasks WHERE id=? AND tenant_id=?",(row["task_id"],tenant_id)).fetchone()
        decision["task"] = dict(task) if task else None
        decision["milestones"] = [{**dict(item),"completed":bool(item["completed"])} for item in milestones]
        decision["quotes"] = [dict(item) for item in quotes]
        decision["events"] = [{**dict(item),"details":json.loads(item["details_json"])} for item in events]
        return decision

    def list_renewal_decisions(self, tenant_id: str, status: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            if status:
                rows = db.execute("SELECT * FROM renewal_decisions WHERE tenant_id=? AND status=? ORDER BY updated_at DESC",(tenant_id,status)).fetchall()
            else:
                rows = db.execute("SELECT * FROM renewal_decisions WHERE tenant_id=? ORDER BY updated_at DESC",(tenant_id,)).fetchall()
        return [self._renewal_decision_record(row,tenant_id) for row in rows]

    def get_renewal_decision(self, decision_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM renewal_decisions WHERE id=? AND tenant_id=?",(decision_id,tenant_id)).fetchone()
        return self._renewal_decision_record(row,tenant_id) if row else None

    def update_renewal_decision(self, decision_id: str, tenant_id: str, actor: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        now = utc_now().isoformat()
        with self.connect() as db:
            row = db.execute("SELECT * FROM renewal_decisions WHERE id=? AND tenant_id=?",(decision_id,tenant_id)).fetchone()
            if not row:
                return None
            if row["status"] not in {"draft","in_progress","rejected"}:
                raise ValueError("Only draft or active decisions can be edited")
            forecast_id = changes.get("forecast_id",row["forecast_run_id"])
            if forecast_id:
                linked = db.execute("SELECT 1 FROM renewal_forecast_lines WHERE run_id=? AND contract_id=? AND tenant_id=?",
                                    (forecast_id,row["contract_id"],tenant_id)).fetchone()
                if not linked:
                    raise ValueError("Forecast must be a saved tenant forecast that includes this contract")
            assignments, params = [], []
            for key,value in changes.items():
                column = "forecast_run_id" if key == "forecast_id" else key
                if column not in {"strategy","reason","forecast_run_id","owner_name","owner_email","target_date"}:
                    continue
                assignments.append(f"{column}=?")
                params.append(iso(value) if key == "target_date" else value)
            if assignments:
                assignments.extend(["status='in_progress'","updated_at=?"])
                params.extend([now,decision_id,tenant_id])
                db.execute(f"UPDATE renewal_decisions SET {','.join(assignments)} WHERE id=? AND tenant_id=?",tuple(params))
                self._renewal_decision_event(db,tenant_id,decision_id,"updated",changes,actor,now)
                self.add_audit(db,row["contract_id"],"renewal.decision_updated",actor,{"decision_id":decision_id,"changes":changes},tenant_id)
        return self.get_renewal_decision(decision_id,tenant_id)

    def add_renewal_decision_milestone(self, decision_id: str, tenant_id: str, actor: str, request: dict[str, Any]) -> dict[str, Any] | None:
        milestone_id,now=str(uuid4()),utc_now().isoformat()
        with self.connect() as db:
            decision=db.execute("SELECT * FROM renewal_decisions WHERE id=? AND tenant_id=?",(decision_id,tenant_id)).fetchone()
            if not decision: return None
            if decision["status"] in {"approved","rejected","completed"}:
                raise ValueError("Milestones cannot be changed on a terminal decision")
            db.execute("INSERT INTO renewal_decision_milestones (id,tenant_id,decision_id,title,due_date,owner_email,created_by,created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (milestone_id,tenant_id,decision_id,request["title"],iso(request.get("due_date")),request.get("owner_email"),actor,now))
            self._renewal_decision_event(db,tenant_id,decision_id,"milestone_added",{"milestone_id":milestone_id,**request},actor,now)
            self.add_audit(db,decision["contract_id"],"renewal.milestone_added",actor,{"decision_id":decision_id,"milestone_id":milestone_id},tenant_id)
            row=db.execute("SELECT * FROM renewal_decision_milestones WHERE id=?",(milestone_id,)).fetchone()
        return {**dict(row),"completed":bool(row["completed"])}

    def update_renewal_decision_milestone(self, decision_id: str, milestone_id: str, tenant_id: str,
                                          actor: str, completed: bool, comment: str | None) -> dict[str, Any] | None:
        now=utc_now().isoformat()
        with self.connect() as db:
            row=db.execute("SELECT m.*,d.contract_id,d.status AS decision_status FROM renewal_decision_milestones m JOIN renewal_decisions d ON d.id=m.decision_id "
                           "WHERE m.id=? AND m.decision_id=? AND m.tenant_id=?",(milestone_id,decision_id,tenant_id)).fetchone()
            if not row: return None
            if row["decision_status"] in {"approved","rejected","completed"}:
                raise ValueError("Milestones cannot be changed on a terminal decision")
            db.execute("UPDATE renewal_decision_milestones SET completed=?,completed_by=?,completed_at=?,comment=? WHERE id=? AND tenant_id=?",
                       (int(completed),actor if completed else None,now if completed else None,comment,milestone_id,tenant_id))
            self._renewal_decision_event(db,tenant_id,decision_id,"milestone_completed" if completed else "milestone_reopened",
                                        {"milestone_id":milestone_id,"comment":comment},actor,now)
            self.add_audit(db,row["contract_id"],"renewal.milestone_updated",actor,{"decision_id":decision_id,"milestone_id":milestone_id,"completed":completed},tenant_id)
            updated=db.execute("SELECT * FROM renewal_decision_milestones WHERE id=?",(milestone_id,)).fetchone()
        return {**dict(updated),"completed":bool(updated["completed"])}

    def add_renewal_decision_quote(self, decision_id: str, tenant_id: str, actor: str, request: dict[str, Any]) -> dict[str, Any] | None:
        quote_id,now=str(uuid4()),utc_now().isoformat()
        with self.connect() as db:
            decision=db.execute("SELECT * FROM renewal_decisions WHERE id=? AND tenant_id=?",(decision_id,tenant_id)).fetchone()
            if not decision: return None
            if decision["status"] in {"approved","rejected","completed"}:
                raise ValueError("Quotes cannot be added to a terminal decision")
            db.execute("INSERT INTO renewal_decision_quotes (id,tenant_id,decision_id,supplier,amount,currency,quote_date,valid_until,reference,note,created_by,created_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (quote_id,tenant_id,decision_id,request.get("supplier"),str(request["amount"]),request["currency"],iso(request.get("quote_date")),
                        iso(request.get("valid_until")),request.get("reference"),request.get("note"),actor,now))
            self._renewal_decision_event(db,tenant_id,decision_id,"quote_added",{"quote_id":quote_id,"amount":str(request["amount"]),"currency":request["currency"]},actor,now)
            self.add_audit(db,decision["contract_id"],"renewal.quote_added",actor,{"decision_id":decision_id,"quote_id":quote_id,"amount":str(request["amount"]),"currency":request["currency"]},tenant_id)
            row=db.execute("SELECT * FROM renewal_decision_quotes WHERE id=?",(quote_id,)).fetchone()
        return dict(row)

    def submit_renewal_decision(self, decision_id: str, tenant_id: str, actor: str) -> dict[str, Any] | None:
        now=utc_now().isoformat()
        with self.connect() as db:
            row=db.execute("SELECT * FROM renewal_decisions WHERE id=? AND tenant_id=?",(decision_id,tenant_id)).fetchone()
            if not row: return None
            if row["status"] not in {"draft","in_progress","rejected"}:
                raise ValueError("Only draft or active decisions can be submitted for review")
            db.execute("UPDATE renewal_decisions SET status='pending_approval',updated_at=? WHERE id=? AND tenant_id=?",(now,decision_id,tenant_id))
            self._renewal_decision_event(db,tenant_id,decision_id,"submitted",{},actor,now)
            self.add_audit(db,row["contract_id"],"renewal.decision_submitted",actor,{"decision_id":decision_id},tenant_id)
        return self.get_renewal_decision(decision_id,tenant_id)

    def review_renewal_decision(self, decision_id: str, tenant_id: str, actor: str,
                                approved: bool, comment: str | None) -> dict[str, Any] | None:
        now=utc_now().isoformat()
        with self.connect() as db:
            row=db.execute("SELECT * FROM renewal_decisions WHERE id=? AND tenant_id=?",(decision_id,tenant_id)).fetchone()
            if not row: return None
            if row["status"] != "pending_approval":
                raise ValueError("Only a decision pending approval can be reviewed")
            status="approved" if approved else "rejected"
            db.execute("UPDATE renewal_decisions SET status=?,reviewed_by=?,reviewed_at=?,review_comment=?,updated_at=? WHERE id=? AND tenant_id=?",
                       (status,actor,now,comment,now,decision_id,tenant_id))
            self._renewal_decision_event(db,tenant_id,decision_id,"approved" if approved else "rejected",{"comment":comment},actor,now)
            self.add_audit(db,row["contract_id"],f"renewal.decision_{status}",actor,{"decision_id":decision_id,"comment":comment},tenant_id)
        return self.get_renewal_decision(decision_id,tenant_id)

    def complete_renewal_decision(self, decision_id: str, tenant_id: str, request: dict[str, Any], actor: str) -> dict[str, Any] | None:
        now=utc_now().isoformat()
        with self.connect() as db:
            decision=db.execute("SELECT * FROM renewal_decisions WHERE id=? AND tenant_id=?",(decision_id,tenant_id)).fetchone()
            if not decision: return None
            if decision["status"] != "approved":
                raise ValueError("Decision must be approved before it can be completed")
            if decision["strategy"] == "renew" and request["outcome"] != "renewed":
                raise ValueError("A renew decision must complete with a renewed outcome")
            if decision["strategy"] == "negotiate" and request["outcome"] != "renegotiated":
                raise ValueError("A negotiate decision must complete with a renegotiated outcome")
            if decision["strategy"] == "terminate" and request["outcome"] not in {"terminated","expired","no_action"}:
                raise ValueError("A terminate decision must complete with a terminated, expired, or no-action outcome")
            task=db.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?",(decision["task_id"],tenant_id)).fetchone()
            if not task or task["status"]!="open": raise ValueError("The renewal task is no longer open")
            if db.execute("SELECT 1 FROM renewal_outcomes WHERE task_id=?",(task["id"],)).fetchone():
                raise ValueError("A renewal outcome has already been recorded for this task")
            successor=request.get("successor_contract_id")
            if successor and not db.execute("SELECT 1 FROM contracts WHERE id=? AND tenant_id=?",(successor,tenant_id)).fetchone():
                raise ValueError("Successor contract must belong to this organization")
            workflow="renewed" if request["outcome"] in {"renewed","renegotiated"} else "cancelled" if request["outcome"]=="no_action" else "terminated"
            db.execute("INSERT INTO renewal_outcomes (task_id,tenant_id,contract_id,outcome,actual_annual_value,annual_savings,currency,successor_contract_id,actual_expiration_date,notes,recorded_by,recorded_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (task["id"],tenant_id,decision["contract_id"],request["outcome"],str(request["actual_annual_value"]) if request.get("actual_annual_value") is not None else None,
                        str(request["annual_savings"]) if request.get("annual_savings") is not None else None,request.get("currency"),successor,
                        iso(request.get("actual_expiration_date")),request.get("notes"),actor,now))
            db.execute("UPDATE tasks SET status='resolved',workflow_state=? WHERE id=? AND tenant_id=?",(workflow,task["id"],tenant_id))
            db.execute("UPDATE renewal_decisions SET status='completed',updated_at=? WHERE id=? AND tenant_id=?",(now,decision_id,tenant_id))
            details={key:value for key,value in request.items() if key!="notes"}
            details.update({"task_id":task["id"],"decision_id":decision_id,"forecast_id":decision["forecast_run_id"]})
            self._renewal_decision_event(db,tenant_id,decision_id,"completed",details,actor,now)
            self.add_audit(db,decision["contract_id"],"renewal.outcome_recorded",actor,details,tenant_id)
        with self.connect() as db:
            row=db.execute("SELECT * FROM renewal_outcomes WHERE task_id=? AND tenant_id=?",(decision["task_id"],tenant_id)).fetchone()
        return dict(row)

    def record_renewal_outcome(self, task_id: str, request: dict[str, Any], actor: str, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            if not task:
                return None
            if db.execute("SELECT 1 FROM renewal_outcomes WHERE task_id=?", (task_id,)).fetchone():
                raise ValueError("A renewal outcome has already been recorded for this task")
            if task["status"] != "open":
                raise ValueError("Only open tasks can have a renewal outcome recorded")
            decision = db.execute("SELECT * FROM renewal_decisions WHERE task_id=? AND tenant_id=?",(task_id,tenant_id)).fetchone()
            if decision:
                if decision["status"] != "approved":
                    raise ValueError("Renewal decision must be approved before an outcome can be recorded")
                strategy_outcomes = {"renew":{"renewed"},"negotiate":{"renegotiated"},
                                     "terminate":{"terminated","expired","no_action"}}
                if request["outcome"] not in strategy_outcomes[decision["strategy"]]:
                    raise ValueError("Recorded outcome does not match the approved renewal decision")
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
            if decision:
                db.execute("UPDATE renewal_decisions SET status='completed',updated_at=? WHERE id=? AND tenant_id=?",
                           (now,decision["id"],tenant_id))
                self._renewal_decision_event(db,tenant_id,decision["id"],"completed",{
                    "outcome":outcome,"task_id":task_id,"forecast_id":decision["forecast_run_id"]},actor,now)
            details = {key: value for key, value in request.items() if key != "notes"}
            details["task_id"] = task_id
            self.add_audit(db, task["contract_id"], "renewal.outcome_recorded", actor, details, tenant_id)
            return dict(db.execute("SELECT * FROM renewal_outcomes WHERE task_id=?", (task_id,)).fetchone())

    def record_renewal_forecast(self, tenant_id: str, forecast: dict[str, Any], actor: str) -> str:
        """Persist a reviewer-generated forecast so later outcomes can be scored against it."""
        run_id, recorded_at = str(uuid4()), utc_now().isoformat()
        period = forecast["period"]
        assumptions = forecast["assumptions"]
        with self.connect() as db:
            db.execute(
                "INSERT INTO renewal_forecast_runs "
                "(id,tenant_id,period_start,period_end,renewal_increase_pct,renegotiation_savings_pct,recorded_by,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_id, tenant_id, period["start_date"], period["end_date"],
                 str(assumptions["renewal_increase_pct"]), str(assumptions["renegotiation_savings_pct"]), actor, recorded_at),
            )
            for item in forecast["contracts"]:
                if item.get("annual_contract_value") is None:
                    continue
                db.execute(
                    "INSERT INTO renewal_forecast_lines (run_id,tenant_id,contract_id,supplier,currency,annual_contract_value) "
                    "VALUES (?,?,?,?,?,?)",
                    (run_id, tenant_id, item["contract_id"], item.get("supplier"), item.get("currency"), item["annual_contract_value"]),
                )
        return run_id

    def list_renewal_forecast_runs(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows=db.execute("SELECT r.*,COUNT(l.contract_id) AS contract_count FROM renewal_forecast_runs r "
                            "LEFT JOIN renewal_forecast_lines l ON l.run_id=r.id AND l.tenant_id=r.tenant_id "
                            "WHERE r.tenant_id=? GROUP BY r.id ORDER BY r.recorded_at DESC",(tenant_id,)).fetchall()
        return [dict(row) for row in rows]

    def renewal_forecast_accuracy(self, tenant_id: str) -> dict[str, Any]:
        """Compare the most recent pre-outcome forecast with actual annualized decisions."""
        with self.connect() as db:
            outcomes = db.execute(
                "SELECT o.*,c.confirmed_json,c.extracted_json,d.forecast_run_id AS decision_forecast_id FROM renewal_outcomes o "
                "JOIN contracts c ON c.id=o.contract_id AND c.tenant_id=o.tenant_id "
                "LEFT JOIN renewal_decisions d ON d.task_id=o.task_id AND d.tenant_id=o.tenant_id "
                "WHERE o.tenant_id=? ORDER BY o.recorded_at", (tenant_id,),
            ).fetchall()
            runs = db.execute(
                "SELECT r.*,l.contract_id,l.supplier,l.currency,l.annual_contract_value "
                "FROM renewal_forecast_runs r JOIN renewal_forecast_lines l ON l.run_id=r.id "
                "WHERE r.tenant_id=? ORDER BY r.recorded_at", (tenant_id,),
            ).fetchall()
        groups: dict[tuple[str | None, str, str], list[tuple[Decimal, Decimal]]] = {}
        increases: list[Decimal] = []
        savings: list[Decimal] = []
        scored = 0
        for outcome in outcomes:
            candidates = [r for r in runs if r["contract_id"] == outcome["contract_id"] and r["recorded_at"] <= outcome["recorded_at"]]
            if outcome["decision_forecast_id"]:
                candidates = [r for r in candidates if r["id"] == outcome["decision_forecast_id"]]
            if not candidates:
                continue
            run = candidates[-1]
            if outcome["actual_annual_value"] is None and outcome["outcome"] not in {"terminated", "expired"}:
                continue
            terms = json.loads(outcome["confirmed_json"] or outcome["extracted_json"] or "{}")
            baseline = Decimal(str(run["annual_contract_value"]))
            actual = Decimal(str(outcome["actual_annual_value"] or 0))
            currency = outcome["currency"] or run["currency"] or terms.get("currency") or ""
            supplier = run["supplier"]
            predictions = {
                "renew": baseline * (Decimal(1) + Decimal(run["renewal_increase_pct"]) / Decimal(100)),
                "renegotiate": baseline * (Decimal(1) - Decimal(run["renegotiation_savings_pct"]) / Decimal(100)),
                "terminate": Decimal(0),
            }
            for scenario, predicted in predictions.items():
                groups.setdefault((supplier, scenario, currency), []).append((predicted, actual))
            scored += 1
            if baseline > 0 and outcome["actual_annual_value"] is not None:
                ratio = (Decimal(str(outcome["actual_annual_value"])) / baseline - 1) * 100
                if outcome["outcome"] == "renewed":
                    increases.append(ratio)
                elif outcome["outcome"] == "renegotiated":
                    savings.append(-ratio)
        metrics = []
        for (supplier, scenario, currency), values in sorted(groups.items(), key=lambda x: (x[0][0] or "", x[0][1], x[0][2])):
            variances = [predicted - actual for predicted, actual in values]
            metrics.append({
                "supplier": supplier, "scenario": scenario, "currency": currency,
                "sample_count": len(values), "mean_predicted_annual_value": _money_string(sum((x[0] for x in values), Decimal(0)) / len(values)),
                "mean_actual_annual_value": _money_string(sum((x[1] for x in values), Decimal(0)) / len(values)),
                "mean_variance": _money_string(sum(variances, Decimal(0)) / len(variances)),
                "mean_absolute_variance": _money_string(sum((abs(x) for x in variances), Decimal(0)) / len(variances)),
            })
        return {
            "outcome_count": scored,
            "by_supplier_and_scenario": metrics,
            "recommended_assumptions": {
                "renewal_increase_pct": _percent_string(median(increases)) if increases else None,
                "renegotiation_savings_pct": _percent_string(median(savings)) if savings else None,
                "renewal_samples": len(increases), "renegotiation_samples": len(savings),
            },
        }

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

    def portfolio_performance(
        self, tenant_id: str, start_date: date | None = None, end_date: date | None = None,
        supplier_id: str | None = None, owner_email: str | None = None,
        sla_days: int = 3, as_of: date | None = None,
    ) -> dict[str, Any]:
        """Aggregate renewal, obligation, and approval performance for a tenant portfolio."""
        filters = ["o.tenant_id=?"]
        params: list[Any] = [tenant_id]
        if supplier_id:
            filters.append("c.supplier_id=?")
            params.append(supplier_id)
        if owner_email:
            filters.append("lower(COALESCE(t.owner_email,''))=?")
            params.append(owner_email.strip().casefold())
        date_sql, date_params = "", []
        if start_date:
            date_sql += " AND {event_date}>=?"
            date_params.append(start_date.isoformat())
        if end_date:
            date_sql += " AND {event_date}<=?"
            date_params.append(end_date.isoformat() + "T23:59:59.999999+00:00")
        as_of = as_of or date.today()
        sla_days = max(1, sla_days)
        with self.connect() as db:
            outcome_sql = ("SELECT o.outcome,o.actual_annual_value,o.annual_savings,o.currency,o.recorded_at "
                           "FROM renewal_outcomes o JOIN contracts c ON c.id=o.contract_id AND c.tenant_id=o.tenant_id "
                           "JOIN tasks t ON t.id=o.task_id AND t.tenant_id=o.tenant_id WHERE " + " AND ".join(filters))
            outcomes = db.execute(outcome_sql + date_sql.format(event_date="o.recorded_at") + " ORDER BY o.recorded_at", tuple(params + date_params)).fetchall()

            history_sql = ("SELECT h.outcome,h.created_at FROM obligation_compliance_history h "
                           "JOIN contract_obligations o ON o.id=h.obligation_id AND o.tenant_id=h.tenant_id "
                           "JOIN contracts c ON c.id=o.contract_id AND c.tenant_id=o.tenant_id "
                           "JOIN tasks t ON t.id=h.task_id AND t.tenant_id=h.tenant_id WHERE " + " AND ".join(
                               clause.replace("o.tenant_id", "h.tenant_id").replace("c.supplier_id", "c.supplier_id")
                               .replace("t.owner_email", "t.owner_email") for clause in filters))
            compliance = db.execute(history_sql + date_sql.format(event_date="h.created_at") + " ORDER BY h.created_at", tuple(params + date_params)).fetchall()

            overdue_sql = ("SELECT COUNT(*) AS count FROM tasks t JOIN contract_obligations o ON o.task_id=t.id AND o.tenant_id=t.tenant_id "
                           "JOIN contracts c ON c.id=o.contract_id AND c.tenant_id=o.tenant_id WHERE t.tenant_id=? "
                           "AND t.status='open' AND t.due_date<?")
            overdue_params: list[Any] = [tenant_id, as_of.isoformat()]
            if supplier_id:
                overdue_sql += " AND c.supplier_id=?"; overdue_params.append(supplier_id)
            if owner_email:
                overdue_sql += " AND lower(COALESCE(t.owner_email,''))=?"; overdue_params.append(owner_email.strip().casefold())
            overdue_obligations = db.execute(overdue_sql, tuple(overdue_params)).fetchone()["count"]

            pending_sql = ("SELECT e.created_at FROM obligation_evidence e JOIN contract_obligations o ON o.id=e.obligation_id "
                           "JOIN contracts c ON c.id=o.contract_id JOIN tasks t ON t.id=e.task_id "
                           "WHERE e.tenant_id=? AND e.status='pending_signoff' AND t.status='open'")
            pending_params: list[Any] = [tenant_id]
            if supplier_id:
                pending_sql += " AND c.supplier_id=?"; pending_params.append(supplier_id)
            if owner_email:
                pending_sql += " AND lower(COALESCE(t.owner_email,''))=?"; pending_params.append(owner_email.strip().casefold())
            pending_evidence = db.execute(pending_sql, tuple(pending_params)).fetchall()

            pending_playbook_sql = ("SELECT r.applied_at FROM task_playbook_reviews r JOIN tasks t ON t.id=r.task_id "
                                    "JOIN contracts c ON c.id=t.contract_id WHERE r.tenant_id=? AND r.status='blocked' AND t.status='open'")
            pending_playbook_params: list[Any] = [tenant_id]
            if supplier_id:
                pending_playbook_sql += " AND c.supplier_id=?"; pending_playbook_params.append(supplier_id)
            if owner_email:
                pending_playbook_sql += " AND lower(COALESCE(t.owner_email,''))=?"; pending_playbook_params.append(owner_email.strip().casefold())
            pending_playbooks = db.execute(pending_playbook_sql, tuple(pending_playbook_params)).fetchall()

            approval_filters = ["e.tenant_id=?", "e.status IN ('approved','rejected')", "e.reviewed_at IS NOT NULL"]
            approval_params: list[Any] = [tenant_id]
            if supplier_id:
                approval_filters.append("c.supplier_id=?"); approval_params.append(supplier_id)
            if owner_email:
                approval_filters.append("lower(COALESCE(t.owner_email,''))=?"); approval_params.append(owner_email.strip().casefold())
            if start_date:
                approval_filters.append("e.reviewed_at>=?"); approval_params.append(start_date.isoformat())
            if end_date:
                approval_filters.append("e.reviewed_at<=?"); approval_params.append(end_date.isoformat() + "T23:59:59.999999+00:00")
            reviewed_evidence = db.execute(
                "SELECT e.created_at,e.reviewed_at FROM obligation_evidence e JOIN contract_obligations o ON o.id=e.obligation_id "
                "JOIN contracts c ON c.id=o.contract_id JOIN tasks t ON t.id=e.task_id WHERE " + " AND ".join(approval_filters),
                tuple(approval_params),
            ).fetchall()
            playbook_filters = ["r.tenant_id=?", "r.status IN ('approved','rejected')"]
            playbook_params: list[Any] = [tenant_id]
            if supplier_id:
                playbook_filters.append("c.supplier_id=?"); playbook_params.append(supplier_id)
            if owner_email:
                playbook_filters.append("lower(COALESCE(t.owner_email,''))=?"); playbook_params.append(owner_email.strip().casefold())
            playbook_rows = db.execute(
                "SELECT r.applied_at,r.status,r.approvals_json FROM task_playbook_reviews r JOIN tasks t ON t.id=r.task_id "
                "JOIN contracts c ON c.id=t.contract_id WHERE " + " AND ".join(playbook_filters), tuple(playbook_params),
            ).fetchall()
            decision_filters = ["d.tenant_id=?", "d.status IN ('approved','rejected')", "d.reviewed_at IS NOT NULL"]
            decision_params: list[Any] = [tenant_id]
            if supplier_id:
                decision_filters.append("c.supplier_id=?"); decision_params.append(supplier_id)
            if owner_email:
                decision_filters.append("lower(COALESCE(t.owner_email,''))=?"); decision_params.append(owner_email.strip().casefold())
            if start_date:
                decision_filters.append("d.reviewed_at>=?"); decision_params.append(start_date.isoformat())
            if end_date:
                decision_filters.append("d.reviewed_at<=?"); decision_params.append(end_date.isoformat() + "T23:59:59.999999+00:00")
            renewal_decisions = db.execute(
                "SELECT COALESCE((SELECT MIN(e.created_at) FROM renewal_decision_events e "
                "WHERE e.decision_id=d.id AND e.event_type='submitted'),d.created_at) AS submitted_at,d.reviewed_at,d.status "
                "FROM renewal_decisions d JOIN contracts c ON c.id=d.contract_id "
                "JOIN tasks t ON t.id=d.task_id WHERE " + " AND ".join(decision_filters), tuple(decision_params),
            ).fetchall()

        monthly: dict[str, dict[str, Any]] = {}
        outcome_counts: dict[str, int] = {}
        savings: dict[str, Decimal] = {}
        for row in outcomes:
            month = str(row["recorded_at"])[:7]
            entry = monthly.setdefault(month, _empty_performance_month())
            entry["renewal_outcomes"] += 1
            entry["outcomes_by_type"][row["outcome"]] = entry["outcomes_by_type"].get(row["outcome"], 0) + 1
            outcome_counts[row["outcome"]] = outcome_counts.get(row["outcome"], 0) + 1
            if row["currency"]:
                savings[row["currency"]] = savings.get(row["currency"], Decimal(0)) + Decimal(row["annual_savings"] or "0")
        compliance_counts = {"completed": 0, "failed": 0, "missed": 0}
        for row in compliance:
            outcome = row["outcome"]
            if outcome not in compliance_counts:
                continue
            month = str(row["created_at"])[:7]
            entry = monthly.setdefault(month, _empty_performance_month())
            if outcome == "completed":
                entry["obligations_completed"] += 1
            elif outcome == "failed":
                entry["obligations_failed"] += 1
            elif outcome == "missed":
                entry["obligations_missed"] += 1
            compliance_counts[outcome] += 1

        turnaround_days: list[float] = []
        def add_turnaround(start: str | None, finish: str | None) -> None:
            if not start or not finish:
                return
            try:
                finish_day = datetime.fromisoformat(finish).date()
                if (start_date and finish_day < start_date) or (end_date and finish_day > end_date):
                    return
                delta = (datetime.fromisoformat(finish) - datetime.fromisoformat(start)).total_seconds() / 86400
            except ValueError:
                return
            if delta < 0:
                return
            turnaround_days.append(delta)
            month = finish[:7]
            entry = monthly.setdefault(month, _empty_performance_month())
            entry["approval_decisions"] += 1
            entry["approval_turnaround_days_total"] += delta
        for row in reviewed_evidence:
            add_turnaround(row["created_at"], row["reviewed_at"])
        for row in playbook_rows:
            stamps = [item.get("at") for item in json.loads(row["approvals_json"]).values() if item and item.get("at")]
            add_turnaround(row["applied_at"], max(stamps) if stamps else None)
        for row in renewal_decisions:
            add_turnaround(row["submitted_at"], row["reviewed_at"])
        for entry in monthly.values():
            count = entry["approval_decisions"]
            total_days = entry.pop("approval_turnaround_days_total")
            entry["mean_approval_turnaround_days"] = round(total_days / count, 2) if count else None
        return {
            "tenant_id": tenant_id, "as_of": as_of.isoformat(),
            "filters": {"start_date": iso(start_date), "end_date": iso(end_date), "supplier_id": supplier_id,
                        "owner_email": owner_email.strip() if owner_email else None},
            "summary": {
                "renewal_outcome_count": len(outcomes), "renewal_outcomes_by_type": outcome_counts,
                "annual_savings_by_currency": {key: _money_string(value) for key, value in savings.items()},
                "obligations_completed": compliance_counts["completed"], "obligations_failed": compliance_counts["failed"],
                "obligations_missed": compliance_counts["missed"], "overdue_open_obligations": overdue_obligations,
                "overdue_signoffs": sum(1 for row in pending_evidence if _approval_age_days(row["created_at"], as_of) >= sla_days)
                                    + sum(1 for row in pending_playbooks if _approval_age_days(row["applied_at"], as_of) >= sla_days),
                "pending_signoffs": len(pending_evidence) + len(pending_playbooks),
                "approval_decisions": len(turnaround_days),
                "mean_approval_turnaround_days": round(sum(turnaround_days) / len(turnaround_days), 2) if turnaround_days else None,
            },
            "monthly": [{"month": month, **monthly[month]} for month in sorted(monthly)],
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
            deadline_calculation = calculate_deadline(
                data.expiration_date, data.renewal_notice_days or 0, data.notice_day_type,
                timezone=data.notice_timezone, jurisdiction=data.notice_jurisdiction,
                holidays=set(data.notice_holidays), weekend_days=set(data.notice_weekend_days),
            )
            due = date.fromisoformat(deadline_calculation["due_date"])
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
                "INSERT INTO tasks (id, contract_id, title, owner_name, owner_email, due_date, expiration_date, status, created_at, tenant_id, notice_day_type, notice_timezone, notice_holidays_json, notice_jurisdiction, deadline_calculation_json) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)",
                (task_id, contract_id, f"Review renewal: {data.contract or row['filename']}", data.owner_name, data.owner_email, due.isoformat(), data.expiration_date.isoformat(), now, tenant_id, data.notice_day_type, data.notice_timezone, json.dumps([d.isoformat() for d in data.notice_holidays]), data.notice_jurisdiction, json.dumps(deadline_calculation)),
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
            self.add_audit(db, contract_id, "task.created", "system", {"task_id": task_id, "due_date": due.isoformat(), "notice_day_type": data.notice_day_type, "notice_timezone": data.notice_timezone, "notice_jurisdiction": data.notice_jurisdiction, "deadline_calculation": deadline_calculation}, tenant_id)
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
        ranked = sorted(candidates, key=lambda item: (-item["match_score"], item["contract"].casefold()))[:limit]
        for candidate in ranked:
            candidate["automatic_match"] = candidate["match_score"] >= 0.65
        return ranked

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
            deadline_calculation = calculate_deadline(
                data.expiration_date, data.renewal_notice_days or 0, data.notice_day_type,
                timezone=data.notice_timezone, jurisdiction=data.notice_jurisdiction,
                holidays=set(data.notice_holidays), weekend_days=set(data.notice_weekend_days),
            )
            due = date.fromisoformat(deadline_calculation["due_date"])
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
                    "SELECT id,title,due_date,expiration_date,owner_email,workflow_state,task_kind FROM tasks "
                    "WHERE contract_id=? AND tenant_id=? AND status='open' ORDER BY due_date,id",
                    (data.supersedes_contract_id, tenant_id),
                ).fetchall()]
                previous_obligations = [dict(row) for row in db.execute(
                    "SELECT o.id,o.category,o.title,o.description,o.recurrence_interval_months,o.evidence_json,"
                    "t.id AS task_id,t.due_date,t.owner_email FROM contract_obligations o "
                    "JOIN tasks t ON t.id=o.task_id WHERE o.contract_id=? AND o.tenant_id=? AND t.status='open' "
                    "ORDER BY t.due_date,o.title",
                    (data.supersedes_contract_id, tenant_id),
                ).fetchall()]
            else:
                previous_obligations = []

        after = data.model_dump(mode="json")
        before = json.loads(previous["confirmed_json"] or previous["extracted_json"]) if previous else {}
        compared_fields = (
            "contract", "start_date", "expiration_date", "renewal_notice_days", "notice_day_type",
            "notice_timezone", "notice_holidays", "notice_jurisdiction", "notice_weekend_days",
            "notice_method", "notice_recipient", "auto_renew",
            "termination_notice", "owner_name", "owner_email", "annual_contract_value", "currency",
        )
        changes = {
            field: {"before": before.get(field), "after": after.get(field)}
            for field in compared_fields if previous and before.get(field) != after.get(field)
        }
        obligation_proposals = data.obligations or ContractData.model_validate_json(proposal["extracted_json"]).obligations
        proposed_obligations = [
            {
                "category": item.category,
                "title": item.title,
                "due_date": item.due_date.isoformat() if item.due_date else None,
                "due_offset_days": item.due_offset_days,
                "due_offset_from": item.due_offset_from,
                "due_offset_direction": item.due_offset_direction,
                "recurrence_interval_months": item.recurrence_interval_months,
                "evidence_quote": item.evidence.quote,
            }
            for item in obligation_proposals
        ]
        old_obligation_summary = [
            {key: row[key] for key in ("category", "title", "due_date", "owner_email", "recurrence_interval_months")}
            for row in previous_obligations
        ]
        if previous and old_obligation_summary != proposed_obligations:
            changes["obligations"] = {"before": old_obligation_summary, "after": proposed_obligations}
        source_comparison = _compare_source_clauses(previous["raw_text"], proposal["raw_text"]) if previous else None
        detected_clause_categories = sorted({
            category for change in (source_comparison or {}).get("changes", []) for category in change["categories"]
        })
        detected_clause_categories.extend(category for category in self._structured_clause_categories(changes) if category not in detected_clause_categories)
        return {
            "contract_id": contract_id,
            "amendment": previous is not None,
            "supersedes": ({
                "contract_id": previous["id"], "filename": previous["filename"],
                "contract": before.get("contract") or previous["filename"], "status_after_confirmation": "superseded",
            } if previous else None),
            "changes": changes,
            "source_comparison": source_comparison,
            "detected_clause_categories": detected_clause_categories,
            "impact": {
                "open_tasks_to_resolve": previous_tasks,
                "open_obligations_to_review": [
                    {key: row[key] for key in ("id", "task_id", "category", "title", "due_date", "owner_email", "recurrence_interval_months")}
                    for row in previous_obligations
                ],
                "proposed_obligations_to_review": proposed_obligations,
                "new_task": {
                    "title": f"Review renewal: {data.contract or proposal['filename']}",
                    "due_date": due.isoformat(), "expiration_date": data.expiration_date.isoformat(),
                    "owner_email": data.owner_email, "deadline_calculation": deadline_calculation,
                },
            },
        }

    @staticmethod
    def _structured_clause_categories(changes: dict[str, Any]) -> list[str]:
        mapping = {
            "start_date": "effective_term", "expiration_date": "expiration",
            "renewal_notice_days": "notice_period", "notice_day_type": "notice_period",
            "notice_method": "notice_period", "notice_recipient": "notice_period",
            "auto_renew": "auto_renewal", "termination_notice": "termination",
            "annual_contract_value": "pricing", "currency": "pricing",
        }
        return sorted({mapping[field] for field in changes if field in mapping})

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
            "notice_timezone", "notice_holidays", "notice_jurisdiction", "notice_weekend_days",
            "notice_method", "notice_recipient", "auto_renew",
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

    def link_contracts(self, contract_id: str, related_contract_id: str, relationship_type: str,
                       actor: str, tenant_id: str, note: str | None = None) -> dict[str, Any]:
        allowed = {"master_of", "sow_under", "amendment_of", "renewal_of", "related_to"}
        if relationship_type not in allowed:
            raise ValueError("Unsupported contract relationship type")
        if contract_id == related_contract_id:
            raise ValueError("A contract cannot be related to itself")
        now = utc_now().isoformat()
        relationship_id = str(uuid4())
        with self.connect() as db:
            contracts = db.execute(
                "SELECT id FROM contracts WHERE tenant_id=? AND id IN (?,?)",
                (tenant_id, contract_id, related_contract_id),
            ).fetchall()
            if len(contracts) != 2:
                raise KeyError("Both contracts must belong to this organization")
            child_id, parent_id = ((related_contract_id, contract_id) if relationship_type == "master_of"
                                   else (contract_id, related_contract_id))
            if relationship_type in {"master_of", "sow_under", "amendment_of", "renewal_of"}:
                existing_edges = db.execute(
                    "SELECT contract_id,related_contract_id,relationship_type FROM contract_relationships WHERE tenant_id=?",
                    (tenant_id,),
                ).fetchall()
                parents: dict[str, set[str]] = {}
                for edge in existing_edges:
                    if edge["relationship_type"] in {"sow_under", "amendment_of", "renewal_of"}:
                        parents.setdefault(edge["contract_id"], set()).add(edge["related_contract_id"])
                    elif edge["relationship_type"] == "master_of":
                        parents.setdefault(edge["related_contract_id"], set()).add(edge["contract_id"])
                for version in db.execute(
                    "SELECT contract_id,prior_contract_id FROM contract_versions WHERE tenant_id=?", (tenant_id,),
                ).fetchall():
                    parents.setdefault(version["contract_id"], set()).add(version["prior_contract_id"])
                if parent_id in parents.get(child_id, set()):
                    raise ValueError("This contract already inherits terms from that linked contract")
                if parents.get(child_id):
                    raise ValueError("A contract can inherit terms from only one linked parent")
                pending, visited = [parent_id], set()
                while pending:
                    ancestor = pending.pop()
                    if ancestor == child_id:
                        raise ValueError("Contract inheritance relationships cannot contain cycles")
                    if ancestor not in visited:
                        visited.add(ancestor)
                        pending.extend(parents.get(ancestor, set()))
            existing = db.execute(
                "SELECT * FROM contract_relationships WHERE tenant_id=? AND contract_id=? AND related_contract_id=? AND relationship_type=?",
                (tenant_id, contract_id, related_contract_id, relationship_type),
            ).fetchone()
            if existing:
                return dict(existing)
            db.execute(
                "INSERT INTO contract_relationships (id,tenant_id,contract_id,related_contract_id,relationship_type,note,linked_by,linked_at) VALUES (?,?,?,?,?,?,?,?)",
                (relationship_id, tenant_id, contract_id, related_contract_id, relationship_type, note, actor, now),
            )
            self.add_audit(db, contract_id, "contract.relationship_linked", actor, {
                "related_contract_id": related_contract_id, "relationship_type": relationship_type, "note": note,
            }, tenant_id)
            self.add_audit(db, related_contract_id, "contract.relationship_linked", actor, {
                "related_contract_id": contract_id, "relationship_type": relationship_type, "note": note,
            }, tenant_id)
            return {"id": relationship_id, "tenant_id": tenant_id, "contract_id": contract_id,
                    "related_contract_id": related_contract_id, "relationship_type": relationship_type,
                    "note": note, "linked_by": actor, "linked_at": now}

    def contract_relationship_graph(self, contract_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            root = db.execute("SELECT * FROM contracts WHERE id=? AND tenant_id=?", (contract_id, tenant_id)).fetchone()
            if not root:
                return None
            raw_edges = [dict(row) for row in db.execute(
                "SELECT id,contract_id,related_contract_id,relationship_type,note,linked_by,linked_at FROM contract_relationships WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall()]
            version_edges = [dict(row) for row in db.execute(
                "SELECT contract_id,prior_contract_id,linked_by,linked_at FROM contract_versions WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall()]
            edges = raw_edges + [{"id": None, "contract_id": edge["contract_id"],
                                  "related_contract_id": edge["prior_contract_id"],
                                  "relationship_type": "supersedes", "note": None,
                                  "linked_by": edge["linked_by"], "linked_at": edge["linked_at"]}
                                 for edge in version_edges]
            component = {contract_id}
            changed = True
            while changed:
                changed = False
                for edge in edges:
                    left, right = edge["contract_id"], edge["related_contract_id"]
                    if left in component and right not in component:
                        component.add(right); changed = True
                    elif right in component and left not in component:
                        component.add(left); changed = True
            ids = sorted(component)
            placeholders = ",".join("?" for _ in ids)
            nodes = db.execute(
                f"SELECT * FROM contracts WHERE tenant_id=? AND id IN ({placeholders})",
                (tenant_id, *ids),
            ).fetchall()
            audits = db.execute(
                f"SELECT contract_id,event_type,actor,details_json,created_at FROM audit_events WHERE tenant_id=? AND contract_id IN ({placeholders}) ORDER BY created_at,id",
                (tenant_id, *ids),
            ).fetchall()
        node_map = {}
        for row in nodes:
            terms = json.loads(row["confirmed_json"] or row["extracted_json"])
            node_map[row["id"]] = {"id": row["id"], "filename": row["filename"], "status": row["status"],
                                    "created_at": row["created_at"], "confirmed_at": row["confirmed_at"], "terms": terms}
        visible_edges = [edge for edge in edges if edge["contract_id"] in component and edge["related_contract_id"] in component]
        parents: dict[str, list[str]] = {}
        for edge in visible_edges:
            if edge["relationship_type"] in {"sow_under", "amendment_of", "renewal_of", "supersedes"}:
                parents.setdefault(edge["contract_id"], []).append(edge["related_contract_id"])
            elif edge["relationship_type"] == "master_of":
                parents.setdefault(edge["related_contract_id"], []).append(edge["contract_id"])
        effective: dict[str, Any] = {}
        sources: dict[str, str] = {}
        active: set[str] = set()
        inheritable_terms = {
            "start_date", "expiration_date", "renewal_notice_days", "notice_day_type", "notice_timezone",
            "notice_holidays", "notice_jurisdiction", "notice_weekend_days", "notice_method",
            "notice_recipient", "auto_renew", "termination_notice", "annual_contract_value", "currency",
        }
        def resolve(node_id: str) -> tuple[dict[str, Any], dict[str, str]]:
            if node_id in active:
                return {}, {}
            active.add(node_id)
            inherited, inherited_sources = {}, {}
            for parent in sorted(parents.get(node_id, [])):
                values, origins = resolve(parent)
                for key in inheritable_terms:
                    if values.get(key) is not None and key not in inherited:
                        inherited[key], inherited_sources[key] = values[key], origins[key]
            active.remove(node_id)
            terms = node_map[node_id]["terms"]
            for key, value in terms.items():
                if value is not None and value != [] and value != {}:
                    inherited[key], inherited_sources[key] = value, node_id
            return inherited, inherited_sources
        for node_id in ids:
            effective[node_id], sources[node_id] = resolve(node_id)
        timeline = [{"contract_id": row["contract_id"], "event_type": row["event_type"], "actor": row["actor"],
                     "details": json.loads(row["details_json"]), "created_at": row["created_at"]} for row in audits]
        timeline.sort(key=lambda item: (item["created_at"], item["contract_id"]))
        return {"root_contract_id": contract_id, "nodes": [node_map[node_id] for node_id in ids],
                "edges": visible_edges, "effective_terms": effective[contract_id],
                "term_sources": sources[contract_id], "timeline": timeline}

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
        duration_ms: int, error_message: str | None = None, recipient: str | None = None,
    ) -> None:
        stamp = utc_now().isoformat()
        with self.connect() as db:
            task = db.execute("SELECT contract_id, tenant_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                return
            db.execute(
                "INSERT INTO notification_deliveries (tenant_id, task_id, channel, notification_type, status, duration_ms, error_message, attempted_at, recipient) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task["tenant_id"], task_id, channel, notification_type, status, max(0, duration_ms), error_message[:1000] if error_message else None, stamp, recipient),
            )
            self.add_audit(db, task["contract_id"], f"notification.{status}", "system", {
                "task_id": task_id, "channel": channel, "notification_type": notification_type,
                "duration_ms": max(0, duration_ms), "recipient": recipient,
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
                "SELECT task_id, recipient, channel, notification_type, status, duration_ms, error_message, attempted_at "
                "FROM notification_deliveries WHERE tenant_id=? ORDER BY attempted_at DESC LIMIT 50", (tenant_id,),
            ).fetchall()
        by_status: dict[str, int] = {}
        by_type: dict[str, dict[str, int]] = {}
        for row in counts:
            by_status[row["status"]] = by_status.get(row["status"], 0) + row["count"]
            by_type.setdefault(row["job_type"], {})[row["status"]] = row["count"]
        connections = [
            {key: item[key] for key in ("integration_name", "provider", "status", "last_checked_at", "last_error")}
            for item in self.list_integration_connections(tenant_id)
        ]
        return {
            "generated_at": now.isoformat(), "tenant_id": tenant_id,
            "jobs": {"by_status": by_status, "by_type": by_type, "queued_stuck": queued_stuck,
                     "expired_leases": expired_leases, "retrying": retrying,
                     "recent": [dict(row) for row in recent_jobs]},
            "notifications": {"by_channel": [dict(row) for row in deliveries],
                               "failed_last_hour": failed_deliveries_hour,
                               "recent": [dict(row) for row in recent_deliveries]},
            "integrations": {"connections": connections,
                             "error_count": sum(1 for item in connections if item["status"] == "error")},
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
            if row["task_kind"] == "obligation":
                evidence = db.execute(
                    "SELECT status FROM obligation_evidence WHERE task_id=? AND tenant_id=? ORDER BY created_at DESC LIMIT 1",
                    (task_id, tenant_id),
                ).fetchone()
                if not evidence or evidence["status"] != "approved":
                    raise ValueError("Obligations require evidence and reviewer sign-off before resolution")
            db.execute("UPDATE tasks SET status='resolved', workflow_state='resolved' WHERE id=?", (task_id,))
            self.add_audit(db, row["contract_id"], "task.resolved", actor, {"task_id": task_id, "comment": comment}, tenant_id)
            self._create_next_obligation_occurrence(db, row, actor)
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

    @staticmethod
    def _governance_event(db, tenant_id: str, event_type: str, entity_type: str,
                          entity_id: str | None, actor: str, details: dict[str, Any], now: str | None = None) -> None:
        db.execute("INSERT INTO governance_audit_events (tenant_id,event_type,entity_type,entity_id,actor,details_json,created_at) VALUES (?,?,?,?,?,?,?)",
                   (tenant_id,event_type,entity_type,entity_id,actor,json.dumps(details,default=str),now or utc_now().isoformat()))

    def get_data_retention_policy(self, tenant_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM data_retention_policies WHERE tenant_id=?",(tenant_id,)).fetchone()
        return dict(row) if row else None

    def save_data_retention_policy(self, tenant_id: str, config: dict[str, Any], actor: str) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self.connect() as db:
            prior = db.execute("SELECT * FROM data_retention_policies WHERE tenant_id=?",(tenant_id,)).fetchone()
            db.execute("INSERT INTO data_retention_policies (tenant_id,contract_days,evidence_days,provider_evaluation_days,updated_by,updated_at) VALUES (?,?,?,?,?,?) "
                       "ON CONFLICT(tenant_id) DO UPDATE SET contract_days=excluded.contract_days,evidence_days=excluded.evidence_days,provider_evaluation_days=excluded.provider_evaluation_days,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                       (tenant_id,int(config["contract_days"]),int(config["evidence_days"]),int(config["provider_evaluation_days"]),actor,now))
            self._governance_event(db,tenant_id,"data_retention.policy_updated","retention_policy",tenant_id,actor,
                                   {"before":dict(prior) if prior else None,"after":config},now)
        return self.get_data_retention_policy(tenant_id) or {}

    def run_data_retention(self, tenant_id: str, actor: str = "system") -> dict[str, int]:
        policy = self.get_data_retention_policy(tenant_id)
        result = {"contracts_redacted":0,"evidence_deleted":0,"provider_evaluations_deleted":0}
        if not policy:
            return result
        if int(policy["contract_days"]) > 0:
            result["contracts_redacted"] = self.redact_expired_contracts(int(policy["contract_days"]),actor,tenant_id)
        with self.connect() as db:
            now = utc_now().isoformat()
            evidence_days = int(policy["evidence_days"])
            if evidence_days > 0:
                cutoff = (utc_now() - timedelta(days=evidence_days)).isoformat()
                expired = db.execute("SELECT e.id,e.obligation_id,o.contract_id FROM obligation_evidence e "
                                     "JOIN contract_obligations o ON o.id=e.obligation_id AND o.tenant_id=e.tenant_id "
                                     "WHERE e.tenant_id=? AND e.created_at<=? AND NOT EXISTS (SELECT 1 FROM legal_holds h "
                                     "WHERE h.contract_id=o.contract_id AND h.tenant_id=o.tenant_id AND h.released_at IS NULL)",
                                     (tenant_id,cutoff)).fetchall()
                for item in expired:
                    db.execute("DELETE FROM obligation_evidence WHERE id=? AND tenant_id=?",(item["id"],tenant_id))
                    self.add_audit(db,item["contract_id"],"obligation.evidence_purged",actor,
                                   {"evidence_id":item["id"],"obligation_id":item["obligation_id"]},tenant_id)
                    self._governance_event(db,tenant_id,"obligation.evidence_purged","evidence",item["id"],actor,
                                           {"contract_id":item["contract_id"],"obligation_id":item["obligation_id"]},now)
                    result["evidence_deleted"] += 1
            evaluation_days = int(policy["provider_evaluation_days"])
            if evaluation_days > 0:
                cutoff = (utc_now() - timedelta(days=evaluation_days)).isoformat()
                rows = db.execute("SELECT id,provider,model,created_at FROM provider_evaluation_runs WHERE tenant_id=? AND created_at<=?",(tenant_id,cutoff)).fetchall()
                for item in rows:
                    self._governance_event(db,tenant_id,"provider_evaluation.purged","provider_evaluation",item["id"],actor,
                                           {"provider":item["provider"],"model":item["model"],"created_at":item["created_at"]},now)
                    db.execute("DELETE FROM provider_evaluation_runs WHERE id=? AND tenant_id=?",(item["id"],tenant_id))
                    result["provider_evaluations_deleted"] += 1
        return result

    def audit_export_rows(self, tenant_id: str, actor: str, from_date: date | None = None,
                          to_date: date | None = None) -> list[dict[str, Any]]:
        """Export tenant audit metadata only; source text and evidence payloads are excluded."""
        start = from_date.isoformat()+"T00:00:00" if from_date else None
        end = (to_date+timedelta(days=1)).isoformat()+"T00:00:00" if to_date else None
        with self.connect() as db:
            self._governance_event(db,tenant_id,"audit.exported","tenant",tenant_id,actor,
                                   {"from_date":from_date,"to_date":to_date})
            output: list[dict[str, Any]] = []
            def allowed(stamp: str) -> bool:
                return (not start or stamp >= start) and (not end or stamp < end)
            for row in db.execute("SELECT contract_id,event_type,actor,details_json,created_at FROM audit_events WHERE tenant_id=? ORDER BY id",(tenant_id,)).fetchall():
                if allowed(row["created_at"]):
                    output.append({"source":"contract_audit","entity_type":"contract","entity_id":row["contract_id"],"event_type":row["event_type"],"actor":row["actor"],"details":json.loads(row["details_json"]),"created_at":row["created_at"]})
            for row in db.execute("SELECT event_type,entity_type,entity_id,actor,details_json,created_at FROM governance_audit_events WHERE tenant_id=? ORDER BY id",(tenant_id,)).fetchall():
                if allowed(row["created_at"]):
                    output.append({"source":"governance_audit","entity_type":row["entity_type"],"entity_id":row["entity_id"],"event_type":row["event_type"],"actor":row["actor"],"details":json.loads(row["details_json"]),"created_at":row["created_at"]})
            for row in db.execute("SELECT h.obligation_id,h.task_id,h.outcome,h.comment,h.actor,h.evidence_id,h.created_at,o.contract_id FROM obligation_compliance_history h JOIN contract_obligations o ON o.id=h.obligation_id AND o.tenant_id=h.tenant_id WHERE h.tenant_id=? ORDER BY h.id",(tenant_id,)).fetchall():
                if allowed(row["created_at"]):
                    output.append({"source":"obligation_compliance","entity_type":"evidence","entity_id":row["evidence_id"],"contract_id":row["contract_id"],"event_type":row["outcome"],"actor":row["actor"],"details":{"obligation_id":row["obligation_id"],"task_id":row["task_id"],"comment":row["comment"]},"created_at":row["created_at"]})
            for row in db.execute("SELECT id,provider,model,dataset_fingerprint,cases_scored,exact_accuracy,evidence_support_accuracy,clause_accuracy,amendment_comparison_accuracy,created_by,created_at FROM provider_evaluation_runs WHERE tenant_id=? ORDER BY created_at",(tenant_id,)).fetchall():
                if allowed(row["created_at"]):
                    output.append({"source":"provider_evaluation","entity_type":"provider_evaluation","entity_id":row["id"],"event_type":"evaluation.recorded","actor":row["created_by"],"details":{key:row[key] for key in ("provider","model","dataset_fingerprint","cases_scored","exact_accuracy","evidence_support_accuracy","clause_accuracy","amendment_comparison_accuracy")},"created_at":row["created_at"]})
            for row in db.execute("SELECT actor,action,entity_type,entity_id,accessed_at FROM access_events WHERE tenant_id=? ORDER BY id",(tenant_id,)).fetchall():
                if allowed(row["accessed_at"]):
                    output.append({"source":"access_audit","entity_type":row["entity_type"],"entity_id":row["entity_id"],"event_type":row["action"],"actor":row["actor"],"details":{},"created_at":row["accessed_at"]})
        return sorted(output,key=lambda item:item["created_at"])

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
                approval_targets = db.execute(
                    "SELECT id FROM obligation_evidence WHERE task_id=? UNION SELECT id FROM task_playbook_reviews WHERE task_id=?",
                    (task["id"], task["id"]),
                ).fetchall()
                for target in approval_targets:
                    db.execute("DELETE FROM approval_notification_state WHERE target_id=? AND tenant_id=?", (target["id"], tenant_id))
                db.execute("DELETE FROM approval_notification_deliveries WHERE task_id=? AND tenant_id=?", (task["id"], tenant_id))
                obligations = db.execute("SELECT id FROM contract_obligations WHERE task_id=?", (task["id"],)).fetchall()
                for obligation in obligations:
                    db.execute("DELETE FROM obligation_evidence WHERE obligation_id=? AND tenant_id=?", (obligation["id"], tenant_id))
                    db.execute("DELETE FROM obligation_compliance_history WHERE obligation_id=? AND tenant_id=?", (obligation["id"], tenant_id))
                db.execute("DELETE FROM task_playbook_reviews WHERE task_id=? AND tenant_id=?", (task["id"], tenant_id))
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


def _configured_approval_reviewers() -> list[str]:
    configured = os.getenv("APPROVAL_REVIEWER_EMAILS", "")
    return sorted({email.strip().casefold() for email in configured.split(",") if "@" in email})


def _empty_performance_month() -> dict[str, Any]:
    return {"renewal_outcomes": 0, "outcomes_by_type": {}, "obligations_completed": 0,
            "obligations_failed": 0, "obligations_missed": 0, "approval_decisions": 0,
            "approval_turnaround_days_total": 0.0, "mean_approval_turnaround_days": None}


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


def _percent_string(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _compare_source_clauses(active_text: str, amendment_text: str) -> dict[str, Any]:
    """Return exact source quotes for changed clause-sized text spans."""
    def split_clauses(text: str) -> list[str]:
        parts = re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z0-9(])", text or "")
        return [part.strip() for part in parts if len(part.strip()) >= 12]

    def normalized(text: str) -> str:
        return re.sub(r"\W+", " ", text.casefold()).strip()

    before, after = split_clauses(active_text), split_clauses(amendment_text)
    matcher = SequenceMatcher(a=[normalized(x) for x in before], b=[normalized(x) for x in after])
    changes: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for kind, clauses in (("removed", before[i1:i2]), ("added", after[j1:j2])):
            for quote in clauses:
                text = quote.casefold()
                categories = []
                for category, terms in {
                    "effective_term": ("effective date", "commencement", "service start", "term begins"),
                    "expiration": ("expire", "expiration", "expiry", "end of term"),
                    "renewal": ("renew", "extension"),
                    "auto_renewal": ("automatically renew", "auto-renew", "automatic renewal"),
                    "notice_period": ("notice", "days prior", "days before expiration"),
                    "pricing": ("price", "fee", "cost", "payment", "increase"),
                    "termination": ("terminat", "cancel"),
                    "data_retention": ("data", "retain", "delete", "privacy"),
                    "liability": ("liabilit", "indemn", "damages"),
                    "security": ("security", "breach", "encryption"),
                }.items():
                    if any(term in text for term in terms):
                        categories.append(category)
                changes.append({
                    "change": kind, "source_quote": quote, "categories": categories or ["other"],
                    "needs_reviewer_attention": True,
                })
    return {"changes": changes, "unchanged_clause_count": len(set(normalized(x) for x in before) & set(normalized(x) for x in after))}


def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def calculate_notice_deadline(expiration: date, notice_days: int, day_type: str, holidays: set[date] | None = None) -> date:
    """Calculate the action deadline using the explicit contract counting convention."""
    return date.fromisoformat(calculate_deadline(
        expiration, notice_days, day_type, holidays=holidays,
    )["due_date"])
