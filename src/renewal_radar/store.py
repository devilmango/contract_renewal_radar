from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from uuid import uuid4

from .schemas import ContractData


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value else None


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("DATABASE_PATH", "renewal_radar.db")
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
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
                    tenant_id TEXT NOT NULL DEFAULT 'default'
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
                    tenant_id TEXT NOT NULL DEFAULT 'default'
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
                CREATE INDEX IF NOT EXISTS idx_contract_status ON contracts(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, due_date);
                CREATE INDEX IF NOT EXISTS idx_audit_contract ON audit_events(contract_id, id);
                """
            )
            # Upgrade databases created by earlier versions without dropping data.
            for table in ("contracts", "tasks", "audit_events"):
                columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                if "tenant_id" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
                if table == "tasks" and "notice_day_type" not in columns:
                    db.execute("ALTER TABLE tasks ADD COLUMN notice_day_type TEXT NOT NULL DEFAULT 'calendar'")
                if table == "tasks" and "notice_timezone" not in columns:
                    db.execute("ALTER TABLE tasks ADD COLUMN notice_timezone TEXT NOT NULL DEFAULT 'UTC'")
                if table == "tasks" and "notice_holidays_json" not in columns:
                    db.execute("ALTER TABLE tasks ADD COLUMN notice_holidays_json TEXT NOT NULL DEFAULT '[]'")
                if table == "tasks" and "workflow_state" not in columns:
                    db.execute("ALTER TABLE tasks ADD COLUMN workflow_state TEXT NOT NULL DEFAULT 'review'")
            db.execute("CREATE INDEX IF NOT EXISTS idx_contract_tenant ON contracts(tenant_id, status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_tenant ON tasks(tenant_id, status, due_date)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(status, available_at, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_access_tenant ON access_events(tenant_id, accessed_at)")

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
                db.execute("UPDATE contracts SET status='superseded' WHERE id=?", (superseded["id"],))
                db.execute("UPDATE tasks SET status='resolved', workflow_state='cancelled' WHERE contract_id=? AND status='open'", (superseded["id"],))
                self.add_audit(db, superseded["id"], "contract.superseded", actor, {"replacement_contract_id": contract_id}, tenant_id)
            db.execute(
                "UPDATE contracts SET status='active', confirmed_json=?, confirmed_at=? WHERE id=?",
                (data.model_dump_json(), now, contract_id),
            )
            db.execute(
                "INSERT INTO tasks (id, contract_id, title, owner_name, owner_email, due_date, expiration_date, status, created_at, tenant_id, notice_day_type, notice_timezone, notice_holidays_json) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                (task_id, contract_id, f"Review renewal: {data.contract or row['filename']}", data.owner_name, data.owner_email, due.isoformat(), data.expiration_date.isoformat(), now, tenant_id, data.notice_day_type, data.notice_timezone, json.dumps([d.isoformat() for d in data.notice_holidays])),
            )
            self.add_audit(db, contract_id, "contract.confirmed", actor, {"contract": data.model_dump(mode="json"), "task_id": task_id}, tenant_id)
            self.add_audit(db, contract_id, "task.created", "system", {"task_id": task_id, "due_date": due.isoformat(), "notice_day_type": data.notice_day_type, "notice_timezone": data.notice_timezone}, tenant_id)
            return task_id

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

    def list_tasks(self, status: str | None = None, owner_email: str | None = None, tenant_id: str = "default") -> list[sqlite3.Row]:
        with self.connect() as db:
            if owner_email is not None and status:
                return db.execute(
                    "SELECT * FROM tasks WHERE tenant_id=? AND status=? AND lower(owner_email)=lower(?) ORDER BY due_date",
                    (tenant_id, status, owner_email),
                ).fetchall()
            if owner_email is not None:
                return db.execute(
                    "SELECT * FROM tasks WHERE tenant_id=? AND lower(owner_email)=lower(?) ORDER BY due_date",
                    (tenant_id, owner_email),
                ).fetchall()
            if status:
                return db.execute("SELECT * FROM tasks WHERE tenant_id=? AND status=? ORDER BY due_date", (tenant_id, status)).fetchall()
            return db.execute("SELECT * FROM tasks WHERE tenant_id=? ORDER BY due_date", (tenant_id,)).fetchall()

    def get_task(self, task_id: str, tenant_id: str = "default") -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()

    def due_tasks(self, today: date | None = None, tenant_id: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as db:
            if today is None and tenant_id is None:
                return db.execute("SELECT * FROM tasks WHERE status='open' ORDER BY due_date").fetchall()
            if today is not None and tenant_id is None:
                return db.execute("SELECT * FROM tasks WHERE status='open' AND due_date<=? ORDER BY due_date", (today.isoformat(),)).fetchall()
            if today is None:
                return db.execute("SELECT * FROM tasks WHERE tenant_id=? AND status='open' ORDER BY due_date", (tenant_id,)).fetchall()
            return db.execute("SELECT * FROM tasks WHERE tenant_id=? AND status='open' AND due_date<=? ORDER BY due_date", (tenant_id, today.isoformat())).fetchall()

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

    def open_for_escalation(self, cutoff: date | None, tenant_id: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as db:
            if cutoff is None and tenant_id is None:
                return db.execute("SELECT * FROM tasks WHERE status='open' AND reminder_sent_at IS NOT NULL AND escalated_at IS NULL").fetchall()
            if cutoff is None:
                return db.execute("SELECT * FROM tasks WHERE tenant_id=? AND status='open' AND reminder_sent_at IS NOT NULL AND escalated_at IS NULL", (tenant_id,)).fetchall()
            if tenant_id is None:
                return db.execute("SELECT * FROM tasks WHERE status='open' AND reminder_sent_at IS NOT NULL AND escalated_at IS NULL AND date(reminder_sent_at) <= ?", (cutoff.isoformat(),)).fetchall()
            return db.execute("SELECT * FROM tasks WHERE tenant_id=? AND status='open' AND reminder_sent_at IS NOT NULL AND escalated_at IS NULL AND date(reminder_sent_at) <= ?", (tenant_id, cutoff.isoformat())).fetchall()

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
                "INSERT INTO task_comments (task_id, tenant_id, actor, body, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, tenant_id, actor, body, utc_now().isoformat()),
            )
            self.add_audit(db, task["contract_id"], "task.comment_added", actor, {"task_id": task_id, "comment_id": cursor.lastrowid}, tenant_id)
            return db.execute("SELECT * FROM task_comments WHERE id=?", (cursor.lastrowid,)).fetchone()

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
                    f"SELECT * FROM jobs WHERE id=? AND {ready} ORDER BY available_at, created_at LIMIT 1",
                    (job_id, stamp, stamp),
                ).fetchone()
            else:
                row = db.execute(
                    f"SELECT * FROM jobs WHERE {ready} ORDER BY available_at, created_at LIMIT 1", (stamp, stamp),
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
                db.execute("DELETE FROM task_comments WHERE task_id=?", (task["id"],))
                db.execute("DELETE FROM notices WHERE task_id=?", (task["id"],))
            db.execute("DELETE FROM tasks WHERE contract_id=?", (contract_id,))
            db.execute("DELETE FROM external_documents WHERE contract_id=?", (contract_id,))
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
