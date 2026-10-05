from __future__ import annotations

import hashlib
import json
import os
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .store import Store


class CalendarSyncError(RuntimeError):
    pass


def sync_calendar(store: Store, tenant_id: str) -> dict[str, int | str]:
    """Upsert open renewal tasks into Microsoft 365 or Google Calendar."""
    provider = os.getenv("CALENDAR_PROVIDER", "").strip().casefold()
    if provider not in {"microsoft", "google"}:
        raise CalendarSyncError("Set CALENDAR_PROVIDER to 'microsoft' or 'google' to enable live sync.")
    if provider == "microsoft":
        token = graph_access_token()
        calendar_id = os.getenv("MS_GRAPH_CALENDAR_ID", "").strip()
        user_id = os.getenv("MS_GRAPH_USER_ID", "").strip()
        if not calendar_id or not user_id:
            raise CalendarSyncError("MS_GRAPH_CALENDAR_ID and MS_GRAPH_USER_ID are required.")
        return _sync_graph(store, tenant_id, token, user_id, calendar_id)
    token = os.getenv("GOOGLE_CALENDAR_ACCESS_TOKEN", "").strip()
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
    if not token or not calendar_id:
        raise CalendarSyncError("GOOGLE_CALENDAR_ACCESS_TOKEN and GOOGLE_CALENDAR_ID are required.")
    return _sync_google(store, tenant_id, token, calendar_id)


def graph_access_token() -> str:
    token = os.getenv("MS_GRAPH_ACCESS_TOKEN", "").strip()
    if token:
        return token
    tenant = os.getenv("MS_GRAPH_TENANT_ID", "").strip()
    client_id = os.getenv("MS_GRAPH_CLIENT_ID", "").strip()
    secret = os.getenv("MS_GRAPH_CLIENT_SECRET", "").strip()
    if not all((tenant, client_id, secret)):
        raise CalendarSyncError("Configure an MS_GRAPH_ACCESS_TOKEN or Microsoft tenant/client credentials.")
    body = urlencode({
        "client_id": client_id,
        "client_secret": secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode()
    payload = _request_json(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        method="POST",
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not payload.get("access_token"):
        raise CalendarSyncError("Microsoft identity platform did not return an access token.")
    return payload["access_token"]


def _sync_graph(store: Store, tenant_id: str, token: str, user_id: str, calendar_id: str) -> dict[str, int | str]:
    tasks = store.list_tasks(status="open", tenant_id=tenant_id)
    open_ids = {task["id"] for task in tasks}
    synced = 0
    base = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/events"
    for task in tasks:
        saved = store.get_calendar_event(task["id"], "microsoft", tenant_id)
        url = f"https://graph.microsoft.com/v1.0/users/{user_id}/events/{saved['event_id']}" if saved else base
        event = _graph_event(task)
        if not saved:
            event["transactionId"] = task["id"]
        response = _request_json(url, method="PATCH" if saved else "POST", token=token, payload=event)
        event_id = response.get("id") or (saved["event_id"] if saved else None)
        if not event_id:
            raise CalendarSyncError(f"Microsoft Graph did not return an event ID for task {task['id']}.")
        store.save_calendar_event(task["id"], "microsoft", event_id, tenant_id)
        synced += 1
    removed = 0
    for saved in store.list_calendar_events("microsoft", tenant_id):
        if saved["task_id"] not in open_ids:
            _delete_event(f"https://graph.microsoft.com/v1.0/users/{user_id}/events/{saved['event_id']}", token)
            store.delete_calendar_event(saved["task_id"], "microsoft", tenant_id)
            removed += 1
    return {"provider": "microsoft", "synced": synced, "removed": removed}


def _graph_event(task) -> dict:
    end = date.fromisoformat(task["due_date"]) + timedelta(days=1)
    description = f"Review renewal before contract expiration on {task['expiration_date']}. Task: {task['id']}"
    return {
        "subject": task["title"],
        "body": {"contentType": "text", "content": description},
        "isAllDay": True,
        "start": {"dateTime": task["due_date"] + "T00:00:00", "timeZone": "UTC"},
        "end": {"dateTime": end.isoformat() + "T00:00:00", "timeZone": "UTC"},
        "showAs": "busy",
    }


def _sync_google(store: Store, tenant_id: str, token: str, calendar_id: str) -> dict[str, int | str]:
    tasks = store.list_tasks(status="open", tenant_id=tenant_id)
    open_ids = {task["id"] for task in tasks}
    base = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
    for task in tasks:
        # Google event IDs accept lowercase hexadecimal, making this key repeatable and idempotent.
        event_id = "rr" + hashlib.sha256((tenant_id + task["id"]).encode()).hexdigest()[:40]
        end = date.fromisoformat(task["due_date"]) + timedelta(days=1)
        event = {
            "id": event_id,
            "summary": task["title"],
            "description": f"Review renewal before contract expiration on {task['expiration_date']}. Task: {task['id']}",
            "start": {"date": task["due_date"]},
            "end": {"date": end.isoformat()},
        }
        _request_json(f"{base}/{event_id}", method="PATCH", token=token, payload=event)
        store.save_calendar_event(task["id"], "google", event_id, tenant_id)
    removed = 0
    for saved in store.list_calendar_events("google", tenant_id):
        if saved["task_id"] not in open_ids:
            _delete_event(f"{base}/{saved['event_id']}", token)
            store.delete_calendar_event(saved["task_id"], "google", tenant_id)
            removed += 1
    return {"provider": "google", "synced": len(tasks), "removed": removed}


def _request_json(url: str, *, method: str = "GET", token: str | None = None, payload: dict | None = None,
                  body: bytes | None = None, headers: dict[str, str] | None = None) -> dict:
    request_headers = {"Accept": "application/json", **(headers or {})}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except HTTPError as exc:
        details = exc.read(2000).decode("utf-8", errors="replace")
        if exc.code == 404 and method == "PATCH" and "googleapis.com" in url:
            # Google PATCH requires an existing event; insert then becomes idempotent via the stable ID.
            create_url = url.rsplit("/", 1)[0]
            event = json.loads(body or b"{}")
            return _request_json(create_url, method="POST", token=token, payload=event)
        raise CalendarSyncError(f"Calendar API returned HTTP {exc.code}: {details}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CalendarSyncError(f"Calendar API request failed: {exc}") from exc


def _delete_event(url: str, token: str) -> None:
    request = Request(url, headers={"Authorization": f"Bearer {token}"}, method="DELETE")
    try:
        with urlopen(request, timeout=20):
            return
    except HTTPError as exc:
        if exc.code == 404:
            return
        details = exc.read(2000).decode("utf-8", errors="replace")
        raise CalendarSyncError(f"Calendar delete returned HTTP {exc.code}: {details}") from exc
    except (URLError, TimeoutError) as exc:
        raise CalendarSyncError(f"Calendar delete failed: {exc}") from exc
