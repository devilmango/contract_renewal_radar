"""Optional integration test, enabled by CI's ephemeral PostgreSQL service."""

import os
from datetime import date
from uuid import uuid4

import pytest

from renewal_radar.schemas import ContractData
from renewal_radar.store import Store


def test_postgres_schema_migrations_and_concurrent_worker_storage():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to run the PostgreSQL integration check")
    pytest.importorskip("psycopg")

    tenant = f"ci-{uuid4()}"
    store = Store(url)
    contract_id = f"contract-{uuid4()}"
    terms = ContractData(
        contract="CI Vendor Agreement",
        start_date=date(2025, 1, 1),
        expiration_date=date(2026, 1, 1),
        renewal_notice_days=30,
    )
    store.save_contract(contract_id, "agreement.pdf", "rules", terms, "notice 30 days", "ci", tenant)
    task_id = store.confirm_contract(contract_id, terms, "ci", tenant)
    comment = store.add_task_comment(task_id, "Review notice clause", "ci", tenant)
    assert comment["body"] == "Review notice clause"
    assert store.list_tasks(status="open", tenant_id=tenant, unassigned=True)[0]["id"] == task_id
    assert store.get_review_feedback(contract_id, tenant)["status"] == "captured"
    store.record_notification_delivery(task_id, "log", "reminder", "sent", 3)
    assert store.operations_snapshot(tenant)["notifications"]["by_channel"][0]["count"] == 1

    job = store.enqueue_job(tenant, "calendar.sync", {"tenant_id": tenant}, f"sync:{uuid4()}")
    claimed = store.claim_job("ci-postgres-worker", job_id=job["id"])
    assert claimed["id"] == job["id"]
    assert store.complete_job(job["id"], "ci-postgres-worker", {"synced": 1})

    # Reopening exercises idempotent startup/schema upgrade behavior.
    reopened = Store(url)
    assert reopened.get_task(task_id, tenant)["contract_id"] == contract_id
    assert reopened.get_review_feedback(contract_id, tenant)["status"] == "captured"
