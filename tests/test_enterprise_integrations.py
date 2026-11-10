import asyncio
import base64
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from renewal_radar import docusign
from renewal_radar.api import create_app
from renewal_radar.archive_storage import ArchiveStorageError, upload_and_verify_archive
from renewal_radar.auth import AuthRegistry, Principal, TokenRecord
from renewal_radar.integrations import encrypt_credentials
from renewal_radar.schemas import ContractData
from renewal_radar.store import Store


class FakeS3:
    def __init__(self, *, checksum_match=True, versioning="Enabled", object_lock="Enabled"):
        self.versioning = versioning
        self.object_lock = object_lock
        self.checksum_match = checksum_match
        self.put_args = None

    def get_bucket_versioning(self, **_kwargs):
        return {"Status": self.versioning}

    def get_object_lock_configuration(self, **_kwargs):
        return {"ObjectLockConfiguration": {"ObjectLockEnabled": self.object_lock}}

    def put_object(self, **kwargs):
        self.put_args = kwargs
        return {"VersionId": "immutable-version-17"}

    def head_object(self, **kwargs):
        body = self.put_args["Body"]
        digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
        return {
            "ContentLength": len(body), "Metadata": self.put_args["Metadata"],
            "ChecksumSHA256": digest if self.checksum_match else "wrong-checksum",
        }

    def get_object_retention(self, **_kwargs):
        return {"Retention": {
            "Mode": self.put_args["ObjectLockMode"],
            "RetainUntilDate": self.put_args["ObjectLockRetainUntilDate"],
        }}


def test_s3_archive_upload_requires_object_lock_and_verifies_exact_version():
    client = FakeS3()
    result = upload_and_verify_archive(client, "radar-locked", "tenant/audit.zip", b"archive", 30, "COMPLIANCE")
    assert result["object_version_id"] == "immutable-version-17"
    assert result["verification_status"] == "verified"
    assert result["retention_mode"] == "COMPLIANCE"
    assert client.put_args["ObjectLockMode"] == "COMPLIANCE"
    assert client.put_args["ChecksumSHA256"] == base64.b64encode(hashlib.sha256(b"archive").digest()).decode()
    with pytest.raises(ArchiveStorageError, match="versioning"):
        upload_and_verify_archive(FakeS3(versioning="Suspended"), "bucket", "key", b"x", 1, "GOVERNANCE")
    with pytest.raises(ArchiveStorageError, match="checksum"):
        upload_and_verify_archive(FakeS3(checksum_match=False), "bucket", "key", b"x", 1, "GOVERNANCE")


def test_admin_archive_endpoint_records_verified_object_version_and_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", "33" * 32)
    store = Store(str(tmp_path / "archive-api.db"))
    token = "admin-token"
    principal = Principal("admin", frozenset({"admin"}), tenant_id="acme")
    registry = AuthRegistry([TokenRecord(hashlib.sha256(token.encode()).hexdigest(), principal)])
    ciphertext = encrypt_credentials("acme", "audit_storage", {})
    store.save_integration_connection("acme", "audit_storage", "s3_object_lock", {
        "bucket": "radar-locked", "region": "us-east-1", "retention_days": "90", "retention_mode": "GOVERNANCE",
    }, ciphertext, "admin")
    app = create_app(store, registry)
    archive_endpoint = next(route.endpoint for route in app.routes if route.path == "/audit/archive/store")
    monkeypatch.setattr("renewal_radar.archive_storage.s3_client", lambda *_args: FakeS3())
    saved = archive_endpoint(principal=principal)
    assert saved["object_version_id"] == "immutable-version-17"
    assert saved["verification_status"] == "verified"
    assert saved["retain_until"]
    assert store.list_audit_archive_uploads("other") == []

    monkeypatch.setattr("renewal_radar.archive_storage.s3_client", lambda *_args: FakeS3(checksum_match=False))
    with pytest.raises(HTTPException) as exc:
        archive_endpoint(principal=principal)
    assert exc.value.status_code == 502
    failure_id = exc.value.detail["archive_upload_id"]
    failure = store.get_audit_archive_upload(failure_id, "acme")
    assert failure["status"] == "failed"
    assert failure["verification_status"] == "failed"
    assert "verification" in failure["failure_detail"].lower()


