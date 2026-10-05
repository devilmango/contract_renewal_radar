from datetime import date, datetime, timezone

import sqlite3

import pytest

from renewal_radar.schemas import ContractData
from renewal_radar.store import Store


def create_task(store: Store, tenant_id: str = "acme") -> tuple[str, str]:
    contract_id = f"contract-{tenant_id}"
    terms = ContractData(
        contract="Sample Agreement",
        start_date=date(2025, 1, 1),
        expiration_date=date(2026, 1, 1),
        renewal_notice_days=20,
        auto_renew=True,
        owner_name="Legal Owner",
        owner_email="owner@example.com",
    )
    store.save_contract(contract_id, "sample.pdf", "rules", terms, "sample text", "reviewer", tenant_id)
    task_id = store.confirm_contract(contract_id, terms, "reviewer", tenant_id)
    return contract_id, task_id


def test_tenant_scoped_contract_and_task_records(tmp_path):
    store = Store(str(tmp_path / "radar.db"))
    contract_id, task_id = create_task(store, "tenant-a")

    assert store.get_contract(contract_id, "tenant-a") is not None
    assert store.get_contract(contract_id, "tenant-b") is None
    assert store.get_task(task_id, "tenant-b") is None
    assert store.list_tasks(tenant_id="tenant-b") == []


def test_workflow_transitions_comments_and_assignment_are_audited(tmp_path):
    store = Store(str(tmp_path / "radar.db"))
    contract_id, task_id = create_task(store)

    store.transition_task(task_id, "needs_changes", "owner", "Need notice section", "acme")
    store.transition_task(task_id, "review", "reviewer", "Updated terms", "acme")
    store.transition_task(task_id, "pending_approval", "reviewer", None, "acme")
    store.transition_task(task_id, "notice_in_progress", "reviewer", None, "acme")
    task = store.transition_task(task_id, "pending_approval", "owner", "Notice draft ready", "acme")
    assert task["workflow_state"] == "pending_approval"
    assert task["status"] == "open"

    store.assign_task(task_id, "New Owner", "new-owner@example.com", "reviewer", "acme")
    comment = store.add_task_comment(task_id, "Please confirm clause 8.2.", "owner", "acme")
    assert comment["body"] == "Please confirm clause 8.2."
    assert store.get_task(task_id, "acme")["owner_email"] == "new-owner@example.com"

    events = store.audit(contract_id, "acme")
    event_types = [event["event_type"] for event in events]
    assert "task.workflow_transition" in event_types
    assert "task.assigned" in event_types
    assert "task.comment_added" in event_types


def test_notice_requires_approval_then_tracks_dispatch_and_delivery(tmp_path):
    store = Store(str(tmp_path / "radar.db"))
    contract_id, task_id = create_task(store)
    notice = store.create_notice(
        task_id, "acme", "owner", "Non-renewal notice", "Human-reviewed notice text",
        "vendor@example.com", "email",
    )

    with pytest.raises(ValueError, match="approved notice"):
        store.dispatch_notice(notice["id"], "acme", "reviewer", datetime.now(timezone.utc), "mail-1", None)

    approved = store.approve_notice(notice["id"], "acme", "reviewer")
    assert approved["status"] == "approved"
    dispatched = store.dispatch_notice(
        notice["id"], "acme", "reviewer", datetime(2025, 10, 1, tzinfo=timezone.utc), "mail-1", "Sent via vendor portal",
    )
    assert dispatched["status"] == "dispatched"
    delivered = store.mark_notice_delivered(
        notice["id"], "acme", "reviewer", datetime(2025, 10, 3, tzinfo=timezone.utc), "receipt-9", "Receipt uploaded to matter file",
    )
    assert delivered["status"] == "delivered"
    assert delivered["delivery_reference"] == "receipt-9"
    assert [event["event_type"] for event in store.audit(contract_id, "acme")][-3:] == [
        "notice.approved", "notice.dispatched", "notice.delivered",
    ]


def test_external_document_revision_deduplication_and_cursor(tmp_path):
    store = Store(str(tmp_path / "radar.db"))
    terms = ContractData(contract="Drive Agreement")
    first_id, created = store.save_external_contract(
        "contract-1", "drive.pdf", "rules", terms, "source", "acme",
        "google_drive", "folder-1", "file-1", "revision-1", "https://drive.example/file-1",
    )
    second_id, created_again = store.save_external_contract(
        "contract-2", "drive.pdf", "rules", terms, "source", "acme",
        "google_drive", "folder-1", "file-1", "revision-1", "https://drive.example/file-1",
    )
    assert created is True
    assert created_again is False
    assert second_id == first_id == "contract-1"
    assert store.external_document_exists("acme", "google_drive", "folder-1", "file-1", "revision-1")

    store.save_integration_cursor("acme", "google_drive", "folder-1", "page-2")
    assert store.get_integration_cursor("acme", "google_drive", "folder-1") == "page-2"
    assert store.get_integration_cursor("other", "google_drive", "folder-1") is None


def test_sqlite_upgrade_adds_workflow_and_tenant_columns(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE contracts (
              id TEXT PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL, provider TEXT NOT NULL,
              extracted_json TEXT NOT NULL, confirmed_json TEXT, raw_text TEXT NOT NULL, created_at TEXT NOT NULL,
              confirmed_at TEXT
            );
            CREATE TABLE tasks (
              id TEXT PRIMARY KEY, contract_id TEXT NOT NULL REFERENCES contracts(id), title TEXT NOT NULL,
              owner_name TEXT, owner_email TEXT, due_date TEXT NOT NULL, expiration_date TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open', reminder_sent_at TEXT, escalated_at TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE audit_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id TEXT NOT NULL REFERENCES contracts(id),
              event_type TEXT NOT NULL, actor TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            INSERT INTO contracts VALUES ('old-contract','old.pdf','active','rules','{}',NULL,'text','2025-01-01',NULL);
            INSERT INTO tasks VALUES ('old-task','old-contract','Renewal','Owner','owner@example.com','2025-10-01','2026-01-01','open',NULL,NULL,'2025-01-01');
            """
        )

    store = Store(str(path))
    task = store.get_task("old-task", "default")
    assert task["workflow_state"] == "review"
    assert task["notice_timezone"] == "UTC"
    assert task["tenant_id"] == "default"
