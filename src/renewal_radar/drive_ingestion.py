from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from .calendar_sync import graph_access_token
from .documents import DocumentError
from .extractor import ExtractionError
from .ingestion import ingest_pdf_bytes
from .store import Store


class DocumentSourceError(RuntimeError):
    pass


def sync_document_sources(store: Store, tenant_id: str) -> dict:
    """Poll configured Google Drive and Microsoft Graph delta feeds for PDF changes."""
    results = []
    if os.getenv("GOOGLE_DRIVE_ACCESS_TOKEN"):
        results.append(_sync_google_drive(store, tenant_id))
    graph_configured = os.getenv("MS_GRAPH_DRIVE_ID") and any(
        os.getenv(key) for key in ("MS_GRAPH_ACCESS_TOKEN", "MS_GRAPH_CLIENT_ID")
    )
    if graph_configured:
        results.append(_sync_graph_drive(store, tenant_id))
    if not results:
        raise DocumentSourceError("Configure GOOGLE_DRIVE_ACCESS_TOKEN or Microsoft Graph drive credentials and drive ID.")
    return {"providers": results, "contracts_created": sum(result["contracts_created"] for result in results)}


def _sync_google_drive(store: Store, tenant_id: str) -> dict:
    token = os.environ["GOOGLE_DRIVE_ACCESS_TOKEN"].strip()
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    source_key = folder_id or "root"
    cursor = store.get_integration_cursor(tenant_id, "google_drive", source_key)
    created = scanned = unchanged = 0
    if not cursor:
        start = _json_request("https://www.googleapis.com/drive/v3/changes/startPageToken", token)
        page_token = start.get("startPageToken")
        if not page_token:
            raise DocumentSourceError("Google Drive did not return an initial changes page token.")
        query = "mimeType='application/pdf' and trashed=false"
        if folder_id:
            query += f" and '{folder_id}' in parents"
        files_url = "https://www.googleapis.com/drive/v3/files?" + urlencode({
            "q": query,
            "pageSize": "1000",
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,md5Checksum,version,webViewLink,parents,trashed)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        })
        while files_url:
            response = _json_request(files_url, token)
            for item in response.get("files", []):
                is_new, was_scanned = _ingest_google_file(store, tenant_id, token, source_key, item)
                created += is_new
                scanned += was_scanned
                unchanged += int(not was_scanned)
            next_page = response.get("nextPageToken")
            files_url = "https://www.googleapis.com/drive/v3/files?" + urlencode({
                "q": query,
                "pageSize": "1000",
                "pageToken": next_page,
                "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,md5Checksum,version,webViewLink,parents,trashed)",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }) if next_page else ""
        store.save_integration_cursor(tenant_id, "google_drive", source_key, page_token)
        return {"provider": "google_drive", "source": source_key, "scanned": scanned, "contracts_created": created, "unchanged": unchanged}

    page_token = cursor
    while page_token:
        url = "https://www.googleapis.com/drive/v3/changes?" + urlencode({
            "pageToken": page_token,
            "pageSize": "1000",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "fields": "nextPageToken,newStartPageToken,changes(fileId,removed,file(id,name,mimeType,modifiedTime,md5Checksum,version,webViewLink,parents,trashed))",
        })
        response = _json_request(url, token)
        for change in response.get("changes", []):
            item = change.get("file") or {}
            if change.get("removed") or item.get("trashed"):
                continue
            if folder_id and folder_id not in item.get("parents", []):
                continue
            if not _is_pdf(item):
                continue
            is_new, was_scanned = _ingest_google_file(store, tenant_id, token, source_key, item)
            created += is_new
            scanned += was_scanned
            unchanged += int(not was_scanned)
        page_token = response.get("nextPageToken")
        if not page_token:
            new_cursor = response.get("newStartPageToken")
            if not new_cursor:
                raise DocumentSourceError("Google Drive change feed omitted its next start page token.")
            store.save_integration_cursor(tenant_id, "google_drive", source_key, new_cursor)
    return {"provider": "google_drive", "source": source_key, "scanned": scanned, "contracts_created": created, "unchanged": unchanged}


