from __future__ import annotations

import json
import os
import socket
from datetime import date
from uuid import uuid4

from .calendar_sync import sync_calendar
from .reminders import run_reminders
from .store import Store


def enqueue_reminder_run(store: Store, tenant_id: str, *, as_of: date | None = None) -> dict:
    day = (as_of or date.today()).isoformat()
    row = store.enqueue_job(
        tenant_id, "reminders.run", {"tenant_id": tenant_id, "as_of": as_of.isoformat() if as_of else None},
        f"reminders:{tenant_id}:{day}", max_attempts=_max_attempts(),
    )
    if row["status"] == "dead":
        row = store.retry_dead_job(row["id"], tenant_id) or row
    return job_record(row)


def enqueue_calendar_sync(store: Store, tenant_id: str) -> dict:
    row = store.enqueue_job(
        tenant_id, "calendar.sync", {"tenant_id": tenant_id}, f"calendar-sync:{tenant_id}:{uuid4()}",
        max_attempts=_max_attempts(),
    )
    if row["status"] == "dead":
        row = store.retry_dead_job(row["id"], tenant_id) or row
    return job_record(row)


def enqueue_retention_run(store: Store, tenant_id: str, retention_days: int) -> dict:
    day = date.today().isoformat()
    row = store.enqueue_job(
        tenant_id, "retention.purge", {"tenant_id": tenant_id, "retention_days": retention_days},
        f"retention:{tenant_id}:{day}", max_attempts=_max_attempts(),
    )
    if row["status"] == "dead":
        row = store.retry_dead_job(row["id"], tenant_id) or row
    return job_record(row)


def process_jobs(store: Store, limit: int = 20, worker_id: str | None = None, job_id: str | None = None) -> dict[str, int]:
    worker = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    processed = succeeded = retried = dead = 0
    while processed < max(0, limit):
        job = store.claim_job(worker, lease_seconds=max(15, int(os.getenv("JOB_LEASE_SECONDS", "120"))), job_id=job_id)
        if not job:
            break
        processed += 1
        try:
            payload = json.loads(job["payload_json"])
            if job["job_type"] == "reminders.run":
                as_of = date.fromisoformat(payload["as_of"]) if payload.get("as_of") else None
                result = run_reminders(store, today=as_of, tenant_id=job["tenant_id"])
            elif job["job_type"] == "calendar.sync":
                result = sync_calendar(store, job["tenant_id"])
            elif job["job_type"] == "retention.purge":
                result = {"contracts_redacted": store.redact_expired_contracts(int(payload["retention_days"]), tenant_id=job["tenant_id"])}
            else:
                raise ValueError(f"Unsupported job type: {job['job_type']}")
            if store.complete_job(job["id"], worker, result):
                succeeded += 1
        except Exception as exc:
            store.fail_job(job["id"], worker, f"{type(exc).__name__}: {exc}")
            updated = store.get_job(job["id"], job["tenant_id"])
            if updated and updated["status"] == "dead":
                dead += 1
            else:
                retried += 1
    return {"processed": processed, "succeeded": succeeded, "retried": retried, "dead": dead}


def job_record(row) -> dict:
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "job_type": row["job_type"],
        "status": row["status"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "available_at": row["available_at"],
        "last_error": row["last_error"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def _max_attempts() -> int:
    return max(1, int(os.getenv("JOB_MAX_ATTEMPTS", "5")))
