from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

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
                    confirmed_at TEXT
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
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id TEXT NOT NULL REFERENCES contracts(id),
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_contract_status ON contracts(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, due_date);
                CREATE INDEX IF NOT EXISTS idx_audit_contract ON audit_events(contract_id, id);
                """
            )

    def save_contract(self, contract_id: str, filename: str, provider: str, data: ContractData, raw_text: str, actor: str) -> None:
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO contracts VALUES (?, ?, 'pending_review', ?, ?, NULL, ?, ?, NULL)",
                (contract_id, filename, provider, data.model_dump_json(), raw_text, now),
            )
            self.add_audit(db, contract_id, "contract.ingested", actor, {"filename": filename, "provider": provider})

    def get_contract(self, contract_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()

    def list_contracts(self, status: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as db:
            if status:
                return db.execute("SELECT * FROM contracts WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
            return db.execute("SELECT * FROM contracts ORDER BY created_at DESC").fetchall()

    def confirm_contract(self, contract_id: str, data: ContractData, actor: str) -> str:
        from uuid import uuid4

        with self.connect() as db:
            row = db.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
            if not row:
                raise KeyError(contract_id)
            if row["status"] != "pending_review":
                raise ValueError("Only contracts pending review can be confirmed")
            if not data.expiration_date:
                raise ValueError("expiration_date is required before a contract can be activated")
            if data.auto_renew and data.renewal_notice_days is None:
                raise ValueError("renewal_notice_days is required for an auto-renewing contract")
            notice_days = data.renewal_notice_days or 0
            due = date.fromordinal(data.expiration_date.toordinal() - notice_days)
            task_id = str(uuid4())
            now = utc_now().isoformat()
            db.execute(
                "UPDATE contracts SET status='active', confirmed_json=?, confirmed_at=? WHERE id=?",
                (data.model_dump_json(), now, contract_id),
            )
            db.execute(
                "INSERT INTO tasks (id, contract_id, title, owner_name, owner_email, due_date, expiration_date, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (task_id, contract_id, f"Review renewal: {data.contract or row['filename']}", data.owner_name, data.owner_email, due.isoformat(), data.expiration_date.isoformat(), now),
            )
            self.add_audit(db, contract_id, "contract.confirmed", actor, {"contract": data.model_dump(mode="json"), "task_id": task_id})
            self.add_audit(db, contract_id, "task.created", "system", {"task_id": task_id, "due_date": due.isoformat()})
            return task_id

    def reject_contract(self, contract_id: str, actor: str) -> None:
        with self.connect() as db:
            row = db.execute("SELECT status FROM contracts WHERE id=?", (contract_id,)).fetchone()
            if not row:
                raise KeyError(contract_id)
            if row["status"] != "pending_review":
                raise ValueError("Only contracts pending review can be rejected")
            db.execute("UPDATE contracts SET status='rejected' WHERE id=?", (contract_id,))
            self.add_audit(db, contract_id, "contract.rejected", actor, {})

    @staticmethod
    def add_audit(db: sqlite3.Connection, contract_id: str, event_type: str, actor: str, details: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO audit_events (contract_id, event_type, actor, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (contract_id, event_type, actor, json.dumps(details, default=str), utc_now().isoformat()),
        )

    def audit(self, contract_id: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM audit_events WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()

    def list_tasks(self, status: str | None = None, owner_email: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as db:
            if owner_email is not None and status:
                return db.execute(
                    "SELECT * FROM tasks WHERE status=? AND lower(owner_email)=lower(?) ORDER BY due_date",
                    (status, owner_email),
                ).fetchall()
            if owner_email is not None:
                return db.execute(
                    "SELECT * FROM tasks WHERE lower(owner_email)=lower(?) ORDER BY due_date",
                    (owner_email,),
                ).fetchall()
            if status:
                return db.execute("SELECT * FROM tasks WHERE status=? ORDER BY due_date", (status,)).fetchall()
            return db.execute("SELECT * FROM tasks ORDER BY due_date").fetchall()

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def due_tasks(self, today: date) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM tasks WHERE status='open' AND due_date<=? ORDER BY due_date", (today.isoformat(),)).fetchall()

    def mark_notified(self, task_id: str, escalation: bool, channel: str) -> None:
        stamp = utc_now().isoformat()
        field = "escalated_at" if escalation else "reminder_sent_at"
        with self.connect() as db:
            task = db.execute("SELECT contract_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                return
            db.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (stamp, task_id))
            self.add_audit(db, task["contract_id"], "task.escalated" if escalation else "task.reminder_sent", "system", {"task_id": task_id, "channel": channel})

    def open_for_escalation(self, cutoff: date) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM tasks WHERE status='open' AND reminder_sent_at IS NOT NULL AND escalated_at IS NULL AND date(reminder_sent_at) <= ?", (cutoff.isoformat(),)).fetchall()

    def resolve_task(self, task_id: str, actor: str, comment: str | None) -> sqlite3.Row | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            if row["status"] != "open":
                raise ValueError("Task is already resolved")
            db.execute("UPDATE tasks SET status='resolved' WHERE id=?", (task_id,))
            self.add_audit(db, row["contract_id"], "task.resolved", actor, {"task_id": task_id, "comment": comment})
            return row