def test_docusign_helpers_restrict_hosts_and_send_pdf_as_sent_envelope(monkeypatch):
    observed = {}

    def fake_request(url, token, method="GET", payload=None):
        observed.update(url=url, token=token, method=method, payload=payload)
        return b'{"envelopeId":"env-123","status":"sent"}', "application/json"

    monkeypatch.setattr(docusign, "_request", fake_request)
    response = docusign.create_envelope(
        "https://demo.docusign.net/restapi", "acct-1", "bearer", b"%PDF-file", "agreement.pdf",
        "Reviewer", "reviewer@example.com", "Please sign",
    )
    assert response["envelopeId"] == "env-123"
    assert observed["url"].endswith("/restapi/v2.1/accounts/acct-1/envelopes")
    assert observed["payload"]["status"] == "sent"
    assert base64.b64decode(observed["payload"]["documents"][0]["documentBase64"]) == b"%PDF-file"
    with pytest.raises(docusign.DocuSignError, match="DocuSign domain"):
        docusign.api_base_uri("https://docusign.net.attacker.example")


def test_only_approved_decisions_can_create_docusign_handoffs(tmp_path, monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", "44" * 32)
    store = Store(str(tmp_path / "handoff.db"))
    decision_id = _approved_decision(store)
    encrypted = encrypt_credentials("acme", "signature", {"access_token": "token", "hmac_secret": "secret"})
    store.save_integration_connection("acme", "signature", "docusign", {"account_id": "acct", "base_uri": "https://demo.docusign.net"}, encrypted, "admin")
    monkeypatch.setattr(docusign, "create_envelope", lambda *_args: {"envelopeId": "env-handoff", "status": "sent"})
    reviewer = Principal("reviewer", frozenset({"reviewer"}), tenant_id="acme")
    endpoint = next(route.endpoint for route in create_app(store, AuthRegistry([])).routes if route.path == "/renewal-decisions/{decision_id}/signature-envelope")
    from renewal_radar.schemas import SignatureEnvelopeRequest
    result = endpoint(decision_id, SignatureEnvelopeRequest(signer_name="Counsel", signer_email="counsel@example.com"), reviewer)
    assert result["provider_envelope_id"] == "env-handoff"
    assert result["status"] == "sent"
    with pytest.raises(HTTPException) as error:
        endpoint("missing", SignatureEnvelopeRequest(signer_name="Counsel", signer_email="counsel@example.com"), reviewer)
    assert error.value.status_code == 404


def _approved_decision(store, tenant="acme"):
    terms = ContractData(contract="Renewal Agreement", expiration_date=date(2027, 1, 1), renewal_notice_days=90)
    store.save_contract("contract-1", "agreement.pdf", "rules", terms, "contract text", "reviewer", tenant)
    store.save_contract_document("contract-1", tenant, "agreement.pdf", "application/pdf", b"%PDF-source", hashlib.sha256(b"%PDF-source").hexdigest(), "reviewer")
    task_id = store.confirm_contract("contract-1", terms, "reviewer", tenant)
    decision = store.create_renewal_decision({"task_id": task_id, "strategy": "renew", "reason": "Approved renewal"}, "reviewer", tenant)
    store.submit_renewal_decision(decision["id"], tenant, "reviewer")
    store.review_renewal_decision(decision["id"], tenant, "approver", True, "Approved")
    return decision["id"]


def test_docusign_connect_verifies_hmac_tracks_events_and_attaches_signed_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", "22" * 32)
    store = Store(str(tmp_path / "signature.db"))
    decision_id = _approved_decision(store)
    envelope = store.create_signature_envelope_record(decision_id, "acme", "env-456", "reviewer", "sent")
    encrypted = encrypt_credentials("acme", "signature", {"access_token": "token", "hmac_secret": "webhook-secret"})
    store.save_integration_connection("acme", "signature", "docusign", {"account_id": "acct", "base_uri": "https://demo.docusign.net"}, encrypted, "admin")
    app = create_app(store, AuthRegistry([]))
    callback = next(route.endpoint for route in app.routes if route.path == "/webhooks/docusign/{tenant_id}")
    body = json.dumps({"event": "envelope-completed", "eventId": "evt-1", "data": {"envelopeId": "env-456", "status": "completed"}}).encode()
    signature = base64.b64encode(hmac.new(b"webhook-secret", body, hashlib.sha256).digest()).decode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/webhooks/docusign/acme", "headers": [], "query_string": b"", "server": ("test", 80), "scheme": "http", "client": ("test", 1), "root_path": ""}, receive)
    monkeypatch.setattr(docusign, "download_completed_document", lambda *_args: b"%PDF-signed")
    result = asyncio.run(callback("acme", request, signature, None))
    assert result["status"] == "signed"
    assert result["signed_document"] == "attached"
    assert store.list_signature_envelopes(decision_id, "acme")[0]["status"] == "signed"
    docs = store.list_contract_documents("contract-1", "acme")
    assert [doc["document_type"] for doc in docs] == ["source", "signed_agreement"]
    # Duplicate delivery is idempotent and cannot create another signed file.
    duplicate = asyncio.run(callback("acme", request, signature, None))
    assert duplicate["signed_document"] == "not_applicable"
    assert len(store.list_contract_documents("contract-1", "acme")) == 2
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(callback("acme", request, "invalid", None))
    assert invalid.value.status_code == 401
    assert envelope["status"] == "sent"


def test_scim_group_mapping_deprovisions_users_without_waiting_for_token_expiry(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "scim.db"))
    user = store.scim_upsert_user("acme", {"externalId": "idp-sub-1", "userName": "reviewer@example.com", "emails": [{"value": "reviewer@example.com", "primary": True}]}, "scim")
    group = store.scim_upsert_group("acme", {"externalId": "legal-review", "displayName": "Legal Reviewers", "members": [{"value": user["id"]}]})
    store.save_scim_group_role_mapping("acme", "legal-review", ["reviewer"], "admin")
    registry = AuthRegistry([], {"jwks_url": "unused", "issuer": "issuer", "audience": "aud", "roles_claim": "roles", "tenant_claim": "tenant_id", "groups_claim": "groups"})
    registry.bind_store(store)
    monkeypatch.setattr(registry, "verify_oidc_token", lambda _token: {
        "sub": "idp-sub-1", "preferred_username": "reviewer@example.com", "email": "reviewer@example.com",
        "tenant_id": "acme", "groups": ["legal-review"], "roles": [],
    })
    assert registry.authenticate("Bearer oidc-token").roles == frozenset({"reviewer"})
    assert group["members"][0]["id"] == user["id"]
    store.scim_set_user_active(user["id"], "acme", False, "scim")
    assert registry.authenticate("Bearer oidc-token") is None
    # Group mappings and their grants are tenant-scoped; another tenant cannot inherit this reviewer grant.
    foreign = store.resolve_scim_identity("other", "idp-sub-1", "reviewer@example.com", ["legal-review"])
    assert foreign["roles"] is None


