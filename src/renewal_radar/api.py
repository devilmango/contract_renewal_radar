from __future__ import annotations

import json
import hashlib
import os
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import Cookie, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .auth import AuthRegistry, Principal
from .calendar import export_ics
from .jobs import enqueue_calendar_sync, enqueue_document_source_sync, enqueue_reminder_run, enqueue_retention_run, job_record
from .documents import DocumentError
from .drive_ingestion import DocumentSourceError, sync_document_sources
from .extractor import ExtractionError
from .evaluation import _evidence_supports, validate_approved_deidentified_text
from .ingestion import ingest_pdf_bytes
from .schemas import (
    AccessEvent, AuditEvent, ConfirmationRequest, ContractData, ContractRecord, JobRecord,
    LegalHoldRecord, LegalHoldRequest, NoticeCreateRequest, NoticeDeliveryRequest,
    NoticeDispatchRequest, NoticeRecord, ReminderTask, ResolveTaskRequest, TaskAssignmentRequest,
    EvaluationFeedbackApprovalRequest, TaskComment, TaskCommentRequest, TaskTransitionRequest,
)
from .store import Store


def create_app(store: Store | None = None, auth_registry: AuthRegistry | None = None) -> FastAPI:
    db = store or Store()
    auth = auth_registry or AuthRegistry.from_env()
    app = FastAPI(title="Contract Renewal Radar", version="0.2.0", description="Extract, review, and track contract renewal obligations.")
    app.state.store = db
    app.state.auth_registry = auth

    def authenticate(
        authorization: str | None = Header(default=None),
        radar_session: str | None = Cookie(default=None),
    ) -> Principal:
        if not auth.users and not auth.oidc:
            raise HTTPException(status_code=503, detail="Authentication is not configured. Set RADAR_AUTH_USERS_JSON.")
        principal = auth.authenticate(authorization or (f"Bearer {radar_session}" if radar_session else None))
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="A valid bearer token is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return principal

    def require_scope(principal: Principal, scope: str) -> None:
        if not principal.has_scope(scope):
            raise HTTPException(status_code=403, detail=f"The authenticated user lacks the '{scope}' permission.")

    def task_for_principal(task_id: str, principal: Principal):
        row = db.get_task(task_id, principal.tenant_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        owner_only = "owner" in principal.roles and not (principal.is_admin or "reviewer" in principal.roles)
        if owner_only and (not principal.email or not row["owner_email"] or row["owner_email"].casefold() != principal.email.casefold()):
            raise HTTPException(status_code=403, detail="Owners can access only their assigned tasks.")
        return row

    @app.middleware("http")
    async def access_history_middleware(request, call_next):
        response = await call_next(request)
        if request.url.path not in {"/health", "/docs", "/openapi.json", "/redoc", "/review"}:
            authorization = request.headers.get("authorization")
            session = request.cookies.get("radar_session")
            principal = auth.authenticate(authorization or (f"Bearer {session}" if session else None))
            if principal:
                parts = request.url.path.strip("/").split("/")
                entity_type = parts[0] if parts else "api"
                entity_id = parts[1] if len(parts) > 1 and parts[0] in {"contracts", "tasks", "notices", "jobs", "legal-holds"} else None
                action = f"{request.method} {request.url.path} -> {response.status_code}"
                db.record_access(principal.tenant_id, principal.actor, action, entity_type, entity_id)
        return response

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/review", response_class=HTMLResponse, include_in_schema=False)
    def reviewer_workspace() -> HTMLResponse:
        return HTMLResponse(_REVIEWER_HTML)

    @app.get("/tasks/inbox", response_class=HTMLResponse, include_in_schema=False)
    def task_inbox() -> HTMLResponse:
        return HTMLResponse(_TASK_INBOX_HTML)

    @app.get("/operations", response_class=HTMLResponse, include_in_schema=False)
    def operations_dashboard() -> HTMLResponse:
        return HTMLResponse(_OPERATIONS_HTML)

    @app.get("/auth/oidc/login", include_in_schema=False)
    def oidc_login() -> RedirectResponse:
        endpoint = os.getenv("OIDC_AUTHORIZATION_ENDPOINT", "").strip()
        client_id = os.getenv("OIDC_CLIENT_ID", "").strip()
        redirect_uri = os.getenv("OIDC_REDIRECT_URI", "").strip()
        if not auth.oidc or not all((endpoint, client_id, redirect_uri)):
            raise HTTPException(status_code=503, detail="OIDC interactive login is not fully configured.")
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        query = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": os.getenv("OIDC_SCOPES", "openid profile email"),
            "state": state,
            "nonce": nonce,
        })
        response = RedirectResponse(f"{endpoint}?{query}", status_code=302)
        response.set_cookie(
            "radar_oidc_state", state, httponly=True, secure=_secure_cookies(),
            samesite="lax", max_age=600, path="/auth/oidc/callback",
        )
        response.set_cookie(
            "radar_oidc_nonce", nonce, httponly=True, secure=_secure_cookies(),
            samesite="lax", max_age=600, path="/auth/oidc/callback",
        )
        return response

    @app.get("/auth/oidc/callback", include_in_schema=False)
    def oidc_callback(
        code: str,
        state: str,
        radar_oidc_state: str | None = Cookie(default=None),
        radar_oidc_nonce: str | None = Cookie(default=None),
    ) -> RedirectResponse:
        if (
            not auth.oidc or not radar_oidc_state or not radar_oidc_nonce
            or not secrets.compare_digest(state, radar_oidc_state)
        ):
            raise HTTPException(status_code=400, detail="OIDC state validation failed.")
        token_endpoint = os.getenv("OIDC_TOKEN_ENDPOINT", "").strip()
        client_id = os.getenv("OIDC_CLIENT_ID", "").strip()
        client_secret = os.getenv("OIDC_CLIENT_SECRET", "").strip()
        redirect_uri = os.getenv("OIDC_REDIRECT_URI", "").strip()
        if not all((token_endpoint, client_id, client_secret, redirect_uri)):
            raise HTTPException(status_code=503, detail="OIDC token exchange is not fully configured.")
        body = urlencode({
            "grant_type": "authorization_code", "code": code, "client_id": client_id,
            "client_secret": client_secret, "redirect_uri": redirect_uri,
        }).encode()
        try:
            request = Request(
                token_endpoint, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
            with urlopen(request, timeout=15) as upstream:
                tokens = json.loads(upstream.read())
        except Exception as exc:
            raise HTTPException(status_code=502, detail="OIDC authorization-code exchange failed.") from exc
        identity_token = tokens.get("id_token") if isinstance(tokens, dict) else None
        claims = auth.verify_oidc_token(identity_token) if identity_token else None
        if not claims or claims.get("nonce") != radar_oidc_nonce:
            raise HTTPException(status_code=401, detail="OIDC identity token nonce validation failed.")
        principal = auth.authenticate(f"Bearer {identity_token}") if identity_token else None
        if principal is None:
            raise HTTPException(status_code=401, detail="OIDC identity token failed validation or lacks role and tenant claims.")
        response = RedirectResponse("/review", status_code=303)
        response.set_cookie(
            "radar_session", identity_token, httponly=True, secure=_secure_cookies(),
            samesite="lax", max_age=3600, path="/",
        )
        response.delete_cookie("radar_oidc_state", path="/auth/oidc/callback")
        response.delete_cookie("radar_oidc_nonce", path="/auth/oidc/callback")
        return response

    @app.post("/auth/logout", include_in_schema=False)
    def logout() -> Response:
        response = Response(status_code=204)
        response.delete_cookie("radar_session", path="/")
        return response

    @app.post("/contracts", response_model=ContractRecord, status_code=201)
    async def ingest_contract(
        file: UploadFile = File(...), principal: Principal = Depends(authenticate)
    ) -> ContractRecord:
        require_scope(principal, "contracts:upload")
        filename = Path(file.filename or "contract.pdf").name
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=415, detail="Only PDF files are supported")
        content = await file.read(25 * 1024 * 1024 + 1)
        if len(content) > 25 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="PDF must be 25 MB or smaller")
        try:
            contract_id, _created = ingest_pdf_bytes(db, content, filename, principal.actor, principal.tenant_id)
        except (DocumentError, ExtractionError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _contract_record(db.get_contract(contract_id, principal.tenant_id))

    @app.get("/contracts", response_model=list[ContractRecord])
    def list_contracts(
        status: str | None = Query(default=None, pattern="^(pending_review|active|rejected|superseded|redacted)$"),
        principal: Principal = Depends(authenticate),
    ) -> list[ContractRecord]:
        require_scope(principal, "contracts:read")
        return [_contract_record(row) for row in db.list_contracts(status, principal.tenant_id)]

    @app.get("/contracts/{contract_id}", response_model=ContractRecord)
    def get_contract(contract_id: str, principal: Principal = Depends(authenticate)) -> ContractRecord:
        require_scope(principal, "contracts:read")
        row = db.get_contract(contract_id, principal.tenant_id)
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        return _contract_record(row)

    @app.delete("/contracts/{contract_id}", status_code=204)
    def redact_contract(contract_id: str, principal: Principal = Depends(authenticate)) -> Response:
        require_scope(principal, "data:delete")
        try:
            redacted = db.redact_contract(contract_id, principal.tenant_id, principal.actor)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not redacted:
            raise HTTPException(status_code=404, detail="Contract not found")
        return Response(status_code=204)

    @app.get("/contracts/{contract_id}/legal-holds", response_model=list[LegalHoldRecord])
    def get_legal_holds(contract_id: str, principal: Principal = Depends(authenticate)) -> list[LegalHoldRecord]:
        require_scope(principal, "data:hold")
        if not db.get_contract(contract_id, principal.tenant_id):
            raise HTTPException(status_code=404, detail="Contract not found")
        return [_legal_hold_record(row) for row in db.list_legal_holds(contract_id, principal.tenant_id)]

    @app.post("/contracts/{contract_id}/legal-holds", response_model=LegalHoldRecord, status_code=201)
    def place_legal_hold(contract_id: str, request: LegalHoldRequest, principal: Principal = Depends(authenticate)) -> LegalHoldRecord:
        require_scope(principal, "data:hold")
        row = db.place_legal_hold(contract_id, principal.tenant_id, principal.actor, request.reason)
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        return _legal_hold_record(row)

    @app.post("/legal-holds/{hold_id}/release", response_model=LegalHoldRecord)
    def release_legal_hold(hold_id: str, principal: Principal = Depends(authenticate)) -> LegalHoldRecord:
        require_scope(principal, "data:hold")
        try:
            row = db.release_legal_hold(hold_id, principal.tenant_id, principal.actor)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not row:
            raise HTTPException(status_code=404, detail="Legal hold not found")
        return _legal_hold_record(row)

    @app.get("/access-history", response_model=list[AccessEvent])
    def access_history(limit: int = Query(default=100, ge=1, le=1000), principal: Principal = Depends(authenticate)) -> list[AccessEvent]:
        require_scope(principal, "access:read")
        return [AccessEvent(**dict(row)) for row in db.access_history(principal.tenant_id, limit)]

    @app.get("/contracts/{contract_id}/source")
    def get_contract_source(contract_id: str, principal: Principal = Depends(authenticate)) -> dict[str, str]:
        require_scope(principal, "contracts:source")
        row = db.get_contract(contract_id, principal.tenant_id)
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        return {"text": row["raw_text"]}

    @app.post("/contracts/{contract_id}/confirm", response_model=ContractRecord)
    def confirm_contract(
        contract_id: str,
        request: ConfirmationRequest,
        principal: Principal = Depends(authenticate),
    ) -> ContractRecord:
        require_scope(principal, "contracts:review")
        try:
            db.confirm_contract(contract_id, request.contract, principal.actor, principal.tenant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Contract not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _contract_record(db.get_contract(contract_id, principal.tenant_id))

    @app.get("/contracts/{contract_id}/evaluation-feedback")
    def get_evaluation_feedback(contract_id: str, principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "contracts:review")
        if not db.get_contract(contract_id, principal.tenant_id):
            raise HTTPException(status_code=404, detail="Contract not found")
        row = db.get_review_feedback(contract_id, principal.tenant_id)
        if not row:
            raise HTTPException(status_code=404, detail="No reviewer feedback has been captured for this contract")
        return _feedback_record(row)

    @app.post("/contracts/{contract_id}/evaluation-feedback/approve")
    def approve_evaluation_feedback(
        contract_id: str, request: EvaluationFeedbackApprovalRequest,
        principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "evaluation:approve")
        contract = db.get_contract(contract_id, principal.tenant_id)
        if not contract:
            raise HTTPException(status_code=404, detail="Contract not found")
        if contract["status"] != "active":
            raise HTTPException(status_code=409, detail="Only confirmed active contracts can be approved for evaluation feedback")
        feedback = db.get_review_feedback(contract_id, principal.tenant_id)
        if not feedback:
            raise HTTPException(status_code=404, detail="Reviewer feedback was not found")
        if feedback["status"] != "captured":
            raise HTTPException(status_code=409, detail="Reviewer feedback has already been approved")
        if len(set(request.fields)) != len(request.fields) or len(set(request.clause_categories)) != len(request.clause_categories):
            raise HTTPException(status_code=422, detail="Fields and clause categories must not contain duplicates")
        try:
            validate_approved_deidentified_text(request.redacted_text, contract_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        reviewed = json.loads(feedback["reviewed_json"])
        expected = {field: reviewed.get(field) for field in request.fields}
        if any(value is None for value in expected.values()):
            raise HTTPException(status_code=422, detail="Selected evaluation fields must have reviewer-confirmed values")
        selected_evidence: dict[str, str] = {}
        normalized_text = " ".join(request.redacted_text.split()).casefold()
        for evidence in request.evidence:
            if evidence.field not in expected:
                raise HTTPException(status_code=422, detail=f"Evidence field '{evidence.field}' is not selected for evaluation")
            if " ".join(evidence.quote.split()).casefold() not in normalized_text:
                raise HTTPException(status_code=422, detail=f"Evidence for {evidence.field} must appear in the redacted contract text")
            if not _evidence_supports(evidence.field, expected[evidence.field], evidence.quote):
                raise HTTPException(status_code=422, detail=f"Evidence does not support the reviewer-confirmed value for {evidence.field}")
            selected_evidence[evidence.field] = evidence.quote
        missing_evidence = set(expected) - set(selected_evidence)
        if missing_evidence:
            raise HTTPException(status_code=422, detail=f"Add approved evidence for: {', '.join(sorted(missing_evidence))}")

        case = {
            "id": f"review-feedback-{feedback['id']}",
            "source_type": "approved_deidentified",
            "contract_text": request.redacted_text,
            "expected": expected,
            "clause_categories": request.clause_categories,
            "provider_under_review": feedback["extraction_provider"],
            "reviewed_evidence": [item.model_dump(mode="json") for item in request.evidence],
            "governance": {
                "approved": True, "deidentified": True,
                "approval_reference": request.approval_reference,
                "approved_by": principal.actor,
            },
        }
        row = db.approve_review_feedback(
            contract_id, principal.tenant_id, principal.actor, request.approval_reference,
            request.clause_categories, case,
        )
        if not row:
            raise HTTPException(status_code=409, detail="Reviewer feedback has already been approved")
        return _feedback_record(row)

    @app.post("/contracts/{contract_id}/reject", response_model=ContractRecord)
    def reject_contract(contract_id: str, principal: Principal = Depends(authenticate)) -> ContractRecord:
        require_scope(principal, "contracts:review")
        try:
            db.reject_contract(contract_id, principal.actor, principal.tenant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Contract not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _contract_record(db.get_contract(contract_id, principal.tenant_id))

    @app.get("/contracts/{contract_id}/audit", response_model=list[AuditEvent])
    def get_audit(contract_id: str, principal: Principal = Depends(authenticate)) -> list[AuditEvent]:
        require_scope(principal, "audit:read")
        if not db.get_contract(contract_id, principal.tenant_id):
            raise HTTPException(status_code=404, detail="Contract not found")
        return [_audit_record(row) for row in db.audit(contract_id, principal.tenant_id)]

    @app.get("/tasks", response_model=list[ReminderTask])
    def list_tasks(
        status: str | None = Query(default=None, pattern="^(open|resolved)$"),
        workflow_state: str | None = Query(default=None, pattern="^(review|needs_changes|pending_approval|notice_in_progress|renewed|terminated|cancelled|resolved)$"),
        due_before: date | None = Query(default=None),
        unassigned: bool = Query(default=False),
        principal: Principal = Depends(authenticate),
    ) -> list[ReminderTask]:
        require_scope(principal, "tasks:read")
        owner_only = "owner" in principal.roles and not (principal.is_admin or "reviewer" in principal.roles)
        if owner_only and not principal.email:
            raise HTTPException(status_code=403, detail="An email address is required for owner-scoped task access.")
        rows = db.list_tasks(
            status, owner_email=principal.email if owner_only else None, tenant_id=principal.tenant_id,
            workflow_state=workflow_state, due_before=due_before, unassigned=unassigned,
        )
        return [_task_record(row) for row in rows]

    @app.post("/tasks/{task_id}/resolve", response_model=ReminderTask)
    def resolve_task(
        task_id: str,
        request: ResolveTaskRequest,
        principal: Principal = Depends(authenticate),
    ) -> ReminderTask:
        require_scope(principal, "tasks:resolve")
        existing = db.get_task(task_id, principal.tenant_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Task not found")
        owner_only = "owner" in principal.roles and not (principal.is_admin or "reviewer" in principal.roles)
        if owner_only and (
            not principal.email
            or not existing["owner_email"]
            or existing["owner_email"].casefold() != principal.email.casefold()
        ):
            raise HTTPException(status_code=403, detail="Owners can resolve only their assigned tasks.")
        try:
            row = db.resolve_task(task_id, principal.actor, request.comment, principal.tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        updated = db.get_task(task_id, principal.tenant_id)
        return _task_record(updated)

    @app.get("/calendar.ics")
    def calendar(principal: Principal = Depends(authenticate)) -> Response:
        require_scope(principal, "calendar:read")
        owner_only = "owner" in principal.roles and not (principal.is_admin or "reviewer" in principal.roles)
        if owner_only and not principal.email:
            raise HTTPException(status_code=403, detail="An email address is required for owner-scoped calendar access.")
        content = export_ics(db, owner_email=principal.email if owner_only else None, tenant_id=principal.tenant_id)
        return Response(content, media_type="text/calendar", headers={"Content-Disposition": "attachment; filename=renewal-radar.ics"})

    @app.post("/reminders/run", response_model=JobRecord, status_code=202)
    def trigger_reminders(principal: Principal = Depends(authenticate)) -> JobRecord:
        require_scope(principal, "reminders:run")
        return JobRecord(**job_record(enqueue_reminder_run(db, principal.tenant_id)))

    @app.post("/calendar/sync", response_model=JobRecord, status_code=202)
    def calendar_sync(principal: Principal = Depends(authenticate)) -> JobRecord:
        require_scope(principal, "calendar:sync")
        return JobRecord(**job_record(enqueue_calendar_sync(db, principal.tenant_id)))

    @app.get("/operations/health")
    def operations_health(
        stuck_after_minutes: int = Query(default=15, ge=1, le=1440),
        principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "jobs:read")
        snapshot = db.operations_snapshot(principal.tenant_id, stuck_after_minutes)
        alerts = []
        jobs = snapshot["jobs"]
        notifications = snapshot["notifications"]
        dead_jobs = jobs["by_status"].get("dead", 0)
        if dead_jobs:
            alerts.append({"severity": "critical", "code": "dead_jobs", "count": dead_jobs, "message": f"{dead_jobs} job(s) are in the dead-letter state."})
        if jobs["queued_stuck"]:
            alerts.append({"severity": "warning", "code": "queued_jobs_stuck", "count": jobs["queued_stuck"], "message": f"{jobs['queued_stuck']} due job(s) have waited longer than {stuck_after_minutes} minutes."})
        if jobs["expired_leases"]:
            alerts.append({"severity": "warning", "code": "expired_job_leases", "count": jobs["expired_leases"], "message": f"{jobs['expired_leases']} running job lease(s) have expired."})
        if notifications["failed_last_hour"] >= 3:
            alerts.append({"severity": "critical", "code": "repeated_notification_failures", "count": notifications["failed_last_hour"], "message": f"{notifications['failed_last_hour']} notification delivery attempts failed in the last hour."})
        snapshot["alerts"] = alerts
        snapshot["status"] = "degraded" if alerts else "healthy"
        return snapshot

    @app.get("/jobs", response_model=list[JobRecord])
    def list_jobs(limit: int = Query(default=100, ge=1, le=1000), principal: Principal = Depends(authenticate)) -> list[JobRecord]:
        require_scope(principal, "jobs:read")
        return [JobRecord(**job_record(row)) for row in db.list_jobs(principal.tenant_id, limit)]

    @app.get("/jobs/{job_id}", response_model=JobRecord)
    def get_job(job_id: str, principal: Principal = Depends(authenticate)) -> JobRecord:
        require_scope(principal, "jobs:read")
        row = db.get_job(job_id, principal.tenant_id)
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")
        return JobRecord(**job_record(row))

    @app.post("/jobs/{job_id}/retry", response_model=JobRecord)
    def retry_job(job_id: str, principal: Principal = Depends(authenticate)) -> JobRecord:
        require_scope(principal, "jobs:retry")
        row = db.retry_dead_job(job_id, principal.tenant_id)
        if not row:
            raise HTTPException(status_code=404, detail="Dead job not found")
        return JobRecord(**job_record(row))

    @app.post("/data-retention/run", response_model=JobRecord, status_code=202)
    def trigger_retention(principal: Principal = Depends(authenticate)) -> JobRecord:
        require_scope(principal, "data:delete")
        retention_days = int(os.getenv("CONTRACT_RETENTION_DAYS", "0"))
        if retention_days <= 0:
            raise HTTPException(status_code=409, detail="Set CONTRACT_RETENTION_DAYS to a positive number first.")
        return JobRecord(**job_record(enqueue_retention_run(db, principal.tenant_id, retention_days)))

    @app.post("/document-sources/sync")
    def document_source_sync(principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "integrations:sync")
        try:
            return sync_document_sources(db, principal.tenant_id)
        except DocumentSourceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/webhooks/microsoft-graph", include_in_schema=False)
    def graph_webhook_validation(validationToken: str = Query(...)) -> PlainTextResponse:
        # Microsoft Graph validates a newly registered notification URL with this challenge.
        return PlainTextResponse(validationToken, media_type="text/plain")

    @app.post("/webhooks/microsoft-graph", status_code=202, include_in_schema=False)
    def graph_webhook(payload: dict) -> dict:
        expected = os.getenv("MS_GRAPH_WEBHOOK_CLIENT_STATE", "").strip()
        notifications = payload.get("value") if isinstance(payload, dict) else None
        if not expected or not isinstance(notifications, list) or not notifications:
            raise HTTPException(status_code=503 if not expected else 400, detail="Webhook secret or notification payload is missing.")
        if any(
            not isinstance(item, dict) or not isinstance(item.get("clientState"), str)
            or not secrets.compare_digest(item["clientState"], expected)
            for item in notifications
        ):
            raise HTTPException(status_code=401, detail="Microsoft Graph webhook clientState is invalid.")
        event_key = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        job = enqueue_document_source_sync(
            db, _webhook_tenant_id(), "microsoft_graph", event_key,
        )
        return {"accepted": True, "job_id": job["id"]}

    @app.post("/webhooks/google-drive", status_code=202, include_in_schema=False)
    def google_drive_webhook(
        channel_token: str | None = Header(default=None, alias="X-Goog-Channel-Token"),
        channel_id: str | None = Header(default=None, alias="X-Goog-Channel-ID"),
        message_number: str | None = Header(default=None, alias="X-Goog-Message-Number"),
    ) -> dict:
        expected = os.getenv("GOOGLE_DRIVE_WEBHOOK_TOKEN", "").strip()
        if not expected:
            raise HTTPException(status_code=503, detail="Google Drive webhook secret is not configured.")
        if not channel_token or not secrets.compare_digest(channel_token, expected):
            raise HTTPException(status_code=401, detail="Google Drive webhook token is invalid.")
        event_key = f"{channel_id or 'unknown'}:{message_number or secrets.token_urlsafe(12)}"
        job = enqueue_document_source_sync(
            db, _webhook_tenant_id(), "google_drive", event_key,
        )
        return {"accepted": True, "job_id": job["id"]}

    @app.post("/tasks/{task_id}/transition", response_model=ReminderTask)
    def transition_task(task_id: str, request: TaskTransitionRequest, principal: Principal = Depends(authenticate)) -> ReminderTask:
        require_scope(principal, "tasks:workflow")
        existing = task_for_principal(task_id, principal)
        is_reviewer = principal.is_admin or "reviewer" in principal.roles
        if not is_reviewer:
            allowed = {
                ("review", "needs_changes"),
                ("notice_in_progress", "pending_approval"),
            }
            if (existing["workflow_state"], request.workflow_state) not in allowed:
                raise HTTPException(status_code=403, detail="Owners can request changes or submit notice work for approval.")
        try:
            row = db.transition_task(task_id, request.workflow_state, principal.actor, request.comment, principal.tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _task_record(row)

    @app.post("/tasks/{task_id}/assign", response_model=ReminderTask)
    def assign_task(task_id: str, request: TaskAssignmentRequest, principal: Principal = Depends(authenticate)) -> ReminderTask:
        require_scope(principal, "tasks:assign")
        try:
            row = db.assign_task(task_id, request.owner_name, request.owner_email, principal.actor, principal.tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        return _task_record(row)

    @app.get("/tasks/{task_id}/comments", response_model=list[TaskComment])
    def get_task_comments(task_id: str, principal: Principal = Depends(authenticate)) -> list[TaskComment]:
        require_scope(principal, "tasks:comment")
        task_for_principal(task_id, principal)
        return [_comment_record(row) for row in db.list_task_comments(task_id, principal.tenant_id)]

    @app.post("/tasks/{task_id}/comments", response_model=TaskComment, status_code=201)
    def add_task_comment(task_id: str, request: TaskCommentRequest, principal: Principal = Depends(authenticate)) -> TaskComment:
        require_scope(principal, "tasks:comment")
        task_for_principal(task_id, principal)
        row = db.add_task_comment(task_id, request.body, principal.actor, principal.tenant_id)
        return _comment_record(row)

    @app.get("/tasks/{task_id}/notices", response_model=list[NoticeRecord])
    def get_notices(task_id: str, principal: Principal = Depends(authenticate)) -> list[NoticeRecord]:
        require_scope(principal, "notices:read")
        task_for_principal(task_id, principal)
        return [_notice_record(row) for row in db.list_notices(task_id, principal.tenant_id)]

    @app.post("/tasks/{task_id}/notices", response_model=NoticeRecord, status_code=201)
    def create_notice(task_id: str, request: NoticeCreateRequest, principal: Principal = Depends(authenticate)) -> NoticeRecord:
        require_scope(principal, "notices:create")
        task_for_principal(task_id, principal)
        if request.delivery_method == "email" and "@" not in request.recipient:
            raise HTTPException(status_code=422, detail="Email notice recipient must be an email address.")
        try:
            row = db.create_notice(task_id, principal.tenant_id, principal.actor, request.subject, request.body, request.recipient, request.delivery_method)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _notice_record(row)

    @app.post("/notices/{notice_id}/approve", response_model=NoticeRecord)
    def approve_notice(notice_id: str, principal: Principal = Depends(authenticate)) -> NoticeRecord:
        require_scope(principal, "notices:approve")
        try:
            row = db.approve_notice(notice_id, principal.tenant_id, principal.actor)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not row:
            raise HTTPException(status_code=404, detail="Notice not found")
        return _notice_record(row)

    @app.post("/notices/{notice_id}/dispatch", response_model=NoticeRecord)
    def dispatch_notice(notice_id: str, request: NoticeDispatchRequest, principal: Principal = Depends(authenticate)) -> NoticeRecord:
        require_scope(principal, "notices:dispatch")
        sent_at = request.sent_at or datetime.now(timezone.utc)
        if sent_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="sent_at must include a timezone.")
        try:
            row = db.dispatch_notice(notice_id, principal.tenant_id, principal.actor, sent_at, request.delivery_reference, request.note)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not row:
            raise HTTPException(status_code=404, detail="Notice not found")
        return _notice_record(row)

    @app.post("/notices/{notice_id}/delivery", response_model=NoticeRecord)
    def mark_notice_delivery(notice_id: str, request: NoticeDeliveryRequest, principal: Principal = Depends(authenticate)) -> NoticeRecord:
        require_scope(principal, "notices:dispatch")
        delivered_at = request.delivered_at or datetime.now(timezone.utc)
        if delivered_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="delivered_at must include a timezone.")
        try:
            row = db.mark_notice_delivered(notice_id, principal.tenant_id, principal.actor, delivered_at, request.delivery_reference, request.evidence_note)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not row:
            raise HTTPException(status_code=404, detail="Notice not found")
        return _notice_record(row)

    return app


def _contract_record(row) -> ContractRecord:
    return ContractRecord(
        id=row["id"], filename=row["filename"], status=row["status"], extraction_provider=row["provider"],
        extracted=ContractData.model_validate_json(row["extracted_json"]),
        confirmed=ContractData.model_validate_json(row["confirmed_json"]) if row["confirmed_json"] else None,
        created_at=row["created_at"], confirmed_at=row["confirmed_at"],
        tenant_id=row["tenant_id"],
    )


def _audit_record(row) -> AuditEvent:
    import json

    return AuditEvent(id=row["id"], contract_id=row["contract_id"], event_type=row["event_type"], actor=row["actor"], details=json.loads(row["details_json"]), created_at=row["created_at"])


def _feedback_record(row) -> dict:
    return {
        "id": row["id"], "contract_id": row["contract_id"], "tenant_id": row["tenant_id"],
        "provider": row["extraction_provider"], "status": row["status"],
        "proposed": json.loads(row["proposed_json"]), "reviewed": json.loads(row["reviewed_json"]),
        "corrections": json.loads(row["corrections_json"]),
        "evaluation_case": json.loads(row["evaluation_case_json"]) if row["evaluation_case_json"] else None,
        "approval_reference": row["approval_reference"], "clause_categories": json.loads(row["clause_categories_json"]) if row["clause_categories_json"] else [],
        "captured_by": row["captured_by"], "captured_at": row["captured_at"],
        "approved_by": row["approved_by"], "approved_at": row["approved_at"],
    }


def _task_record(row) -> ReminderTask:
    return ReminderTask(
        id=row["id"], contract_id=row["contract_id"], title=row["title"], owner_name=row["owner_name"], owner_email=row["owner_email"],
        due_date=row["due_date"], expiration_date=row["expiration_date"], status=row["status"],
        reminder_sent_at=row["reminder_sent_at"], escalated_at=row["escalated_at"], created_at=row["created_at"],
        tenant_id=row["tenant_id"],
        notice_day_type=row["notice_day_type"], notice_timezone=row["notice_timezone"],
        notice_holidays=json.loads(row["notice_holidays_json"]),
        workflow_state=row["workflow_state"],
    )


def _comment_record(row) -> TaskComment:
    return TaskComment(id=row["id"], task_id=row["task_id"], actor=row["actor"], body=row["body"], created_at=row["created_at"])


def _notice_record(row) -> NoticeRecord:
    return NoticeRecord(
        id=row["id"], task_id=row["task_id"], status=row["status"], subject=row["subject"],
        body=row["body"], recipient=row["recipient"], delivery_method=row["delivery_method"],
        created_by=row["created_by"], created_at=row["created_at"], approved_by=row["approved_by"],
        approved_at=row["approved_at"], dispatched_by=row["dispatched_by"], dispatched_at=row["dispatched_at"],
        delivered_at=row["delivered_at"], delivery_reference=row["delivery_reference"], delivery_note=row["delivery_note"],
    )


def _secure_cookies() -> bool:
    return os.getenv("RADAR_COOKIE_SECURE", "true").strip().casefold() not in {"0", "false", "no"}


def _webhook_tenant_id() -> str:
    return os.getenv("DOCUMENT_SOURCE_WEBHOOK_TENANT_ID", "default").strip() or "default"


_OPERATIONS_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Operations</title>
<style>
:root{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{background:#10243a;color:#fff;padding:18px 26px;display:flex;justify-content:space-between;align-items:center}header h1{font-size:20px;margin:0}.bar{padding:12px 26px;background:#fff;border-bottom:1px solid #dbe2ea;display:flex;gap:9px;align-items:center;flex-wrap:wrap}.bar input{flex:1;max-width:500px;padding:8px;border:1px solid #cbd5e1;border-radius:6px}.bar a{color:#1677c8}.bar button,.refresh{border:0;border-radius:6px;padding:8px 12px;font:inherit;font-weight:650;cursor:pointer;background:#1677c8;color:#fff}.wrap{max-width:1400px;margin:auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(5,minmax(130px,1fr));gap:12px}.card,.panel{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:16px}.card span{display:block;color:#667085;font-size:13px}.card strong{display:block;font-size:26px;margin-top:3px}.panel{margin-top:15px}.panel h2{font-size:16px;margin:0 0 12px}.alerts{display:grid;gap:8px}.alert{padding:10px 12px;border-left:4px solid #d92d20;background:#fff2f0;border-radius:5px}.alert.warning{border-color:#dc6803;background:#fffaeb}.alert.ok{border-color:#039855;background:#ecfdf3}.columns{display:grid;grid-template-columns:1fr 1fr;gap:14px}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:8px;border-bottom:1px solid #e4e7ec;vertical-align:top}th{color:#667085}.error{color:#b42318}.muted{color:#667085}.healthy{color:#039855}.degraded{color:#b42318}@media(max-width:850px){.grid{grid-template-columns:repeat(2,1fr)}.columns{grid-template-columns:1fr}.wrap{padding:12px}}
</style></head><body>
<header><h1>◈ Renewal Radar / operations</h1><span id="health" class="muted">Not connected</span></header>
<div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token"><button onclick="refresh()">Refresh</button><a href="/auth/oidc/login">Sign in with SSO</a><a href="/review">Extraction review</a><a href="/tasks/inbox">Task inbox</a><label>Stuck after <select id="threshold"><option value="15">15 min</option><option value="30">30 min</option><option value="60">60 min</option></select></label></div>
<main class="wrap"><div id="notice" class="error" role="status"></div><section class="grid"><div class="card"><span>Queued jobs</span><strong id="queued">—</strong></div><div class="card"><span>Running jobs</span><strong id="running">—</strong></div><div class="card"><span>Dead letters</span><strong id="dead">—</strong></div><div class="card"><span>Retrying</span><strong id="retrying">—</strong></div><div class="card"><span>Failed notifications · 1h</span><strong id="failed">—</strong></div></section>
<section class="panel"><h2>Alerts</h2><div id="alerts" class="muted">Connect to load operational health.</div></section><div class="columns"><section class="panel"><h2>Recent jobs · includes calendar syncs</h2><div id="jobs" class="tablewrap muted">No data loaded.</div></section><section class="panel"><h2>Notification delivery attempts</h2><div id="deliveries" class="tablewrap muted">No data loaded.</div></section></div><section class="panel"><h2>Notification channels</h2><div id="channels" class="tablewrap muted">No data loaded.</div></section></main>
<script>
const $=id=>document.getElementById(id);function auth(){const t=$('token').value.trim()||sessionStorage.getItem('radar-token')||'';return t?{Authorization:'Bearer '+t}:{}}async function request(path){const r=await fetch(path,{headers:auth()});if(!r.ok){let e={};try{e=await r.json()}catch{}throw Error(e.detail||r.status)}return r.json()}function table(headers,rows){const t=document.createElement('table'),thead=document.createElement('thead'),tr=document.createElement('tr');headers.forEach(h=>{const th=document.createElement('th');th.textContent=h;tr.append(th)});thead.append(tr);t.append(thead);const body=document.createElement('tbody');for(const values of rows){const row=document.createElement('tr');values.forEach(v=>{const td=document.createElement('td');td.textContent=v===null||v===undefined?'—':String(v);row.append(td)});body.append(row)}t.append(body);return t}function fillTable(id,headers,rows){const el=$(id);el.replaceChildren();if(!rows.length){el.textContent='No activity recorded.';el.className='muted';return}el.className='tablewrap';el.append(table(headers,rows))}async function refresh(){try{if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());const d=await request('/operations/health?stuck_after_minutes='+$('threshold').value);$('notice').textContent='';$('health').textContent=d.status+' · updated '+new Date(d.generated_at).toLocaleString();$('health').className=d.status;for(const key of ['queued','running','dead'])$(key).textContent=d.jobs.by_status[key]||0;$('retrying').textContent=d.jobs.retrying;$('failed').textContent=d.notifications.failed_last_hour;const alerts=$('alerts');alerts.replaceChildren();if(!d.alerts.length){const ok=document.createElement('div');ok.className='alert ok';ok.textContent='No stuck jobs, dead letters, or repeated notification failures detected.';alerts.append(ok)}else{for(const a of d.alerts){const item=document.createElement('div');item.className='alert'+(a.severity==='warning'?' warning':'');item.textContent=a.message;alerts.append(item)}}fillTable('jobs',['Type','State','Attempts','Updated','Outcome','Error'],d.jobs.recent.map(j=>[j.job_type,j.status,j.attempts+'/'+j.max_attempts,new Date(j.updated_at).toLocaleString(),j.result_json?JSON.stringify(JSON.parse(j.result_json)):'',j.last_error]));fillTable('deliveries',['Task','Type','Channel','State','Duration','Attempted','Error'],d.notifications.recent.map(n=>[n.task_id,n.notification_type,n.channel,n.status,n.duration_ms+' ms',new Date(n.attempted_at).toLocaleString(),n.error_message]));fillTable('channels',['Channel','Kind','State','Count'],d.notifications.by_channel.map(x=>[x.channel,x.notification_type,x.status,x.count]))}catch(e){$('health').textContent='Unavailable';$('health').className='degraded';$('notice').textContent='Could not load operations: '+e.message}}const saved=sessionStorage.getItem('radar-token');if(saved)$('token').value=saved;refresh();
</script></body></html>'''


_REVIEWER_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Review</title>
<style>
:root{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{background:#10243a;color:white;padding:20px 28px;display:flex;justify-content:space-between;align-items:center}header h1{font-size:20px;margin:0}.bar{padding:14px 28px;background:#fff;border-bottom:1px solid #dbe2ea;display:flex;gap:8px}.shell{display:grid;grid-template-columns:300px 1fr;min-height:calc(100vh - 110px)}aside{padding:18px;border-right:1px solid #dbe2ea;background:white}.item{display:block;width:100%;text-align:left;background:#fff;border:1px solid #dbe2ea;border-radius:8px;padding:12px;margin:0 0 9px;cursor:pointer}.item:hover,.item.selected{border-color:#1677c8;background:#f2f8ff}.item small{display:block;color:#64748b;margin-top:3px}main{padding:24px;display:grid;grid-template-columns:minmax(320px,1fr) minmax(320px,1fr);gap:20px}.panel{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:18px;min-width:0}.panel h2{font-size:17px;margin:0 0 14px}.field{margin:0 0 11px}.field label{display:block;font-size:12px;color:#526174;font-weight:650;margin-bottom:3px}.field input,.field select{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font:inherit}.source{white-space:pre-wrap;max-height:68vh;overflow:auto;background:#f8fafc;padding:14px;border-radius:7px;font:13px/1.6 ui-monospace,monospace}.evidence{font-size:13px;border-left:3px solid #22a06b;padding:6px 10px;margin:8px 0;background:#f4fbf7}.actions{display:flex;gap:9px;margin-top:16px}.actions button,.bar button{border:0;border-radius:6px;padding:9px 14px;font:inherit;font-weight:650;cursor:pointer}.primary{background:#1677c8;color:white}.danger{background:#fff0ef;color:#b42318}.muted{color:#667085}.status{margin-left:auto;color:#9dd8ff}#notice{margin:0;padding:0 28px;color:#b42318}.empty{padding:32px;color:#667085;text-align:center}@media(max-width:850px){.shell{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid #dbe2ea}main{grid-template-columns:1fr;padding:14px}}
</style></head><body>
<header><h1>◈ Renewal Radar <span class="muted" style="color:#b7c7d9">/ contract review</span></h1><span id="who" class="status">Not connected</span></header>
<div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token" style="flex:1;max-width:520px;padding:8px;border:1px solid #cbd5e1;border-radius:6px"><button class="primary" onclick="connect()">Connect</button><a href="/auth/oidc/login" style="align-self:center;color:#1677c8">Sign in with SSO</a><a href="/tasks/inbox" style="align-self:center;color:#1677c8">Renewal task inbox →</a><a href="/operations" style="align-self:center;color:#1677c8">Operations →</a><button onclick="disconnect()">Disconnect</button></div><p id="notice" role="status"></p>
<div class="shell"><aside><h2>Pending review</h2><div id="queue" class="muted">Connect to load contracts.</div></aside><main><section class="panel"><h2>Extracted terms</h2><div id="fields" class="muted">Choose a contract to inspect its proposal.</div><div id="evidence"></div><div class="actions"><button class="primary" onclick="confirmContract()">Confirm terms</button><button class="danger" onclick="rejectContract()">Reject</button></div></section><section class="panel"><h2>Contract text</h2><div id="source" class="source muted">Source text appears here.</div></section></main></div>
<script>
const keys=['contract','start_date','expiration_date','renewal_notice_days','notice_day_type','notice_timezone','notice_holidays','notice_method','notice_recipient','auto_renew','termination_notice','owner_name','owner_email','supersedes_contract_id'];let selected=null,contracts=[];const $=id=>document.getElementById(id);function say(s){$('notice').textContent=s}function auth(){let t=$('token').value.trim();return t?{Authorization:'Bearer '+t}:{}}function disconnect(){sessionStorage.removeItem('radar-token');$('token').value='';fetch('/auth/logout',{method:'POST'});$('who').textContent='Not connected';$('queue').textContent='Connect to load contracts.';selected=null;contracts=[]}async function connect(){if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());try{let r=await fetch('/contracts?status=pending_review',{headers:auth()});if(!r.ok)throw Error((await r.json()).detail||r.status);contracts=await r.json();$('who').textContent='Connected · '+contracts.length+' awaiting review';say('');renderQueue();if(contracts.length)openContract(contracts[0].id)}catch(e){say('Could not connect: '+e.message)}}function renderQueue(){let q=$('queue');q.replaceChildren();if(!contracts.length){q.textContent='No contracts awaiting review.';return}contracts.forEach(c=>{let b=document.createElement('button');b.className='item'+(selected===c.id?' selected':'');b.onclick=()=>openContract(c.id);let title=document.createElement('strong');title.textContent=c.extracted.contract||c.filename;let meta=document.createElement('small');meta.textContent=c.filename+' · '+new Date(c.created_at).toLocaleDateString();b.append(title,meta);q.append(b)})}async function openContract(id){selected=id;renderQueue();let c=contracts.find(x=>x.id===id);let data=c.extracted;let fields=$('fields');fields.replaceChildren();keys.forEach(k=>{let wrap=document.createElement('div');wrap.className='field';let label=document.createElement('label');label.htmlFor='f-'+k;label.textContent=k.replaceAll('_',' ')+(k==='notice_holidays'?' (comma-separated ISO dates)':k==='supersedes_contract_id'?' (active contract ID to replace)':'');let input;if(k==='notice_day_type'||k==='auto_renew'){input=document.createElement('select');let options=k==='auto_renew'?[['','Unknown'],['true','Yes'],['false','No']]:[['calendar','Calendar days'],['business','Business days']];options.forEach(([v,t])=>{let o=document.createElement('option');o.value=v;o.textContent=t;input.append(o)})}else{input=document.createElement('input');input.type=k.endsWith('_date')?'date':k==='renewal_notice_days'?'number':'text';if(k==='renewal_notice_days')input.min='0'}input.id='f-'+k;let value=data[k];input.value=value===null||value===undefined?'':k==='auto_renew'?String(value):k==='notice_holidays'?value.join(', '):String(value);wrap.append(label,input);fields.append(wrap)});let e=$('evidence');e.replaceChildren();(data.evidence||[]).forEach(item=>{let div=document.createElement('div');div.className='evidence';div.textContent=item.field+': “'+item.quote+'” · confidence '+Math.round(item.confidence*100)+'%';e.append(div)});$('source').textContent='Loading…';try{let r=await fetch('/contracts/'+id+'/source',{headers:auth()});if(!r.ok)throw Error((await r.json()).detail||r.status);$('source').textContent=(await r.json()).text}catch(err){$('source').textContent='Source unavailable: '+err.message}}function formData(){let x={};keys.forEach(k=>{let v=$('f-'+k).value;x[k]=k==='notice_holidays'?v.split(',').map(x=>x.trim()).filter(Boolean):v===''?null:k==='renewal_notice_days'?Number(v):k==='auto_renew'?v==='true'?true:v==='false'?false:null:v});x.evidence=contracts.find(c=>c.id===selected).extracted.evidence;return x}async function action(path,body){if(!selected)return say('Select a contract first.');try{let r=await fetch('/contracts/'+selected+'/'+path,{method:'POST',headers:{...auth(),'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});if(!r.ok)throw Error((await r.json()).detail||r.status);say(path==='confirm'?'Contract confirmed and renewal task created.':'Contract rejected.');contracts=contracts.filter(c=>c.id!==selected);selected=null;renderQueue();$('fields').textContent='Choose a contract to inspect its proposal.';$('source').textContent='Source text appears here.';$('evidence').replaceChildren();if(contracts.length)openContract(contracts[0].id)}catch(e){say('Could not '+path+': '+e.message)}}function confirmContract(){action('confirm',{contract:formData()})}function rejectContract(){action('reject')}const saved=sessionStorage.getItem('radar-token');if(saved){$('token').value=saved;connect()}else{connect()}
</script></body></html>'''


_TASK_INBOX_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Task inbox</title>
<style>
:root{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{background:#10243a;color:#fff;padding:18px 26px;display:flex;justify-content:space-between;align-items:center}header h1{font-size:20px;margin:0}a{color:#1677c8}.bar{padding:12px 26px;background:#fff;border-bottom:1px solid #dbe2ea;display:flex;gap:9px;align-items:center;flex-wrap:wrap}.bar input{flex:1;max-width:500px;padding:8px;border:1px solid #cbd5e1;border-radius:6px}.layout{display:grid;grid-template-columns:minmax(280px,360px) 1fr;min-height:calc(100vh - 108px)}aside{padding:18px;background:#fff;border-right:1px solid #dbe2ea}.filters{display:grid;gap:9px;margin-bottom:14px}.filters select{padding:8px;border:1px solid #cbd5e1;border-radius:6px;font:inherit}.filters label{font-size:13px;color:#526174}.task{display:block;width:100%;text-align:left;border:1px solid #dbe2ea;border-radius:8px;padding:11px;margin-bottom:8px;background:#fff;cursor:pointer}.task.selected,.task:hover{border-color:#1677c8;background:#f2f8ff}.task strong,.task small{display:block}.task small{color:#64748b;margin-top:3px}.pill{display:inline-block;border-radius:99px;padding:2px 8px;background:#e9eff6;font-size:12px}.overdue{color:#b42318;background:#fff0ef}.main{padding:20px;display:grid;grid-template-columns:minmax(280px,1.05fr) minmax(280px,.95fr);gap:14px;align-content:start}.panel{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:17px;min-width:0}.panel h2{font-size:16px;margin:0 0 12px}.panel h3{font-size:14px;margin:16px 0 6px}.panel input,.panel select,.panel textarea{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font:inherit;margin:4px 0 9px}.panel textarea{min-height:75px;resize:vertical}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.actions{display:flex;gap:8px;flex-wrap:wrap}button{border:0;border-radius:6px;padding:8px 12px;font:inherit;font-weight:650;cursor:pointer}.primary{background:#1677c8;color:white}.secondary{background:#e9eff6;color:#182230}.event{border-left:3px solid #9bb3ca;padding:6px 9px;margin:7px 0;background:#f8fafc;font-size:13px}.muted{color:#667085}.status{color:#a9d8f5}.empty{padding:24px;color:#667085;text-align:center}#notice{padding:6px 26px;color:#b42318;margin:0}.check{display:flex;align-items:center;gap:7px}.check input{width:auto;margin:0}@media(max-width:850px){.layout{grid-template-columns:1fr}aside{border:0;border-bottom:1px solid #dbe2ea}.main{grid-template-columns:1fr;padding:12px}}
</style></head><body>
<header><h1>◈ Renewal Radar / task inbox</h1><span id="who" class="status">Not connected</span></header>
<div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token"><button class="primary" onclick="loadTasks()">Connect / refresh</button><a href="/auth/oidc/login">Sign in with SSO</a><a href="/review">Extraction review</a><a href="/operations">Operations</a></div><p id="notice" role="status"></p>
<div class="layout"><aside><div class="filters"><label>Workflow state<select id="state"><option value="">All open work</option><option>review</option><option>needs_changes</option><option>pending_approval</option><option>notice_in_progress</option></select></label><label class="check"><input id="due" type="checkbox">Due or overdue today</label><label class="check"><input id="unassigned" type="checkbox">Unassigned only</label><button class="secondary" onclick="loadTasks()">Apply filters</button></div><div id="queue" class="muted">Connect to load renewal tasks.</div></aside>
<main class="main"><section class="panel"><h2 id="title">Select a task</h2><div id="summary" class="muted">Review ownership, deadlines, and workflow state.</div><div id="controls" hidden><h3>Assignment</h3><div class="grid"><input id="ownerName" placeholder="Owner name"><input id="ownerEmail" type="email" placeholder="Owner email"></div><button class="secondary" onclick="assignTask()">Save assignment</button><h3>Workflow</h3><div class="grid"><select id="nextState"><option value="review">In review</option><option value="needs_changes">Needs changes</option><option value="pending_approval">Pending approval</option><option value="notice_in_progress">Notice in progress</option><option value="renewed">Renewed</option><option value="terminated">Terminated</option><option value="cancelled">Cancelled</option></select><input id="transitionComment" placeholder="Decision note (optional)"></div><div class="actions"><button class="primary" onclick="transitionTask()">Update workflow</button><button class="secondary" onclick="resolveTask()">Resolve task</button></div><h3>Comments</h3><div id="comments"></div><textarea id="commentBody" placeholder="Add a team comment"></textarea><button class="secondary" onclick="addComment()">Add comment</button></div></section>
<section class="panel"><h2>Task history</h2><div id="history" class="muted">Audit events appear when a task is selected.</div><h3>Notice drafts</h3><div id="notices" class="muted">No task selected.</div></section></main></div>
<script>
let tasks=[],selected=null;const $=id=>document.getElementById(id);function say(s){$('notice').textContent=s}function auth(){const t=$('token').value.trim()||sessionStorage.getItem('radar-token')||'';return t?{Authorization:'Bearer '+t}:{}}async function api(path,options={}){const r=await fetch(path,{...options,headers:{...auth(),...(options.headers||{})}});if(!r.ok){let e={};try{e=await r.json()}catch{}throw Error(e.detail||r.status)}return r.status===204?null:r.json()}function localDate(){const d=new Date();return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,10)}async function loadTasks(){try{if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());const q=new URLSearchParams({status:'open'});if($('state').value)q.set('workflow_state',$('state').value);if($('due').checked)q.set('due_before',localDate());if($('unassigned').checked)q.set('unassigned','true');tasks=await api('/tasks?'+q);$('who').textContent=tasks.length+' open task'+(tasks.length===1?'':'s');say('');renderQueue();if(selected&&tasks.some(t=>t.id===selected))openTask(selected);else if(tasks.length)openTask(tasks[0].id);else clearTask()}catch(e){say('Could not load tasks: '+e.message)}}function renderQueue(){const q=$('queue');q.replaceChildren();if(!tasks.length){q.textContent='No tasks match these filters.';q.className='empty';return}q.className='';for(const t of tasks){const b=document.createElement('button');b.className='task'+(t.id===selected?' selected':'');b.onclick=()=>openTask(t.id);const s=document.createElement('strong');s.textContent=t.title;const d=document.createElement('small');d.textContent='Due '+t.due_date+' · '+(t.owner_name||t.owner_email||'Unassigned');const p=document.createElement('span');p.className='pill'+(t.due_date<localDate()?' overdue':'');p.textContent=t.workflow_state.replaceAll('_',' ');b.append(s,d,p);q.append(b)}}function clearTask(){selected=null;$('title').textContent='Select a task';$('summary').textContent='No open tasks match these filters.';$('controls').hidden=true;$('history').textContent='';$('notices').textContent='';renderQueue()}async function openTask(id){selected=id;renderQueue();const t=tasks.find(x=>x.id===id);$('title').textContent=t.title;$('summary').textContent='Notice deadline '+t.due_date+' · Contract expiration '+t.expiration_date+' · '+(t.owner_email||'Unassigned');$('controls').hidden=false;$('ownerName').value=t.owner_name||'';$('ownerEmail').value=t.owner_email||'';$('nextState').value=t.workflow_state;$('transitionComment').value='';try{const [comments,notices]=await Promise.all([api('/tasks/'+id+'/comments'),api('/tasks/'+id+'/notices')]);let events=[];try{events=await api('/contracts/'+t.contract_id+'/audit')}catch{}renderEvents('comments',comments.map(c=>({title:c.actor+' · '+new Date(c.created_at).toLocaleString(),detail:c.body})));renderEvents('history',events.slice(-20).reverse().map(e=>({title:e.event_type.replaceAll('.',' · ')+' · '+e.actor+' · '+new Date(e.created_at).toLocaleString(),detail:JSON.stringify(e.details)})));renderEvents('notices',notices.map(n=>({title:n.status+' · '+n.subject,detail:n.recipient+' · '+n.delivery_method})));}catch(e){say('Could not load task history: '+e.message)}}function renderEvents(id,events){const el=$(id);el.replaceChildren();if(!events.length){el.textContent='No history yet.';el.className='muted';return}el.className='';for(const event of events){const div=document.createElement('div');div.className='event';const strong=document.createElement('strong');strong.textContent=event.title;const detail=document.createElement('div');detail.textContent=event.detail;div.append(strong,detail);el.append(div)}}async function assignTask(){try{await api('/tasks/'+selected+'/assign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({owner_name:$('ownerName').value||null,owner_email:$('ownerEmail').value||null})});say('Assignment saved.');await loadTasks()}catch(e){say('Could not assign task: '+e.message)}}async function transitionTask(){try{await api('/tasks/'+selected+'/transition',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workflow_state:$('nextState').value,comment:$('transitionComment').value||null})});say('Workflow updated.');selected=null;await loadTasks()}catch(e){say('Could not update workflow: '+e.message)}}async function resolveTask(){if(!confirm('Resolve this renewal task?'))return;try{await api('/tasks/'+selected+'/resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({comment:$('transitionComment').value||null})});say('Task resolved.');selected=null;await loadTasks()}catch(e){say('Could not resolve task: '+e.message)}}async function addComment(){try{await api('/tasks/'+selected+'/comments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({body:$('commentBody').value})});$('commentBody').value='';say('Comment added.');await openTask(selected)}catch(e){say('Could not add comment: '+e.message)}}const saved=sessionStorage.getItem('radar-token');if(saved)$('token').value=saved;loadTasks();
</script></body></html>'''


app = create_app()