def _ingest_google_file(store: Store, tenant_id: str, token: str, source_key: str, item: dict) -> tuple[int, int]:
    if not _is_pdf(item):
        return 0, 0
    external_id = item.get("id")
    if not external_id:
        return 0, 0
    revision = str(item.get("md5Checksum") or item.get("version") or item.get("modifiedTime") or "unknown")
    if store.external_document_exists(tenant_id, "google_drive", source_key, external_id, revision):
        return 0, 0
    url = f"https://www.googleapis.com/drive/v3/files/{quote(external_id, safe='')}?alt=media"
    content = _bytes_request(url, token)
    try:
        _contract_id, created = ingest_pdf_bytes(
            store, content, item.get("name", "contract.pdf"), "integration:google_drive", tenant_id,
            source=("google_drive", source_key, external_id, revision, item.get("webViewLink")),
        )
    except (ValueError, DocumentError, ExtractionError) as exc:
        raise DocumentSourceError(f"Could not ingest Google Drive file {item.get('name', external_id)}: {exc}") from exc
    return int(created), 1


def _sync_graph_drive(store: Store, tenant_id: str) -> dict:
    token = graph_access_token()
    drive_id = os.environ["MS_GRAPH_DRIVE_ID"].strip()
    folder_id = os.getenv("MS_GRAPH_FOLDER_ITEM_ID", "").strip()
    source_key = drive_id + (f":{folder_id}" if folder_id else ":root")
    cursor = store.get_integration_cursor(tenant_id, "microsoft_graph", source_key)
    if folder_id:
        initial_url = f"https://graph.microsoft.com/v1.0/drives/{quote(drive_id, safe='')}/items/{quote(folder_id, safe='')}/delta"
    else:
        initial_url = f"https://graph.microsoft.com/v1.0/drives/{quote(drive_id, safe='')}/root/delta"
    url = cursor or initial_url
    created = scanned = unchanged = 0
    while url:
        response = _json_request(url, token, graph=True)
        for item in response.get("value", []):
            if "deleted" in item or not _is_pdf(item):
                continue
            external_id = item.get("id")
            if not external_id:
                continue
            revision = str(item.get("eTag") or item.get("cTag") or item.get("lastModifiedDateTime") or "unknown")
            if store.external_document_exists(tenant_id, "microsoft_graph", source_key, external_id, revision):
                unchanged += 1
                continue
            download_url = item.get("@microsoft.graph.downloadUrl")
            if not download_url:
                raise DocumentSourceError(f"Microsoft Graph omitted the download URL for {item.get('name', external_id)}")
            content = _bytes_request(download_url)
            try:
                _contract_id, was_created = ingest_pdf_bytes(
                    store, content, item.get("name", "contract.pdf"), "integration:microsoft_graph", tenant_id,
                    source=("microsoft_graph", source_key, external_id, revision, item.get("webUrl")),
                )
            except (ValueError, DocumentError, ExtractionError) as exc:
                raise DocumentSourceError(f"Could not ingest Microsoft Drive file {item.get('name', external_id)}: {exc}") from exc
            created += int(was_created)
            scanned += 1
        next_url = response.get("@odata.nextLink")
        if next_url:
            url = next_url
        else:
            delta_link = response.get("@odata.deltaLink")
            if not delta_link:
                raise DocumentSourceError("Microsoft Graph delta feed omitted its next delta link.")
            store.save_integration_cursor(tenant_id, "microsoft_graph", source_key, delta_link)
            url = ""
    return {"provider": "microsoft_graph", "source": source_key, "scanned": scanned, "contracts_created": created, "unchanged": unchanged}


def _is_pdf(item: dict) -> bool:
    if "file" in item and isinstance(item["file"], dict):
        return item["file"].get("mimeType") == "application/pdf" or str(item.get("name", "")).lower().endswith(".pdf")
    return item.get("mimeType") == "application/pdf" or str(item.get("name", "")).lower().endswith(".pdf")


def _json_request(url: str, token: str, *, graph: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if graph and "/root/delta" in url or graph and "/items/" in url and url.endswith("/delta"):
        headers["Prefer"] = "odata.maxpagesize=100"
    try:
        request = Request(url, headers=headers)
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        detail = ""
        if isinstance(exc, HTTPError):
            detail = exc.read(1500).decode("utf-8", errors="replace")
        raise DocumentSourceError(f"Document source request failed: {detail or exc}") from exc
    if not isinstance(result, dict):
        raise DocumentSourceError("Document source returned an unexpected response.")
    return result


def _bytes_request(url: str, token: str | None = None) -> bytes:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with urlopen(Request(url, headers=headers), timeout=60) as response:
            content = response.read(25 * 1024 * 1024 + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise DocumentSourceError(f"Unable to download source document: {exc}") from exc
    if len(content) > 25 * 1024 * 1024:
        raise DocumentSourceError("Source PDF exceeds the 25 MB ingestion limit.")
    return content