def test_scim_http_provisioning_lists_groups_and_soft_deprovisions(tmp_path):
    store = Store(str(tmp_path / "scim-api.db"))
    admin_token = "tenant-admin"
    scim_token = "scim-provisioner-secret"
    admin = Principal("admin", frozenset({"admin"}), tenant_id="acme")
    store.save_scim_credential("acme", hashlib.sha256(scim_token.encode()).hexdigest(), "admin")
    registry = AuthRegistry([TokenRecord(hashlib.sha256(admin_token.encode()).hexdigest(), admin)])
    app = create_app(store, registry)
    group_endpoint = next(route.endpoint for route in app.routes if route.path == "/scim/v2/Groups" and "POST" in route.methods)
    user_endpoint = next(route.endpoint for route in app.routes if route.path == "/scim/v2/Users" and "POST" in route.methods)
    group_response = group_endpoint({"externalId": "legal-review", "displayName": "Legal reviewers"}, f"Bearer {scim_token}")
    group_id = json.loads(group_response.body)["id"]
    user_response = user_endpoint({
        "externalId": "subject-2", "userName": "counsel@example.com", "displayName": "Counsel",
        "emails": [{"value": "counsel@example.com", "primary": True}],
    }, f"Bearer {scim_token}")
    user_id = json.loads(user_response.body)["id"]
    replace_user = next(route.endpoint for route in app.routes if route.path == "/scim/v2/Users/{user_id}" and "PUT" in route.methods)
    updated_user = replace_user(user_id, {"userName": "counsel@example.com", "displayName": "Senior Counsel", "active": True}, f"Bearer {scim_token}")
    assert updated_user["displayName"] == "Senior Counsel"
    patch_endpoint = next(route.endpoint for route in app.routes if route.path == "/scim/v2/Groups/{group_id}" and "PATCH" in route.methods)
    patch = patch_endpoint(group_id, {"Operations": [{"op": "add", "path": "members", "value": [{"value": user_id}]}]}, f"Bearer {scim_token}")
    assert patch["members"][0]["value"] == user_id
    mapping_endpoint = next(route.endpoint for route in app.routes if route.path == "/scim/group-role-mappings")
    from renewal_radar.schemas import ScimGroupRoleMappingRequest
    assert mapping_endpoint(ScimGroupRoleMappingRequest(group_identifier="legal-review", roles=["reviewer"]), admin)["roles"] == ["reviewer"]
    unmap_endpoint = next(route.endpoint for route in app.routes if route.path == "/scim/group-role-mappings" and "DELETE" in route.methods)
    assert unmap_endpoint("legal-review", admin).status_code == 204
    delete_endpoint = next(route.endpoint for route in app.routes if route.path == "/scim/v2/Users/{user_id}" and "DELETE" in route.methods)
    assert delete_endpoint(user_id, f"Bearer {scim_token}").status_code == 204
    assert store.get_scim_user(user_id, "acme")["active"] is False
    delete_group = next(route.endpoint for route in app.routes if route.path == "/scim/v2/Groups/{group_id}" and "DELETE" in route.methods)
    assert delete_group(group_id, f"Bearer {scim_token}").status_code == 204
    assert store.get_scim_group(group_id, "acme") is None


