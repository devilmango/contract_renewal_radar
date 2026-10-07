import json

import pytest
from fastapi import HTTPException

from renewal_radar.api import create_app
from renewal_radar.auth import AuthRegistry
from renewal_radar.jobs import process_jobs
from renewal_radar.store import Store, _PostgresConnection


def get_endpoint(app, path: str, method: str):
    return next(route.endpoint for route in app.routes if route.path == path and method in route.methods)


def test_google_webhook_validates_secret_and_queues_idempotent_reconciliation(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "radar.db"))
    app = create_app(store, AuthRegistry([]))
    callback = get_endpoint(app, "/webhooks/google-drive", "POST")
    monkeypatch.setenv("GOOGLE_DRIVE_WEBHOOK_TOKEN", "secret-value")
    monkeypatch.setenv("DOCUMENT_SOURCE_WEBHOOK_TENANT_ID", "tenant-a")

    with pytest.raises(HTTPException) as error:
        callback("wrong", "channel-1", "7")
    assert error.value.status_code == 401

    first = callback("secret-value", "channel-1", "7")
    duplicate = callback("secret-value", "channel-1", "7")
    assert first["job_id"] == duplicate["job_id"]
    jobs = store.list_jobs("tenant-a")
    assert len(jobs) == 1
    assert jobs[0]["job_type"] == "document_sources.sync"
    assert json.loads(jobs[0]["payload_json"])["provider"] == "google_drive"

    from renewal_radar import drive_ingestion
    monkeypatch.setattr(drive_ingestion, "sync_document_sources", lambda _store, _tenant, provider=None: {"provider": provider})
    result = process_jobs(store, worker_id="webhook-worker")
    assert result["succeeded"] == 1
    assert store.get_job(first["job_id"], "tenant-a")["status"] == "succeeded"


def test_graph_webhook_challenge_and_notification_validation(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "radar.db"))
    app = create_app(store, AuthRegistry([]))
    validation = get_endpoint(app, "/webhooks/microsoft-graph", "GET")
    response = validation("challenge-token")
    assert response.body == b"challenge-token"
    assert response.media_type == "text/plain"

    callback = get_endpoint(app, "/webhooks/microsoft-graph", "POST")
    monkeypatch.setenv("MS_GRAPH_WEBHOOK_CLIENT_STATE", "shared-secret")
    with pytest.raises(HTTPException) as error:
        callback({"value": [{"clientState": "invalid"}]})
    assert error.value.status_code == 401

    payload = {"value": [{"subscriptionId": "sub-1", "clientState": "shared-secret", "resource": "drive/root"}]}
    result = callback(payload)
    assert result["accepted"] is True
    assert len(store.list_jobs("default")) == 1


def test_postgres_adapter_translates_qmark_parameters():
    class FakeConnection:
        def execute(self, sql, params):
            self.sql, self.params = sql, params
            return "cursor"

    raw = FakeConnection()
    adapter = _PostgresConnection(raw)
    assert adapter.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", ("task", "acme")) == "cursor"
    assert raw.sql == "SELECT * FROM tasks WHERE id=%s AND tenant_id=%s"
    assert raw.params == ("task", "acme")


def test_task_inbox_route_is_available(tmp_path):
    app = create_app(Store(str(tmp_path / "radar.db")), AuthRegistry([]))
    page = get_endpoint(app, "/tasks/inbox", "GET")()
    assert "task inbox" in page.body.decode().lower()
    assert "Unassigned only" in page.body.decode()
