"""Checksummed, portable audit archive creation and verification."""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any


ARCHIVE_VERSION = 1
RECORDS_FILENAME = "audit-records.jsonl"
MANIFEST_FILENAME = "manifest.json"


def build_audit_archive(
    rows: list[dict[str, Any]], tenant_id: str, from_date: str | None = None,
    to_date: str | None = None,
) -> bytes:
    """Bundle canonical JSONL with a manifest containing its SHA-256 digest."""
    records = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str) + "\n"
        for row in rows
    ).encode("utf-8")
    manifest = {
        "manifest_version": ARCHIVE_VERSION,
        "hash_algorithm": "sha256",
        "tenant_id": tenant_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "filters": {"from_date": from_date, "to_date": to_date},
        "records_file": RECORDS_FILENAME,
        "record_count": len(rows),
        "records_size_bytes": len(records),
        "records_sha256": hashlib.sha256(records).hexdigest(),
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(RECORDS_FILENAME, records)
        archive.writestr(MANIFEST_FILENAME, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return output.getvalue()


def verify_audit_archive(path: str | Path) -> dict[str, Any]:
    """Verify archive structure, record digest, size, and record count."""
    archive_path = Path(path)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = set(archive.namelist())
            if MANIFEST_FILENAME not in names:
                raise ValueError(f"Archive is missing {MANIFEST_FILENAME}")
            manifest = json.loads(archive.read(MANIFEST_FILENAME))
            records_name = manifest.get("records_file")
            if not records_name or records_name not in names:
                raise ValueError("Manifest references a missing records file")
            if manifest.get("manifest_version") != ARCHIVE_VERSION:
                raise ValueError("Unsupported audit archive manifest version")
            if manifest.get("hash_algorithm") != "sha256":
                raise ValueError("Unsupported audit archive checksum algorithm")
            records = archive.read(records_name)
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read audit archive: {exc}") from exc

    actual_digest = hashlib.sha256(records).hexdigest()
    actual_size = len(records)
    record_lines = records.splitlines()
    try:
        for line in record_lines:
            json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Audit records are not valid JSON Lines: {exc}") from exc
    checks = {
        "checksum_matches": actual_digest == manifest.get("records_sha256"),
        "size_matches": actual_size == manifest.get("records_size_bytes"),
        "record_count_matches": len(record_lines) == manifest.get("record_count"),
    }
    return {
        "valid": all(checks.values()),
        "archive": str(archive_path),
        "tenant_id": manifest.get("tenant_id"),
        "record_count": len(record_lines),
        "records_sha256": actual_digest,
        "checks": checks,
    }