def test_calibration_profile_records_audited_adoption_and_is_tenant_scoped(tmp_path):
    store = Store(str(tmp_path / "calibration.db"))
    adoption = store.adopt_renewal_calibration("acme", {
        "renewal_increase_pct": Decimal("6.5"), "renegotiation_savings_pct": Decimal("12"),
        "rationale": "Use the reviewed rolling median", "report_window_start": date(2026, 1, 1),
        "report_window_end": date(2026, 6, 30),
    }, "reviewer", 8)
    profile = adoption
    assert profile["source_outcome_count"] == 8
    assert profile["renewal_increase_pct"] == Decimal("6.5")
    assert store.get_renewal_calibration("other") is None
    audit = store.audit_export_rows("acme", "auditor")
    assert any(event["event_type"] == "renewal.calibration_adopted" for event in audit)


def test_contract_redaction_removes_source_and_signed_agreement_bytes(tmp_path):
    store = Store(str(tmp_path / "signature-redaction.db"))
    decision_id = _approved_decision(store)
    envelope = store.create_signature_envelope_record(decision_id, "acme", "env-redact", "reviewer", "sent")
    store.record_signature_envelope_event("acme", "env-redact", "event-redact", "sent", "sent", {})
    assert store.get_signature_envelope(envelope["id"], "acme")
    assert store.redact_contract("contract-1", "acme", "admin")
    assert store.list_contract_documents("contract-1", "acme") == []
    assert store.get_signature_envelope(envelope["id"], "acme") is None

