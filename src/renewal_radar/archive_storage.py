"""AWS S3 Object Lock upload and remote verification for audit archives."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any


class ArchiveStorageError(RuntimeError):
    pass


def s3_client(settings: dict[str, str], credentials: dict[str, str]):
    try:
        import boto3
    except ImportError as exc:
        raise ArchiveStorageError("S3 archive storage requires `pip install -e '.[audit-s3]'`.") from exc
    options: dict[str, Any] = {"region_name": settings["region"]}
    if settings.get("endpoint_url"):
        options["endpoint_url"] = settings["endpoint_url"]
    if credentials.get("access_key_id"):
        options["aws_access_key_id"] = credentials["access_key_id"]
        options["aws_secret_access_key"] = credentials.get("secret_access_key", "")
        if credentials.get("session_token"):
            options["aws_session_token"] = credentials["session_token"]
    try:
        return boto3.client("s3", **options)
    except Exception as exc:
        raise ArchiveStorageError(f"Could not initialize S3 client: {type(exc).__name__}") from exc


def upload_and_verify_archive(
    client, bucket: str, key: str, archive: bytes, retention_days: int, retention_mode: str,
) -> dict[str, Any]:
    """Require versioning and Object Lock, upload with retention, then verify the exact version."""
    if retention_mode not in {"GOVERNANCE", "COMPLIANCE"}:
        raise ArchiveStorageError("Object Lock mode must be GOVERNANCE or COMPLIANCE")
    if not 1 <= retention_days <= 36500:
        raise ArchiveStorageError("Retention days must be between 1 and 36500")
    checksum = hashlib.sha256(archive).hexdigest()
    checksum_b64 = base64.b64encode(bytes.fromhex(checksum)).decode("ascii")
    retain_until = datetime.now(timezone.utc) + timedelta(days=retention_days)
    try:
        versioning = client.get_bucket_versioning(Bucket=bucket)
        if versioning.get("Status") != "Enabled":
            raise ArchiveStorageError("S3 bucket versioning is not enabled")
        lock = client.get_object_lock_configuration(Bucket=bucket).get("ObjectLockConfiguration", {})
        if lock.get("ObjectLockEnabled") != "Enabled":
            raise ArchiveStorageError("S3 Object Lock is not enabled for this bucket")
        uploaded = client.put_object(
            Bucket=bucket, Key=key, Body=archive, ContentType="application/zip",
            ChecksumSHA256=checksum_b64,
            Metadata={"renewal-radar-sha256": checksum},
            ObjectLockMode=retention_mode, ObjectLockRetainUntilDate=retain_until,
        )
        version_id = uploaded.get("VersionId")
        if not version_id:
            raise ArchiveStorageError("S3 did not return an object version ID")
        head = client.head_object(Bucket=bucket, Key=key, VersionId=version_id, ChecksumMode="ENABLED")
        if (head.get("ContentLength") != len(archive)
                or head.get("Metadata", {}).get("renewal-radar-sha256") != checksum
                or head.get("ChecksumSHA256") != checksum_b64):
            raise ArchiveStorageError("Uploaded audit archive failed size or checksum metadata verification")
        retention = client.get_object_retention(Bucket=bucket, Key=key, VersionId=version_id).get("Retention", {})
        confirmed_until = retention.get("RetainUntilDate")
        if retention.get("Mode") != retention_mode or not confirmed_until or confirmed_until < retain_until - timedelta(seconds=5):
            raise ArchiveStorageError("Uploaded object version does not have the requested Object Lock retention")
    except ArchiveStorageError:
        raise
    except Exception as exc:
        response = getattr(exc, "response", {})
        code = response.get("Error", {}).get("Code", type(exc).__name__) if isinstance(response, dict) else type(exc).__name__
        raise ArchiveStorageError(f"S3 archive upload or verification failed ({code})") from exc
    return {
        "bucket": bucket, "object_key": key, "object_version_id": version_id,
        "checksum_sha256": checksum, "retention_mode": retention_mode,
        "retain_until": confirmed_until.astimezone(timezone.utc).isoformat(),
        "size_bytes": len(archive), "verification_status": "verified",
    }
