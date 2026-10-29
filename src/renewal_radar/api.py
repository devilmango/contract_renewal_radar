from __future__ import annotations

import json
import hashlib
import os
import secrets
import base64
import binascii
import csv
import io
import zipfile
from xml.sax.saxutils import escape
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
from .evaluation import _evidence_supports, evaluate_extractor, load_cases, validate_approved_deidentified_text
from .ingestion import ingest_pdf_bytes
from .integrations import access_token, decrypt_credentials, encrypt_credentials, public_connection
from .extractor import get_extractor
from .policies import default_escalation_policy
from .schemas import (
    AccessEvent, AuditEvent, ConfirmationRequest, ContractData, ContractRecord, EscalationPolicy, JobRecord,
    LegalHoldRecord, LegalHoldRequest, NoticeCreateRequest, NoticeDeliveryRequest,
    NoticeDispatchRequest, NoticeRecord, ReminderTask, ResolveTaskRequest, TaskAssignmentRequest,
    EvaluationFeedbackApprovalRequest, TaskComment, TaskCommentRequest, TaskTransitionRequest,
    IntegrationConnectionRequest,
    RenewalOutcomeRequest, SupplierCreateRequest, ObligationCreateRequest, RenewalForecastRequest,
    ObligationScheduleUpdateRequest, ObligationRescheduleRequest,
    ObligationEvidenceRequest, ObligationComplianceRequest, ObligationEvidenceReviewRequest,
    ApprovalPlaybookRequest, PlaybookApplyRequest, PlaybookApprovalRequest, PlaybookChecklistRequest,
    BulkPortfolioRequest, SupplierRiskUpdateRequest,
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

    def obligation_for_principal(obligation_id: str, principal: Principal):
        row = db.get_contract_obligation(obligation_id, principal.tenant_id)
        if not row:
            raise HTTPException(status_code=404, detail="Obligation not found")
        owner_only = "owner" in principal.roles and not (principal.is_admin or "reviewer" in principal.roles)
        if owner_only and (not principal.email or not row["owner_email"] or row["owner_email"].casefold() != principal.email.casefold()):
            raise HTTPException(status_code=403, detail="Owners can access only their assigned obligations.")
        return row

    def enqueue_calendar_sync_if_configured(tenant_id: str) -> None:
        if db.get_integration_connection(tenant_id, "calendar") or os.getenv("CALENDAR_PROVIDER", "").strip().casefold() in {"microsoft", "google"}:
            enqueue_calendar_sync(db, tenant_id)

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

    @app.get("/integrations", response_class=HTMLResponse, include_in_schema=False)
    def integrations_settings() -> HTMLResponse:
        return HTMLResponse(_INTEGRATIONS_HTML)

    @app.get("/suppliers/workspace", response_class=HTMLResponse, include_in_schema=False)
    def suppliers_workspace() -> HTMLResponse:
        return HTMLResponse(_SUPPLIERS_HTML)

    @app.get("/obligations/workspace", response_class=HTMLResponse, include_in_schema=False)
    def obligations_workspace() -> HTMLResponse:
        return HTMLResponse(_OBLIGATIONS_HTML)

    @app.get("/compliance/workspace", response_class=HTMLResponse, include_in_schema=False)
    def compliance_workspace() -> HTMLResponse:
        return HTMLResponse(_COMPLIANCE_HTML)

    @app.get("/approvals", response_class=HTMLResponse, include_in_schema=False)
    def approvals_workspace() -> HTMLResponse:
        return HTMLResponse(_APPROVALS_HTML)

    @app.get("/portfolio/workspace", response_class=HTMLResponse, include_in_schema=False)
    def portfolio_workspace() -> HTMLResponse:
        return HTMLResponse(_PORTFOLIO_HTML)

    @app.get("/renewal-forecast", response_class=HTMLResponse, include_in_schema=False)
    def renewal_forecast_workspace() -> HTMLResponse:
        return HTMLResponse(_FORECAST_HTML)

    @app.get("/renewal-risk", response_class=HTMLResponse, include_in_schema=False)
    def renewal_risk_dashboard() -> HTMLResponse:
        return HTMLResponse(_RENEWAL_RISK_HTML)

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

    @app.get("/contracts/{contract_id}/versions")
    def get_contract_versions(contract_id: str, principal: Principal = Depends(authenticate)) -> dict:
        """Show amendment lineage and deterministic term changes for this contract."""
        require_scope(principal, "contracts:read")
        if not db.get_contract(contract_id, principal.tenant_id):
            raise HTTPException(status_code=404, detail="Contract not found")
        return {"contract_id": contract_id, "versions": db.list_contract_versions(contract_id, principal.tenant_id)}

    @app.get("/contracts/{contract_id}/amendment-candidates")
    def get_amendment_candidates(contract_id: str, principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "contracts:review")
        if not db.get_contract(contract_id, principal.tenant_id):
            raise HTTPException(status_code=404, detail="Contract not found")
        candidates = db.amendment_candidates(contract_id, principal.tenant_id)
        automatic_match = next((candidate for candidate in candidates if candidate["automatic_match"]), None)
        return {"contract_id": contract_id, "candidates": candidates, "automatic_match": automatic_match}

    @app.post("/contracts/{contract_id}/amendment-preview")
    def preview_amendment(
        contract_id: str, request: ConfirmationRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "contracts:review")
        try:
            return db.preview_contract_confirmation(contract_id, request.contract, principal.tenant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Contract not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
        enqueue_calendar_sync_if_configured(principal.tenant_id)
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
        kind: str | None = Query(default=None, pattern="^(renewal|obligation)$"),
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
            workflow_state=workflow_state, due_before=due_before, unassigned=unassigned, kind=kind,
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
        enqueue_calendar_sync_if_configured(principal.tenant_id)
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
        for connection in snapshot["integrations"]["connections"]:
            if connection["status"] == "error":
                alerts.append({
                    "severity": "critical", "code": "integration_error", "integration": connection["integration_name"],
                    "message": f"{connection['integration_name'].capitalize()} integration check failed: {connection['last_error'] or 'provider unavailable'}",
                })
        snapshot["alerts"] = alerts
        snapshot["status"] = "degraded" if alerts else "healthy"
        return snapshot

    @app.get("/renewal-risk/summary")
    def renewal_risk_summary(
        as_of: date | None = Query(default=None), principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "contracts:read")
        require_scope(principal, "tasks:read")
        return db.renewal_risk_snapshot(principal.tenant_id, as_of)

    @app.get("/renewal-outcomes/summary")
    def renewal_outcomes_summary(principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "contracts:read")
        return db.renewal_outcome_summary(principal.tenant_id)

    @app.get("/obligations")
    def list_obligations(
        status: str | None = Query(default=None, pattern="^(open|resolved)$"),
        principal: Principal = Depends(authenticate),
    ) -> list[dict]:
        require_scope(principal, "tasks:read")
        owner_only = "owner" in principal.roles and not (principal.is_admin or "reviewer" in principal.roles)
        if owner_only and not principal.email:
            raise HTTPException(status_code=403, detail="An email address is required for owner-scoped obligation access.")
        return db.list_contract_obligations(principal.tenant_id, status, principal.email if owner_only else None)

    @app.get("/obligations/{obligation_id}/compliance")
    def obligation_compliance_history(obligation_id: str, principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "tasks:read")
        obligation = obligation_for_principal(obligation_id, principal)
        return {"obligation_id": obligation_id, "history": db.obligation_compliance_history(obligation_id, principal.tenant_id),
                "evidence": db.list_obligation_evidence(obligation_id, principal.tenant_id)}

    @app.post("/obligations/{obligation_id}/evidence", status_code=201)
    def submit_obligation_evidence(
        obligation_id: str, request: ObligationEvidenceRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "tasks:resolve")
        obligation = obligation_for_principal(obligation_id, principal)
        try:
            raw = base64.b64decode(request.content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(status_code=422, detail="content_base64 must contain valid base64 data") from exc
        if not raw or len(raw) > 5 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Evidence files must be between 1 byte and 5 MB")
        if request.content_type == "application/pdf" and not raw.startswith(b"%PDF-"):
            raise HTTPException(status_code=422, detail="PDF evidence must have a valid PDF signature")
        if request.content_type == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(status_code=422, detail="PNG evidence must have a valid PNG signature")
        if request.content_type == "image/jpeg" and not raw.startswith(b"\xff\xd8\xff"):
            raise HTTPException(status_code=422, detail="JPEG evidence must have a valid JPEG signature")
        result = db.submit_obligation_evidence(
            obligation_id, principal.tenant_id, principal.actor, Path(request.filename).name,
            request.content_type, request.content_base64, hashlib.sha256(raw).hexdigest(), request.comment,
        )
        if not result:
            raise HTTPException(status_code=404, detail="Obligation not found")
        return result

    @app.get("/obligation-evidence/{evidence_id}/download")
    def download_obligation_evidence(evidence_id: str, principal: Principal = Depends(authenticate)) -> Response:
        require_scope(principal, "tasks:read")
        evidence = db.get_obligation_evidence_content(evidence_id, principal.tenant_id)
        if not evidence:
            raise HTTPException(status_code=404, detail="Evidence not found")
        obligation = obligation_for_principal(evidence["obligation_id"], principal)
        try:
            content = base64.b64decode(evidence["content_base64"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(status_code=500, detail="Stored evidence is invalid") from exc
        if not secrets.compare_digest(hashlib.sha256(content).hexdigest(), evidence["sha256"]):
            raise HTTPException(status_code=500, detail="Stored evidence checksum does not match")
        filename = Path(evidence["filename"]).name.encode("ascii", "ignore").decode("ascii")
        filename = "".join(character for character in filename if character.isprintable() and character not in '"\\') or "evidence"
        return Response(content, media_type=evidence["content_type"], headers={"Content-Disposition": f'attachment; filename="{filename}"', "X-Content-SHA256": evidence["sha256"]})

    @app.post("/obligations/{obligation_id}/compliance")
    def record_obligation_compliance(
        obligation_id: str, request: ObligationComplianceRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "tasks:resolve")
        obligation = obligation_for_principal(obligation_id, principal)
        try:
            result = db.record_obligation_compliance(obligation_id, principal.tenant_id, principal.actor, request.outcome, request.comment)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=404, detail="Obligation not found")
        return result

    @app.post("/obligation-evidence/{evidence_id}/review")
    def review_obligation_evidence(
        evidence_id: str, request: ObligationEvidenceReviewRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "obligations:signoff")
        try:
            result = db.review_obligation_evidence(evidence_id, principal.tenant_id, principal.actor, request.decision, request.comment)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=404, detail="Evidence not found")
        if request.decision == "approved":
            enqueue_calendar_sync_if_configured(principal.tenant_id)
        return result

    @app.patch("/obligations/{obligation_id}/schedule")
    def update_obligation_schedule(
        obligation_id: str, request: ObligationScheduleUpdateRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "contracts:review")
        try:
            result = db.update_obligation_schedule(
                obligation_id, principal.tenant_id, principal.actor,
                status=request.status, recurrence_interval_months=request.recurrence_interval_months,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=404, detail="Obligation not found")
        return result

    @app.post("/obligations/{obligation_id}/reschedule")
    def reschedule_obligation(
        obligation_id: str, request: ObligationRescheduleRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "contracts:review")
        try:
            result = db.reschedule_obligation(obligation_id, request.due_date, principal.tenant_id, principal.actor)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=404, detail="Obligation not found")
        return result

    @app.post("/contracts/{contract_id}/obligations", status_code=201)
    def create_obligation(
        contract_id: str, request: ObligationCreateRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "contracts:review")
        try:
            result = db.create_contract_obligation(
                contract_id, request.model_dump(mode="python"), request.owner_name,
                request.owner_email, principal.actor, principal.tenant_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=404, detail="Contract not found")
        return result

    @app.post("/renewal-forecast")
    def calculate_renewal_forecast(
        request: RenewalForecastRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "contracts:read")
        require_scope(principal, "contracts:review")
        require_scope(principal, "tasks:read")
        result = db.renewal_forecast(
            principal.tenant_id, request.start_date, request.end_date,
            request.renewal_increase_pct, request.renegotiation_savings_pct,
        )
        result["forecast_id"] = db.record_renewal_forecast(principal.tenant_id, result, principal.actor)
        return result

    @app.get("/renewal-forecast/accuracy")
    def renewal_forecast_accuracy(principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "contracts:read")
        return db.renewal_forecast_accuracy(principal.tenant_id)

    @app.post("/tasks/{task_id}/outcome")
    def record_renewal_outcome(
        task_id: str, request: RenewalOutcomeRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "tasks:workflow")
        if not (principal.is_admin or "reviewer" in principal.roles):
            raise HTTPException(status_code=403, detail="Only reviewers can record a final renewal outcome.")
        try:
            result = db.record_renewal_outcome(task_id, request.model_dump(mode="python"), principal.actor, principal.tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=404, detail="Task not found")
        enqueue_calendar_sync_if_configured(principal.tenant_id)
        return result

    @app.get("/suppliers")
    def list_suppliers(principal: Principal = Depends(authenticate)) -> list[dict]:
        require_scope(principal, "contracts:read")
        return db.list_suppliers(principal.tenant_id)

    @app.post("/suppliers", status_code=201)
    def create_supplier(request: SupplierCreateRequest, principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "contracts:review")
        try:
            return db.create_supplier(request.name, principal.actor, principal.tenant_id, request.risk_level)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/suppliers/{supplier_id}/contracts/{contract_id}")
    def assign_supplier_to_contract(supplier_id: str, contract_id: str, principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "contracts:review")
        try:
            row = db.assign_contract_supplier(contract_id, supplier_id, principal.actor, principal.tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        return row

    @app.patch("/suppliers/{supplier_id}/risk")
    def update_supplier_risk(supplier_id: str, request: SupplierRiskUpdateRequest, principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "contracts:review")
        result = db.update_supplier_risk(supplier_id, request.risk_level, principal.tenant_id, principal.actor)
        if not result:
            raise HTTPException(status_code=404, detail="Supplier not found")
        return result

    @app.get("/renewal-playbooks")
    def list_renewal_playbooks(principal: Principal = Depends(authenticate)) -> list[dict]:
        require_scope(principal, "contracts:read")
        return db.list_renewal_playbooks(principal.tenant_id)

    @app.get("/approvals/inbox")
    def approval_inbox(principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "obligations:signoff")
        result = db.approval_inbox(principal.tenant_id, max(1, int(os.getenv("APPROVAL_SLA_DAYS", "3"))))
        if not principal.is_admin:
            result["items"] = [item for item in result["items"] if item["type"] != "playbook_decision" or
                               (principal.email and principal.email.casefold() in {email.casefold() for email in item["reviewers"]})]
        return result

    @app.get("/renewal-playbooks/simulate")
    def simulate_renewal_playbooks(principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "playbooks:manage")
        return db.simulate_renewal_playbooks(principal.tenant_id)

    @app.get("/renewal-playbooks/{playbook_id}/history")
    def renewal_playbook_history(playbook_id: str, principal: Principal = Depends(authenticate)) -> list[dict]:
        require_scope(principal, "playbooks:manage")
        if not any(item["id"] == playbook_id for item in db.list_renewal_playbooks(principal.tenant_id, include_retired=True)):
            raise HTTPException(status_code=404, detail="Playbook not found")
        return db.renewal_playbook_history(playbook_id, principal.tenant_id)

    @app.post("/renewal-playbooks", status_code=201)
    def create_renewal_playbook(request: ApprovalPlaybookRequest, principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "playbooks:manage")
        try:
            return db.create_renewal_playbook(request.model_dump(mode="json"), principal.actor, principal.tenant_id)
        except Exception as exc:
            if "unique" in str(exc).casefold() or "duplicate" in str(exc).casefold():
                raise HTTPException(status_code=409, detail="A playbook with this name already exists") from exc
            raise

    @app.put("/renewal-playbooks/{playbook_id}")
    def edit_renewal_playbook(
        playbook_id: str, request: ApprovalPlaybookRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "playbooks:manage")
        try:
            result = db.edit_renewal_playbook(playbook_id, request.model_dump(mode="json"), principal.actor, principal.tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            if "unique" in str(exc).casefold() or "duplicate" in str(exc).casefold():
                raise HTTPException(status_code=409, detail="A playbook with this name already exists") from exc
            raise
        if not result:
            raise HTTPException(status_code=404, detail="Playbook not found")
        return result

    @app.post("/renewal-playbooks/{playbook_id}/retire")
    def retire_renewal_playbook(playbook_id: str, principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "playbooks:manage")
        try:
            result = db.edit_renewal_playbook(playbook_id, {}, principal.actor, principal.tenant_id, retire=True)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=404, detail="Playbook not found")
        return result

    @app.post("/tasks/{task_id}/playbooks/{playbook_id}")
    def apply_renewal_playbook(
        task_id: str, playbook_id: str, request: PlaybookApplyRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "playbooks:manage")
        if not db.get_task(task_id, principal.tenant_id):
            raise HTTPException(status_code=404, detail="Task not found")
        try:
            result = db.apply_renewal_playbook(playbook_id, task_id, principal.tenant_id, principal.actor, request.changed_clause_categories)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=404, detail="Playbook or task not found")
        return result

    @app.get("/tasks/{task_id}/playbook-review")
    def get_task_playbook_review(task_id: str, principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "tasks:read")
        task_for_principal(task_id, principal)
        result = db.task_playbook_review(task_id, principal.tenant_id)
        if not result:
            raise HTTPException(status_code=404, detail="No playbook has been applied to this task")
        return result

    @app.post("/tasks/{task_id}/playbook-review/approval")
    def record_playbook_approval(
        task_id: str, request: PlaybookApprovalRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "playbooks:manage")
        if not principal.email:
            raise HTTPException(status_code=403, detail="An email address is required to record an approver decision")
        try:
            result = db.update_playbook_approval(task_id, principal.tenant_id, principal.email, request.approved, principal.actor)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=404, detail="Playbook review not found")
        return result

    @app.patch("/tasks/{task_id}/playbook-review/checklist")
    def update_playbook_checklist(
        task_id: str, request: PlaybookChecklistRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "playbooks:manage")
        try:
            result = db.update_playbook_checklist(task_id, principal.tenant_id, request.index, request.completed, principal.actor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=404, detail="Playbook review not found")
        return result

    @app.post("/portfolio/bulk")
    def bulk_portfolio_action(request: BulkPortfolioRequest, principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "portfolio:bulk")
        try:
            return db.bulk_portfolio_action(
                request.action, request.ids, principal.tenant_id, principal.actor,
                owner_name=request.owner_name, owner_email=request.owner_email,
                workflow_state=request.workflow_state, supplier_id=request.supplier_id, comment=request.comment,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/portfolio/export")
    def export_portfolio(
        dataset: str = Query(pattern="^(contracts|tasks|suppliers|obligations)$"),
        format: str = Query(default="csv", pattern="^(csv|xlsx)$"),
        principal: Principal = Depends(authenticate),
    ) -> Response:
        require_scope(principal, "portfolio:export")
        rows = _portfolio_export_rows(db, dataset, principal.tenant_id)
        filename = f"renewal-radar-{dataset}.{format}"
        if format == "csv":
            content = _portfolio_csv(rows)
            return Response(content, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        content = _portfolio_xlsx(rows, dataset)
        return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/evaluations/runs", status_code=201)
    def run_provider_evaluation(principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "evaluation:run")
        candidates = [Path.cwd() / "evaluation" / "cases", Path(__file__).resolve().parents[2] / "evaluation" / "cases"]
        dataset_path = next((path for path in candidates if path.is_dir()), None)
        if dataset_path is None:
            raise HTTPException(status_code=503, detail="The redacted evaluation fixture set is not installed")
        cases = load_cases(dataset_path)
        fingerprint_payload = [{
            "id": case["id"], "text": case["contract_text"], "active_contract_text": case.get("active_contract_text"),
            "expected": case["expected"], "expected_changed_fields": case.get("expected_changed_fields", []),
            "expected_clause_categories": case.get("expected_clause_categories", []),
        } for case in cases]
        fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True).encode()).hexdigest()
        provider_reports = []
        providers = ["rules", "openai", "anthropic", "google", "mistral", "cohere", "xai"]
        for provider in providers:
            try:
                extractor = get_extractor(provider)
            except Exception:
                continue
            try:
                report = evaluate_extractor(extractor, cases)
            except Exception as exc:
                report = {"provider": provider, "model": getattr(extractor, "model", None), "cases_scored": len(cases), "errors": [{"error": str(exc)}],
                          "exact_case_match": {"accuracy": 0.0}, "field_evidence_support": {"accuracy": 0.0}}
            provider_reports.append(db.record_provider_evaluation(principal.tenant_id, provider, report, fingerprint, principal.actor))
        if not provider_reports:
            raise HTTPException(status_code=503, detail="No configured provider could be evaluated")
        return {"dataset_fingerprint": fingerprint, "runs": provider_reports}

    @app.get("/evaluations/drift")
    def provider_evaluation_drift(
        provider: str | None = Query(default=None, max_length=80), principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "contracts:read")
        history = db.provider_evaluation_history(principal.tenant_id, provider)
        alerts = [dict(run_id=row["id"], provider=row["provider"], created_at=row["created_at"], **alert)
                  for row in history for alert in row["alerts"]]
        return {"runs": history, "alerts": alerts, "threshold": {"metric_drop": 0.1, "comparison": "previous run on the identical fixture fingerprint"}}

    @app.get("/escalation-policy")
    def get_escalation_policy(principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "settings:read")
        saved = db.get_tenant_policy(principal.tenant_id)
        if saved:
            return {
                "tenant_id": principal.tenant_id,
                "policy": EscalationPolicy.model_validate(saved["policy"]).model_dump(mode="json"),
                "source": "tenant", "updated_by": saved["updated_by"], "updated_at": saved["updated_at"],
            }
        return {
            "tenant_id": principal.tenant_id,
            "policy": EscalationPolicy.model_validate(default_escalation_policy()).model_dump(mode="json"),
            "source": "default", "updated_by": None, "updated_at": None,
        }

    @app.put("/escalation-policy")
    def put_escalation_policy(
        request: EscalationPolicy, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "settings:manage")
        saved = db.save_tenant_policy(principal.tenant_id, request.model_dump(mode="json"), principal.actor)
        return {
            "tenant_id": principal.tenant_id, "policy": saved["policy"], "source": "tenant",
            "updated_by": saved["updated_by"], "updated_at": saved["updated_at"],
        }

    @app.get("/integrations/connections")
    def list_integration_connections(principal: Principal = Depends(authenticate)) -> list[dict]:
        require_scope(principal, "settings:read")
        return [public_connection(row) for row in db.list_integration_connections(principal.tenant_id)]

    @app.put("/integrations/connections/{integration}")
    def put_integration_connection(
        integration: str, request: IntegrationConnectionRequest, principal: Principal = Depends(authenticate),
    ) -> dict:
        require_scope(principal, "settings:manage")
        family = "calendar" if integration == "calendar" else "email" if integration == "email" else None
        expected_family = "calendar" if request.provider.endswith("calendar") else "email"
        if family is None or family != expected_family:
            raise HTTPException(status_code=422, detail="Integration must be 'calendar' or 'email' and match the provider.")
        required_settings = {
            "google_calendar": ("calendar_id",), "microsoft_calendar": ("calendar_id", "user_id"),
            "smtp": ("host",), "microsoft_email": ("user_id",), "gmail": (),
        }[request.provider]
        if any(not request.settings.get(key) for key in required_settings) or not request.credentials:
            raise HTTPException(status_code=422, detail="The selected provider requires its connection settings and credentials.")
        try:
            ciphertext = encrypt_credentials(principal.tenant_id, integration, request.credentials)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        row = db.save_integration_connection(
            principal.tenant_id, integration, request.provider, request.settings, ciphertext, principal.actor,
        )
        return public_connection(row)

    @app.delete("/integrations/connections/{integration}", status_code=204)
    def delete_integration_connection(integration: str, principal: Principal = Depends(authenticate)) -> Response:
        require_scope(principal, "settings:manage")
        if integration not in {"calendar", "email"}:
            raise HTTPException(status_code=404, detail="Integration not found")
        if not db.delete_integration_connection(principal.tenant_id, integration, principal.actor):
            raise HTTPException(status_code=404, detail="Integration connection not found")
        return Response(status_code=204)

    @app.post("/integrations/connections/{integration}/check")
    def check_integration_connection(integration: str, principal: Principal = Depends(authenticate)) -> dict:
        require_scope(principal, "settings:manage")
        row = db.get_integration_connection(principal.tenant_id, integration)
        if not row:
            raise HTTPException(status_code=404, detail="Integration connection not found")
        try:
            credentials = decrypt_credentials(principal.tenant_id, integration, row["credentials_ciphertext"])
            provider = row["provider"]
            if provider in {"google_calendar", "microsoft_calendar", "microsoft_email", "gmail"}:
                token = access_token(provider, credentials)
                if provider == "google_calendar":
                    url = f"https://www.googleapis.com/calendar/v3/calendars/{row['settings']['calendar_id']}/events?maxResults=1"
                elif provider == "microsoft_calendar":
                    url = f"https://graph.microsoft.com/v1.0/users/{row['settings']['user_id']}/calendars/{row['settings']['calendar_id']}/events?$top=1"
                elif provider == "microsoft_email":
                    url = f"https://graph.microsoft.com/v1.0/users/{row['settings']['user_id']}?$select=id"
                else:
                    url = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
                req = Request(url, headers={"Authorization": f"Bearer {token}"})
                with urlopen(req, timeout=15) as response:
                    response.read(2048)
            else:
                import smtplib
                import ssl
                host = row["settings"]["host"]
                port = int(row["settings"].get("port", "587"))
                with smtplib.SMTP(host, port, timeout=15) as client:
                    client.ehlo()
                    if row["settings"].get("starttls", "true").casefold() == "true":
                        client.starttls(context=ssl.create_default_context())
                        client.ehlo()
                    if credentials.get("username"):
                        client.login(credentials["username"], credentials.get("password", ""))
            db.update_integration_health(principal.tenant_id, integration, "healthy")
            return {"integration": integration, "status": "healthy", "checked_at": db.get_integration_connection(principal.tenant_id, integration)["last_checked_at"]}
        except Exception as exc:
            db.update_integration_health(principal.tenant_id, integration, "error", f"{type(exc).__name__}: {str(exc)[:300]}")
            raise HTTPException(status_code=502, detail=f"Connection check failed: {type(exc).__name__}") from exc

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
        if row["status"] == "resolved":
            enqueue_calendar_sync_if_configured(principal.tenant_id)
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


def _portfolio_export_rows(db: Store, dataset: str, tenant_id: str) -> list[dict]:
    if dataset == "contracts":
        result = []
        for row in db.list_contracts(tenant_id=tenant_id):
            terms = json.loads(row["confirmed_json"] or row["extracted_json"])
            result.append({key: row[key] for key in ("id", "filename", "status", "provider", "created_at", "confirmed_at", "supplier_id")}
                          | {key: terms.get(key) for key in ("contract", "start_date", "expiration_date", "renewal_notice_days", "auto_renew", "owner_name", "owner_email", "annual_contract_value", "currency")})
        return result
    if dataset == "tasks":
        fields = ("id", "contract_id", "title", "owner_name", "owner_email", "due_date", "expiration_date", "status", "workflow_state", "task_kind", "created_at")
        return [{field: row[field] for field in fields} for row in db.list_tasks(tenant_id=tenant_id)]
    if dataset == "suppliers":
        return [{key: value for key, value in row.items() if key != "annual_spend_at_risk"} | {
            "annual_spend_at_risk": json.dumps(row.get("annual_spend_at_risk", {}), sort_keys=True)
        } for row in db.list_suppliers(tenant_id)]
    if dataset == "obligations":
        fields = ("id", "contract_id", "task_id", "category", "title", "description", "due_date", "owner_name", "owner_email", "status", "schedule_status", "recurrence_interval_months", "recurrence_group_id", "created_at")
        return [{field: row.get(field) for field in fields} | {"source_evidence": row.get("evidence", {}).get("quote", "")} for row in db.list_contract_obligations(tenant_id)]
    return []


def _spreadsheet_safe(value) -> str:
    if value is None:
        return ""
    text = "".join(character for character in str(value) if character in "\t\n\r" or ord(character) >= 32)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def _portfolio_csv(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]), extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _spreadsheet_safe(value) for key, value in row.items()})
    return output.getvalue().encode("utf-8-sig")


def _portfolio_xlsx(rows: list[dict], sheet_name: str) -> bytes:
    columns = list(rows[0]) if rows else ["No records"]
    table = [columns, *[[row.get(column) for column in columns] for row in rows]] if rows else [columns]

    def column_name(index: int) -> str:
        label = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            label = chr(65 + remainder) + label
        return label

    xml_rows = []
    for row_index, row in enumerate(table, start=1):
        cells = []
        for col_index, value in enumerate(row, start=1):
            address = f"{column_name(col_index)}{row_index}"
            escaped_value = escape(_spreadsheet_safe(value))
            cells.append(f'<c r="{address}" t="inlineStr"><is><t xml:space="preserve">{escaped_value}</t></is></c>')
        xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    safe_name = escape(sheet_name[:31])
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{safe_name}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": sheet,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


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
        workflow_state=row["workflow_state"], kind=row["task_kind"],
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


_OBLIGATIONS_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Obligations</title><style>
:root{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{background:#10243a;color:#fff;padding:20px 28px}header h1{font-size:21px;margin:0}.bar{padding:12px 26px;background:#fff;border-bottom:1px solid #dbe2ea;display:flex;gap:10px;align-items:center;flex-wrap:wrap}.bar input{flex:1;max-width:480px;padding:9px;border:1px solid #cbd5e1;border-radius:6px}.bar a{color:#1677c8}main{max-width:1100px;margin:22px auto;padding:0 18px}.columns{display:grid;grid-template-columns:1fr 1fr;gap:16px}.panel{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:18px;min-width:0}.panel h2{font-size:17px;margin:0 0 6px}.panel p,.muted{color:#64748b}.panel label{font-size:13px;font-weight:600;display:block;color:#364152}.panel input,.panel select,.panel textarea{width:100%;padding:9px;margin:4px 0 10px;border:1px solid #cbd5e1;border-radius:6px;font:inherit}.panel textarea{min-height:70px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.proposal,.obligation{border:1px solid #dbe2ea;border-radius:8px;padding:12px;margin:9px 0}.proposal blockquote{margin:6px 0;color:#526174;font-size:13px}.meta{font-size:13px;color:#526174}.actions{display:flex;gap:8px;flex-wrap:wrap}button{border:0;border-radius:6px;padding:8px 12px;font:inherit;font-weight:650;cursor:pointer}.primary{background:#1677c8;color:white}.secondary{background:#e9eff6;color:#182230}.notice{min-height:24px;color:#b42318}.pill{display:inline-block;padding:2px 8px;background:#e9eff6;border-radius:99px;font-size:12px}@media(max-width:760px){.columns,.grid{grid-template-columns:1fr}}
</style></head><body><header><h1>◈ Renewal Radar / contract obligations</h1></header><div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token"><button class="primary" onclick="loadObligations()">Connect / refresh</button><a href="/auth/oidc/login">Sign in with SSO</a><a href="/tasks/inbox">Task inbox</a><a href="/renewal-forecast">Renewal forecast</a><a href="/renewal-risk">Portfolio risk</a><a href="/portfolio/workspace">Portfolio operations</a><a href="/approvals">Approval inbox</a></div><main><p id="notice" class="notice" role="status"></p><div class="columns"><section class="panel"><h2>Extracted obligation proposals</h2><p>Load a contract to review its evidence-backed proposals. Scheduling a proposal always requires a named owner and a verified date.</p><div class="grid"><input id="contractId" placeholder="Contract ID"><button class="secondary" onclick="loadProposals()">Load proposals</button></div><div id="proposals" class="muted">No proposals loaded.</div></section><section class="panel"><h2>Schedule an obligation</h2><p>Only confirmed, active contracts can receive scheduled obligations.</p><label>Category<select id="category"><option value="price_increase">Price increase</option><option value="insurance_certificate">Insurance certificate</option><option value="audit_right">Audit right</option><option value="termination_window">Termination window</option><option value="other">Other</option></select></label><label>Action title<input id="title" maxlength="250" placeholder="Submit current insurance certificate"></label><label>Description<textarea id="description" maxlength="2000"></textarea></label><div class="grid"><label>Due date<input id="dueDate" type="date" required></label><label>Owner name<input id="ownerName" maxlength="150"></label></div><label>Repeat every (months, optional)<input id="recurrenceMonths" type="number" min="1" max="120" placeholder="Leave empty for one time"></label><label>Owner email<input id="ownerEmail" type="email" required></label><label>Evidence quote<textarea id="evidenceQuote" maxlength="1000" required></textarea></label><div class="grid"><label>PDF page<input id="evidencePage" type="number" min="1"></label><label>Evidence confidence<input id="evidenceConfidence" type="number" min="0" max="1" step="0.01" value="0.8"></label></div><button class="primary" onclick="scheduleObligation()">Schedule reminder</button></section></div><section class="panel" style="margin-top:16px"><h2>Scheduled obligations</h2><div id="list" class="muted">Connect to load obligations.</div></section></main><script>
const $=id=>document.getElementById(id);let loadedTerms=null;function auth(){const t=$('token').value.trim()||sessionStorage.getItem('radar-token')||'';return t?{Authorization:'Bearer '+t}:{}}async function api(path,opt={}){const r=await fetch(path,{...opt,headers:{...auth(),...(opt.headers||{})}});if(!r.ok){let e={};try{e=await r.json()}catch{}throw Error(e.detail||r.status)}return r.status===204?null:r.json()}function say(s){$('notice').textContent=s}function resolveDue(o){if(o.due_date)return o.due_date;if(!o.due_offset_days||!o.due_offset_from)return '';const anchor=loadedTerms?.[o.due_offset_from];if(!anchor)return '';const d=new Date(anchor+'T00:00:00Z'),n=o.due_offset_days*(o.due_offset_direction==='before'?-1:1);d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10)}async function loadProposals(){try{const contract=await api('/contracts/'+encodeURIComponent($('contractId').value.trim()));loadedTerms=contract.confirmed||contract.extracted;const proposals=contract.extracted.obligations||[],el=$('proposals');el.replaceChildren();if(!proposals.length){el.textContent='No schedulable obligation proposals were extracted.';return}for(const o of proposals){const card=document.createElement('div');card.className='proposal';const title=document.createElement('strong');title.textContent=o.title+' · '+o.category.replaceAll('_',' ');const due=document.createElement('div');due.className='meta';due.textContent='Proposed due date: '+(resolveDue(o)||'needs reviewer scheduling');const quote=document.createElement('blockquote');quote.textContent='“'+o.evidence.quote+'”';const use=document.createElement('button');use.className='secondary';use.textContent='Review and assign owner';use.onclick=()=>{$('category').value=o.category;$('title').value=o.title;$('description').value=o.description||'';$('dueDate').value=resolveDue(o);$('evidenceQuote').value=o.evidence.quote;$('evidencePage').value=o.evidence.page||'';$('evidenceConfidence').value=o.evidence.confidence||0.8;$('recurrenceMonths').value=o.recurrence_interval_months||''};card.append(title,due,quote,use);el.append(card)}}catch(e){say('Could not load proposals: '+e.message)}}async function scheduleObligation(){const id=$('contractId').value.trim(),payload={category:$('category').value,title:$('title').value.trim(),description:$('description').value.trim()||null,due_date:$('dueDate').value,owner_name:$('ownerName').value.trim()||null,owner_email:$('ownerEmail').value.trim(),evidence:{field:'obligation:'+$('category').value,quote:$('evidenceQuote').value.trim(),page:$('evidencePage').value?Number($('evidencePage').value):null,confidence:Number($('evidenceConfidence').value)},recurrence_interval_months:$('recurrenceMonths').value?Number($('recurrenceMonths').value):null};try{await api('/contracts/'+encodeURIComponent(id)+'/obligations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});say('Obligation scheduled and reminder task created.');await loadObligations()}catch(e){say('Could not schedule obligation: '+e.message)}}async function changeSchedule(o,payload){try{await api('/obligations/'+o.id+'/schedule',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});say('Recurring schedule updated.');await loadObligations()}catch(e){say('Could not update schedule: '+e.message)}}async function loadObligations(){try{if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());const rows=await api('/obligations'),el=$('list');el.replaceChildren();if(!rows.length){el.textContent='No scheduled obligations.';el.className='muted';return}el.className='';const latest=new Map();for(const item of rows){const key=item.recurrence_group_id||item.id,old=latest.get(key);if(!old||(item.status==='open'&&old.status!=='open')||(item.status===old.status&&item.due_date>old.due_date))latest.set(key,item)}for(const o of latest.values()){const card=document.createElement('div');card.className='obligation';const h=document.createElement('strong');h.textContent=o.title;const m=document.createElement('div');m.className='meta';m.textContent=o.category.replaceAll('_',' ')+' · Due '+o.due_date+(o.recurrence_interval_months?' · Repeats every '+o.recurrence_interval_months+' month(s)':'')+' · '+(o.owner_name||o.owner_email||'Unassigned');const q=document.createElement('blockquote');q.textContent='“'+o.evidence.quote+'”';const done=document.createElement('button');done.className='secondary';done.textContent='Mark complete';done.onclick=async()=>{try{await api('/tasks/'+o.task_id+'/resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({comment:'Obligation completed'})});say('Obligation completed.');await loadObligations()}catch(e){say('Could not complete obligation: '+e.message)}};card.append(h,m,q);const schedule=document.createElement('div');schedule.className='meta';schedule.textContent='Schedule: '+(o.schedule_status||'active');card.append(schedule);if(o.recurrence_interval_months){const controls=document.createElement('div');controls.className='actions';const cadence=document.createElement('input');cadence.type='number';cadence.min='1';cadence.max='120';cadence.value=o.recurrence_interval_months;cadence.setAttribute('aria-label','Repeat every months');const saveCadence=document.createElement('button');saveCadence.className='secondary';saveCadence.textContent='Save cadence';saveCadence.onclick=()=>changeSchedule(o,{recurrence_interval_months:Number(cadence.value)});const toggle=document.createElement('button');toggle.className='secondary';toggle.textContent=o.schedule_status==='paused'?'Resume schedule':'Pause schedule';toggle.disabled=o.schedule_status==='ended';toggle.onclick=()=>changeSchedule(o,{status:o.schedule_status==='paused'?'active':'paused'});const end=document.createElement('button');end.className='secondary';end.textContent='End schedule';end.disabled=o.schedule_status==='ended';end.onclick=()=>changeSchedule(o,{status:'ended'});controls.append(cadence,saveCadence,toggle,end);card.append(controls)}if(o.status==='open'){const due=document.createElement('input');due.type='date';due.value=o.due_date;due.setAttribute('aria-label','Reschedule occurrence');const move=document.createElement('button');move.className='secondary';move.textContent='Reschedule';move.onclick=async()=>{try{await api('/obligations/'+o.id+'/reschedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({due_date:due.value})});say('Occurrence rescheduled.');await loadObligations()}catch(e){say('Could not reschedule: '+e.message)}};card.append(due,move);const done=document.createElement('button');done.className='secondary';done.textContent='Submit proof / view history';done.onclick=()=>{location.href='/compliance/workspace?obligation='+encodeURIComponent(o.id)};card.append(done)}el.append(card)}}catch(e){say('Could not load obligations: '+e.message)}}const saved=sessionStorage.getItem('radar-token');if(saved)$('token').value=saved;loadObligations();
</script></body></html>'''

_FORECAST_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Forecast</title><style>
:root{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{background:#10243a;color:#fff;padding:20px 28px}header h1{font-size:21px;margin:0}.bar{padding:12px 26px;background:#fff;border-bottom:1px solid #dbe2ea;display:flex;gap:10px;align-items:center;flex-wrap:wrap}.bar input{flex:1;max-width:480px;padding:9px;border:1px solid #cbd5e1;border-radius:6px}.bar a{color:#1677c8}main{max-width:1100px;margin:22px auto;padding:0 18px}.panel,.scenario{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:18px}.panel h2{font-size:17px;margin:0 0 8px}.panel p{color:#64748b}.form{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;align-items:end}.form label{font-size:13px;font-weight:600;color:#364152}.form input{width:100%;padding:9px;margin:4px 0 0;border:1px solid #cbd5e1;border-radius:6px;font:inherit}button{border:0;border-radius:6px;padding:9px 12px;font:inherit;font-weight:650;cursor:pointer;background:#1677c8;color:white}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:14px 0}.scenario h3{margin:0 0 5px;font-size:16px}.amount{font-size:20px;font-weight:700}.scenario p,.muted{color:#64748b}.tablewrap{overflow:auto;background:#fff;border:1px solid #dbe2ea;border-radius:10px;margin-top:14px}table{border-collapse:collapse;width:100%;min-width:700px}th,td{text-align:left;border-bottom:1px solid #e7ecf2;padding:9px 11px;font-size:13px}th{background:#f8fafc}.notice{min-height:24px;color:#b42318}.notes{color:#526174;font-size:13px;margin-top:14px}@media(max-width:760px){.form{grid-template-columns:1fr 1fr}.cards{grid-template-columns:1fr}}@media(max-width:480px){.form{grid-template-columns:1fr}}
</style></head><body><header><h1>◈ Renewal Radar / renewal planning</h1></header><div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token"><a href="/auth/oidc/login">Sign in with SSO</a><a href="/tasks/inbox">Task inbox</a><a href="/obligations/workspace">Obligations</a><a href="/compliance/workspace">Compliance evidence</a><a href="/renewal-risk">Portfolio risk</a><a href="/portfolio/workspace">Portfolio operations</a></div><main><section class="panel"><h2>Compare renewal decisions</h2><p>Forecast active confirmed contract values over a planning period. Costs are prorated by calendar days and grouped by currency.</p><div class="form"><label>Period starts<input id="start" type="date"></label><label>Period ends<input id="end" type="date"></label><label>Renewal increase %<input id="increase" type="number" min="-100" max="1000" step="0.01" value="0"></label><label>Renegotiation savings %<input id="savings" type="number" min="0" max="100" step="0.01" value="10"></label></div><button onclick="run()" style="margin-top:12px">Generate forecast</button></section><p id="notice" class="notice" role="status"></p><div id="results"></div><section class="panel"><h2>Forecast accuracy</h2><p>Scenario variance is predicted minus actual annual value. Only forecasts saved before a recorded outcome are scored.</p><div id="accuracy" class="muted">No forecast outcomes have been measured yet.</div></section></main><script>
let selectedPlaybook=null;const $=id=>document.getElementById(id);function auth(){const t=$('token').value.trim()||sessionStorage.getItem('radar-token')||'';return t?{Authorization:'Bearer '+t}:{}}function money(values){const entries=Object.entries(values||{});return entries.length?entries.map(([c,v])=>c+' '+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})).join(' · '):'No priced contracts'}async function run(){try{if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());const r=await fetch('/renewal-forecast',{method:'POST',headers:{...auth(),'Content-Type':'application/json'},body:JSON.stringify({start_date:$('start').value,end_date:$('end').value,renewal_increase_pct:$('increase').value,renegotiation_savings_pct:$('savings').value})});if(!r.ok){const e=await r.json();throw Error(e.detail||r.status)}const d=await r.json(),el=$('results');el.replaceChildren();$('notice').textContent='';const cards=document.createElement('div');cards.className='cards';for(const [key,label] of [['renew','Renew'],['renegotiate','Renegotiate'],['terminate','Terminate at expiration']]){const s=d.scenarios[key],card=document.createElement('section');card.className='scenario';const h=document.createElement('h3');h.textContent=label;const amount=document.createElement('div');amount.className='amount';amount.textContent=money(s.projected_cost_by_currency);const p=document.createElement('p');p.textContent='Notice deadlines: '+Object.values(s.notice_deadlines_by_month).reduce((a,b)=>a+b,0)+' · Expirations: '+Object.values(s.expirations_by_month).reduce((a,b)=>a+b,0);card.append(h,amount,p);cards.append(card)}el.append(cards);const meta=document.createElement('p');meta.className='notes';meta.textContent=d.contracts_considered+' active contracts considered · '+d.contracts_missing_value.length+' lack confirmed annual value. '+d.assumptions.proration;el.append(meta);const months=[...new Set(Object.values(d.scenarios).flatMap(s=>Object.keys(s.monthly_projected_cost_by_currency)))].sort();const currencies=[...new Set(Object.values(d.scenarios).flatMap(s=>Object.values(s.monthly_projected_cost_by_currency).flatMap(v=>Object.keys(v))))].sort();for(const currency of currencies){const h=document.createElement('h2');h.textContent='Monthly '+currency+' costs';el.append(h);const table=document.createElement('table'),head=document.createElement('thead'),hr=document.createElement('tr');for(const label of ['Month','Renew','Renegotiate','Terminate']){const th=document.createElement('th');th.textContent=label;hr.append(th)}head.append(hr);table.append(head);const body=document.createElement('tbody');for(const month of months){const tr=document.createElement('tr'),m=document.createElement('td');m.textContent=month;tr.append(m);for(const key of ['renew','renegotiate','terminate']){const td=document.createElement('td');td.textContent=money({[currency]:d.scenarios[key].monthly_projected_cost_by_currency[month]?.[currency]||0});tr.append(td)}body.append(tr)}table.append(body);const wrap=document.createElement('div');wrap.className='tablewrap';wrap.append(table);el.append(wrap)}const h=document.createElement('h2');h.textContent='Contracts in the plan';el.append(h);const table=document.createElement('table'),head=document.createElement('thead'),hr=document.createElement('tr');for(const x of ['Contract','Supplier','Notice deadline','Expiration','Currency','Renew','Renegotiate','Terminate']){const th=document.createElement('th');th.textContent=x;hr.append(th)}head.append(hr);table.append(head);const body=document.createElement('tbody');for(const c of d.contracts){const tr=document.createElement('tr');for(const value of [c.contract,c.supplier||'—',c.notice_deadline||'—',c.expiration_date||'—',c.currency||'—',...(c.projected_costs?[c.projected_costs.renew,c.projected_costs.renegotiate,c.projected_costs.terminate]:['Missing value','Missing value','Missing value'])]){const td=document.createElement('td');td.textContent=value;tr.append(td)}body.append(tr)}table.append(body);const wrap=document.createElement('div');wrap.className='tablewrap';wrap.append(table);el.append(wrap)}catch(e){$('notice').textContent='Could not generate forecast: '+e.message}}async function loadAccuracy(){const root=$('accuracy');try{const r=await fetch('/renewal-forecast/accuracy',{headers:auth()});if(!r.ok)throw Error((await r.json()).detail||r.status);const d=await r.json();root.replaceChildren();if(!d.outcome_count){root.textContent='No scored outcomes yet. Generate a forecast, then record renewal outcomes in the task inbox.';return}const summary=document.createElement('p');summary.textContent=d.outcome_count+' outcome(s) matched to saved forecasts.';root.append(summary);const a=d.recommended_assumptions,apply=document.createElement('button');apply.textContent='Use historical assumptions';apply.disabled=a.renewal_increase_pct===null&&a.renegotiation_savings_pct===null;apply.onclick=()=>{if(a.renewal_increase_pct!==null)$('increase').value=a.renewal_increase_pct;if(a.renegotiation_savings_pct!==null)$('savings').value=a.renegotiation_savings_pct;$('notice').textContent='Planning assumptions updated from '+a.renewal_samples+' renewal(s) and '+a.renegotiation_samples+' renegotiation(s). Generate a new forecast to save them.'};root.append(apply);const table=document.createElement('table'),head=document.createElement('thead'),hr=document.createElement('tr');for(const x of ['Supplier','Scenario','Currency','Cases','Predicted annual','Actual annual','Mean variance','Mean absolute variance']){const th=document.createElement('th');th.textContent=x;hr.append(th)}head.append(hr);table.append(head);const body=document.createElement('tbody');for(const m of d.by_supplier_and_scenario){const tr=document.createElement('tr');for(const x of [m.supplier||'Unassigned',m.scenario,m.currency||'—',m.sample_count,m.mean_predicted_annual_value,m.mean_actual_annual_value,m.mean_variance,m.mean_absolute_variance]){const td=document.createElement('td');td.textContent=x;tr.append(td)}body.append(tr)}table.append(body);const wrap=document.createElement('div');wrap.className='tablewrap';wrap.append(table);root.append(wrap)}catch(e){root.textContent='Could not load forecast accuracy: '+e.message}}const saved=sessionStorage.getItem('radar-token');if(saved)$('token').value=saved;const now=new Date(),localDate=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');$('start').value=localDate(new Date(now.getFullYear(),now.getMonth(),1));$('end').value=localDate(new Date(now.getFullYear()+1,now.getMonth(),0));loadAccuracy();
</script></body></html>'''

_SUPPLIERS_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Suppliers</title><style>
:root{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{background:#10243a;color:#fff;padding:20px 28px}header h1{font-size:21px;margin:0}.bar{padding:12px 26px;background:#fff;border-bottom:1px solid #dbe2ea;display:flex;gap:12px;flex-wrap:wrap}.bar a{color:#1677c8}main{max-width:1050px;margin:22px auto;padding:0 18px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:18px}.card h2{font-size:17px;margin:0 0 8px}.card p{color:#64748b;margin:0 0 12px}input{width:100%;padding:9px;margin:4px 0 10px;border:1px solid #cbd5e1;border-radius:6px;font:inherit}button{border:0;border-radius:6px;padding:8px 12px;font:inherit;font-weight:650;cursor:pointer;background:#1677c8;color:#fff}.supplier{background:#fff;border:1px solid #dbe2ea;border-radius:9px;padding:14px;margin:10px 0}.supplier h3{margin:0 0 3px}.meta{color:#526174;font-size:13px}.spend{font-variant-numeric:tabular-nums;margin-top:8px}.notice{min-height:24px;color:#b42318}@media(max-width:700px){.grid{grid-template-columns:1fr}}
</style></head><body><header><h1>◈ Renewal Radar / supplier directory</h1></header><div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token"><button onclick="load()">Connect / refresh</button><a href="/auth/oidc/login">Sign in with SSO</a><a href="/renewal-risk">Portfolio risk</a><a href="/portfolio/workspace">Portfolio operations</a><a href="/integrations">Integrations</a><a href="/tasks/inbox">Task inbox</a></div><main><p id="notice" class="notice" role="status"></p><div class="grid"><section class="card"><h2>Add supplier</h2><p>Supplier names are normalized per organization to prevent duplicate records.</p><input id="supplierName" maxlength="250" placeholder="Canonical supplier name"><button onclick="createSupplier()">Create supplier</button></section><section class="card"><h2>Assign a contract</h2><p>Use the contract ID from the contract review workspace or API.</p><input id="supplierId" placeholder="Supplier ID"><input id="contractId" placeholder="Contract ID"><button onclick="assign()">Assign contract</button></section></div><h2>Supplier portfolio</h2><div id="list">Connect to load suppliers.</div></main><script>
const $=id=>document.getElementById(id);function auth(){const t=$('token').value.trim()||sessionStorage.getItem('radar-token')||'';return t?{Authorization:'Bearer '+t}:{}}async function api(path,opt={}){const r=await fetch(path,{...opt,headers:{...auth(),...(opt.headers||{})}});if(!r.ok){let e={};try{e=await r.json()}catch{}throw Error(e.detail||r.status)}return r.json()}function money(values){const entries=Object.entries(values||{});return entries.length?entries.map(([k,v])=>k+' '+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})).join(' · '):'No open renewal exposure'}async function load(){try{if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());const rows=await api('/suppliers'),list=$('list');list.replaceChildren();if(!rows.length){list.textContent='No suppliers yet. Create a canonical record to start grouping contracts.';return}for(const s of rows){const card=document.createElement('section');card.className='supplier';const title=document.createElement('h3');title.textContent=s.name;const meta=document.createElement('div');meta.className='meta';meta.textContent=s.contract_count+' contracts · '+s.active_contract_count+' active · '+s.open_renewal_count+' open renewal tasks';const spend=document.createElement('div');spend.className='spend';spend.textContent='Annual spend at risk: '+money(s.annual_spend_at_risk);const sid=document.createElement('div');sid.className='meta';sid.textContent='Supplier ID: '+s.id;const choose=document.createElement('button');choose.textContent='Use this supplier';choose.onclick=()=>{$('supplierId').value=s.id;$('notice').textContent='Supplier selected for contract assignment.'};card.append(title,meta,spend,sid,choose);list.append(card)}}catch(e){$('notice').textContent='Could not load suppliers: '+e.message}}async function createSupplier(){try{await api('/suppliers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('supplierName').value})});$('supplierName').value='';$('notice').textContent='Supplier created.';await load()}catch(e){$('notice').textContent='Could not create supplier: '+e.message}}async function assign(){try{await api('/suppliers/'+encodeURIComponent($('supplierId').value.trim())+'/contracts/'+encodeURIComponent($('contractId').value.trim()),{method:'PUT'});$('notice').textContent='Contract assigned.';$('contractId').value='';await load()}catch(e){$('notice').textContent='Could not assign contract: '+e.message}}const saved=sessionStorage.getItem('radar-token');if(saved)$('token').value=saved;load();
</script></body></html>'''

_INTEGRATIONS_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Integrations</title>
<style>
:root{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{background:#10243a;color:#fff;padding:20px 28px}header h1{font-size:21px;margin:0}header p{margin:4px 0 0;color:#c7d5e3}.bar{padding:12px 26px;background:#fff;border-bottom:1px solid #dbe2ea;display:flex;gap:10px;align-items:center;flex-wrap:wrap}.bar input{flex:1;max-width:520px;padding:9px;border:1px solid #cbd5e1;border-radius:6px}.bar a{color:#1677c8}main{max-width:1050px;margin:22px auto;padding:0 18px}.intro{color:#526174;margin:0 0 16px}.cards{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:18px;min-width:0}.card h2{font-size:17px;margin:0 0 5px}.card p{color:#64748b;margin:0 0 14px}.card label{font-size:13px;font-weight:600;color:#364152;display:block}.card input,.card select{display:block;width:100%;padding:9px;margin:4px 0 11px;border:1px solid #cbd5e1;border-radius:6px;font:inherit}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}button{border:0;border-radius:6px;padding:8px 12px;font:inherit;font-weight:650;cursor:pointer}.primary{background:#1677c8;color:#fff}.secondary{background:#e9eff6;color:#182230}.danger{color:#b42318;background:#fff0ef}.state{font-size:13px;padding:9px 10px;border-radius:6px;background:#f1f5f9;margin:12px 0}.state.ok{background:#e9f7ef;color:#17633a}.state.bad{background:#fff0ef;color:#9a271f}.help{font-size:12px;color:#64748b;margin:-5px 0 10px}.notice{color:#b42318;min-height:24px;margin:10px 0}.lock{color:#64748b;font-size:13px}.hidden{display:none!important}@media(max-width:720px){.cards{grid-template-columns:1fr}.row{grid-template-columns:1fr}}
</style></head><body><header><h1>◈ Renewal Radar / integration setup</h1><p>Configure calendar and email connections for this organization.</p></header>
<div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token"><button class="primary" onclick="loadConnections()">Connect / refresh</button><a href="/auth/oidc/login">Sign in with SSO</a><a href="/operations">Operations</a><a href="/tasks/inbox">Task inbox</a><a href="/renewal-risk">Portfolio risk</a><a href="/portfolio/workspace">Portfolio operations</a></div>
<main><p class="intro">Credentials are encrypted at rest and never shown after saving. Updating a connection replaces its credentials, so enter the complete credential set again.</p><p id="notice" class="notice" role="status"></p><div class="cards">
<section class="card"><h2>Calendar</h2><p>Publish renewal deadlines to your organization calendar.</p><label>Provider<select id="calProvider" onchange="providerChanged('calendar')"><option value="google_calendar">Google Calendar</option><option value="microsoft_calendar">Microsoft 365 Calendar</option></select></label><div class="row"><div><label>Calendar ID<input id="calCalendar" placeholder="primary or calendar ID"></label></div><div id="calUserWrap"><label>User ID<input id="calUser" placeholder="user or mailbox ID"></label></div></div><div class="oauth"><label>Access token (optional when using app credentials)<input id="calAccess" type="password" autocomplete="new-password"></label><div class="row"><label>Tenant ID<input id="calTenant" autocomplete="off"></label><label>Client ID<input id="calClient" autocomplete="off"></label></div><div class="row"><label>Client secret<input id="calSecret" type="password" autocomplete="new-password"></label><label>Refresh token<input id="calRefresh" type="password" autocomplete="new-password"></label></div></div><div class="help">Google supports an access token or client ID, client secret, and refresh token. Microsoft supports an access token or tenant ID, client ID, and client secret.</div><div class="state" id="calState">Not connected</div><div class="actions"><button class="primary" onclick="saveConnection('calendar')">Save calendar</button><button class="secondary" onclick="checkConnection('calendar')">Check connection</button><button class="danger" onclick="deleteConnection('calendar')">Remove</button></div></section>
<section class="card"><h2>Email</h2><p>Send owner reminders and deadline escalations.</p><label>Provider<select id="emailProvider" onchange="providerChanged('email')"><option value="smtp">SMTP</option><option value="gmail">Gmail</option><option value="microsoft_email">Microsoft 365 email</option></select></label><div id="smtpFields"><div class="row"><label>SMTP host<input id="mailHost" placeholder="smtp.example.com"></label><label>Port<input id="mailPort" type="number" value="587"></label></div><div class="row"><label>From address<input id="mailFrom" type="email" placeholder="contracts@example.com"></label><label>Username<input id="mailUser" autocomplete="off"></label></div><label>Password<input id="mailPassword" type="password" autocomplete="new-password"></label><label><input id="mailTls" type="checkbox" checked style="display:inline;width:auto;margin-right:7px">Use STARTTLS</label></div><div id="oauthEmail" class="hidden"><label id="mailUserIdWrap">Microsoft user ID<input id="mailUserId" placeholder="user or mailbox ID"></label><label>From address (optional)<input id="oauthFrom" type="email" placeholder="contracts@example.com"></label><label>Access token (optional when using app credentials)<input id="emailAccess" type="password" autocomplete="new-password"></label><div class="row"><label>Tenant ID<input id="emailTenant" autocomplete="off"></label><label>Client ID<input id="emailClient" autocomplete="off"></label></div><div class="row"><label>Client secret<input id="emailSecret" type="password" autocomplete="new-password"></label><label>Refresh token<input id="emailRefresh" type="password" autocomplete="new-password"></label></div><div class="help">Gmail supports an access token or client ID, client secret, and refresh token. Microsoft supports an access token or tenant ID, client ID, and client secret.</div></div><div class="state" id="emailState">Not connected</div><div class="actions"><button class="primary" onclick="saveConnection('email')">Save email</button><button class="secondary" onclick="checkConnection('email')">Check connection</button><button class="danger" onclick="deleteConnection('email')">Remove</button></div></section></div><p class="lock">Configuration changes require an administrator account. Connection checks contact the selected provider.</p></main>
<script>
const $=id=>document.getElementById(id);function auth(){const t=$('token').value.trim()||sessionStorage.getItem('radar-token')||'';return t?{Authorization:'Bearer '+t}:{}}async function api(path,options={}){const r=await fetch(path,{...options,headers:{...auth(),...(options.headers||{})}});if(!r.ok){let e={};try{e=await r.json()}catch{}throw Error(e.detail||r.status)}return r.status===204?null:r.json()}function say(s){$('notice').textContent=s}function providerChanged(family){const p=family==='calendar'?'calProvider':'emailProvider';if(family==='calendar')$('calUserWrap').classList.toggle('hidden',$(p).value!=='microsoft_calendar');else{$('smtpFields').classList.toggle('hidden',$(p).value!=='smtp');$('oauthEmail').classList.toggle('hidden',$(p).value==='smtp');$('mailUserIdWrap').classList.toggle('hidden',$(p).value!=='microsoft_email')}}function setState(family,c){const el=$(family==='calendar'?'calState':'emailState');el.className='state'+(c?' ok':'');el.textContent=c?`${c.provider.replaceAll('_',' ')} · ${c.status}${c.last_checked_at?' · checked '+new Date(c.last_checked_at).toLocaleString():''}${c.last_error?' · '+c.last_error:''}`:'Not connected'}async function loadConnections(){try{if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());const rows=await api('/integrations/connections');for(const family of ['calendar','email']){const c=rows.find(x=>x.integration===family);setState(family,c);if(c){(family==='calendar'?'calProvider':'emailProvider').value=c.provider;providerChanged(family);if(family==='calendar'){$('calCalendar').value=c.settings.calendar_id||'';$('calUser').value=c.settings.user_id||''}else if(c.provider==='smtp'){$('mailHost').value=c.settings.host||'';$('mailPort').value=c.settings.port||'587';$('mailFrom').value=c.settings.from_address||'';$('mailTls').checked=(c.settings.starttls||'true').toLowerCase()==='true'}else{$('mailUserId').value=c.settings.user_id||'';$('oauthFrom').value=c.settings.from_address||''}}}say('Connection settings loaded. Credentials remain hidden.')}catch(e){say('Could not load connections: '+e.message)}}async function saveConnection(f){const cal=f==='calendar',provider=$(cal?'calProvider':'emailProvider').value,settings={},credentials={};if(provider==='smtp'){Object.assign(settings,{host:$('mailHost').value.trim(),port:$('mailPort').value.trim(),from_address:$('mailFrom').value.trim(),starttls:String($('mailTls').checked)});Object.assign(credentials,{username:$('mailUser').value,password:$('mailPassword').value})}else if(cal){settings.calendar_id=$('calCalendar').value.trim();if(provider==='microsoft_calendar')settings.user_id=$('calUser').value.trim();Object.assign(credentials,{access_token:$('calAccess').value,tenant_id:$('calTenant').value.trim(),client_id:$('calClient').value.trim(),client_secret:$('calSecret').value,refresh_token:$('calRefresh').value})}else{if(provider==='microsoft_email')settings.user_id=$('mailUserId').value.trim();else settings.from_address=$('oauthFrom').value.trim();Object.assign(credentials,{access_token:$('emailAccess').value,tenant_id:$('emailTenant').value.trim(),client_id:$('emailClient').value.trim(),client_secret:$('emailSecret').value,refresh_token:$('emailRefresh').value})}for(const o of [settings,credentials])for(const k of Object.keys(o))if(!o[k])delete o[k];try{const c=await api('/integrations/connections/'+f,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider,settings,credentials})});setState(f,c);say((cal?'Calendar':'Email')+' connection saved.');for(const id of cal?['calAccess','calTenant','calClient','calSecret','calRefresh']:['mailPassword','emailAccess','emailTenant','emailClient','emailSecret','emailRefresh'])$(id).value=''}catch(e){say('Could not save connection: '+e.message)}}async function checkConnection(f){try{const result=await api('/integrations/connections/'+f+'/check',{method:'POST'});say((f==='calendar'?'Calendar':'Email')+' connection check passed. '+(result.message||''));await loadConnections()}catch(e){say('Connection check failed: '+e.message);await loadConnections()}}async function deleteConnection(f){if(!confirm('Remove the saved '+f+' connection?'))return;try{await api('/integrations/connections/'+f,{method:'DELETE'});setState(f,null);say((f==='calendar'?'Calendar':'Email')+' connection removed.')}catch(e){say('Could not remove connection: '+e.message)}}const saved=sessionStorage.getItem('radar-token');if(saved)$('token').value=saved;providerChanged('calendar');providerChanged('email');loadConnections();
</script></body></html>'''

_OPERATIONS_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Operations</title>
<style>
:root{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{background:#10243a;color:#fff;padding:18px 26px;display:flex;justify-content:space-between;align-items:center}header h1{font-size:20px;margin:0}.bar{padding:12px 26px;background:#fff;border-bottom:1px solid #dbe2ea;display:flex;gap:9px;align-items:center;flex-wrap:wrap}.bar input{flex:1;max-width:500px;padding:8px;border:1px solid #cbd5e1;border-radius:6px}.bar a{color:#1677c8}.bar button,.refresh{border:0;border-radius:6px;padding:8px 12px;font:inherit;font-weight:650;cursor:pointer;background:#1677c8;color:#fff}.retry{border:0;border-radius:5px;padding:5px 8px;background:#e9eff6;color:#182230;cursor:pointer;font:inherit}.wrap{max-width:1400px;margin:auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:12px}.card,.panel{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:16px}.card span{display:block;color:#667085;font-size:13px}.card strong{display:block;font-size:26px;margin-top:3px}.panel{margin-top:15px}.panel h2{font-size:16px;margin:0 0 12px}.alerts{display:grid;gap:8px}.alert{padding:10px 12px;border-left:4px solid #d92d20;background:#fff2f0;border-radius:5px}.alert.warning{border-color:#dc6803;background:#fffaeb}.alert.ok{border-color:#039855;background:#ecfdf3}.columns{display:grid;grid-template-columns:1fr 1fr;gap:14px}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:8px;border-bottom:1px solid #e4e7ec;vertical-align:top}th{color:#667085}.error{color:#b42318}.muted{color:#667085}.healthy{color:#039855}.degraded{color:#b42318}@media(max-width:850px){.grid{grid-template-columns:repeat(2,1fr)}.columns{grid-template-columns:1fr}.wrap{padding:12px}}
</style></head><body>
<header><h1>◈ Renewal Radar / operations</h1><span id="health" class="muted">Not connected</span></header>
<div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token"><button onclick="refresh()">Refresh</button><a href="/auth/oidc/login">Sign in with SSO</a><a href="/review">Extraction review</a><a href="/tasks/inbox">Task inbox</a><a href="/renewal-risk">Portfolio risk</a><a href="/portfolio/workspace">Portfolio operations</a><a href="/integrations">Integrations</a><a href="/suppliers/workspace">Suppliers</a><a href="/obligations/workspace">Obligations</a><a href="/compliance/workspace">Compliance evidence</a><a href="/renewal-forecast">Forecast</a><label>Stuck after <select id="threshold"><option value="15">15 min</option><option value="30">30 min</option><option value="60">60 min</option></select></label></div>
<main class="wrap"><div id="notice" class="error" role="status"></div><section class="grid"><div class="card"><span>Queued jobs</span><strong id="queued">—</strong></div><div class="card"><span>Running jobs</span><strong id="running">—</strong></div><div class="card"><span>Dead letters</span><strong id="dead">—</strong></div><div class="card"><span>Retrying</span><strong id="retrying">—</strong></div><div class="card"><span>Failed notifications · 1h</span><strong id="failed">—</strong></div><div class="card"><span>Integration errors</span><strong id="integrationErrors">—</strong></div></section>
<section class="panel"><h2>Alerts</h2><div id="alerts" class="muted">Connect to load operational health.</div></section><div class="columns"><section class="panel"><h2>Recent jobs · includes calendar syncs</h2><div id="jobs" class="tablewrap muted">No data loaded.</div></section><section class="panel"><h2>Notification delivery attempts</h2><div id="deliveries" class="tablewrap muted">No data loaded.</div></section></div><section class="panel"><h2>Notification channels</h2><div id="channels" class="tablewrap muted">No data loaded.</div></section><section class="panel"><h2>Integration health</h2><div id="integrations" class="tablewrap muted">No data loaded.</div><p class="muted">Use Integrations to check a provider connection. Retry requeues a dead job; a running worker must process it.</p></section></main>
<script>
const $=id=>document.getElementById(id);function auth(){const t=$('token').value.trim()||sessionStorage.getItem('radar-token')||'';return t?{Authorization:'Bearer '+t}:{}}async function request(path,options={}){const r=await fetch(path,{...options,headers:{...auth(),...(options.headers||{})}});if(!r.ok){let e={};try{e=await r.json()}catch{}throw Error(e.detail||r.status)}return r.json()}function table(headers,rows){const t=document.createElement('table'),thead=document.createElement('thead'),tr=document.createElement('tr');headers.forEach(h=>{const th=document.createElement('th');th.textContent=h;tr.append(th)});thead.append(tr);t.append(thead);const body=document.createElement('tbody');for(const values of rows){const row=document.createElement('tr');values.forEach(v=>{const td=document.createElement('td');if(v instanceof HTMLElement)td.append(v);else td.textContent=v===null||v===undefined?'—':String(v);row.append(td)});body.append(row)}t.append(body);return t}function fillTable(id,headers,rows){const el=$(id);el.replaceChildren();if(!rows.length){el.textContent='No activity recorded.';el.className='muted';return}el.className='tablewrap';el.append(table(headers,rows))}async function retryJob(id){try{await request('/jobs/'+id+'/retry',{method:'POST'});await refresh();$('notice').textContent='Dead job queued for retry. A running worker must process it.'}catch(e){$('notice').textContent='Could not retry job: '+e.message}}async function refresh(){try{if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());const d=await request('/operations/health?stuck_after_minutes='+$('threshold').value);$('notice').textContent='';$('health').textContent=d.status+' · updated '+new Date(d.generated_at).toLocaleString();$('health').className=d.status;for(const key of ['queued','running','dead'])$(key).textContent=d.jobs.by_status[key]||0;$('retrying').textContent=d.jobs.retrying;$('failed').textContent=d.notifications.failed_last_hour;$('integrationErrors').textContent=d.integrations.error_count;const alerts=$('alerts');alerts.replaceChildren();if(!d.alerts.length){const ok=document.createElement('div');ok.className='alert ok';ok.textContent='No stuck jobs, dead letters, notification failures, or integration errors detected.';alerts.append(ok)}else{for(const a of d.alerts){const item=document.createElement('div');item.className='alert'+(a.severity==='warning'?' warning':'');item.textContent=a.message;alerts.append(item)}}fillTable('jobs',['Type','State','Attempts','Updated','Outcome','Error','Action'],d.jobs.recent.map(j=>{const retry=document.createElement('button');retry.className='retry';retry.textContent='Retry';retry.disabled=j.status!=='dead';retry.onclick=()=>retryJob(j.id);return [j.job_type,j.status,j.attempts+'/'+j.max_attempts,new Date(j.updated_at).toLocaleString(),j.result_json?JSON.stringify(JSON.parse(j.result_json)):'',j.last_error,retry]}));fillTable('deliveries',['Task','Type','Channel','State','Duration','Attempted','Error'],d.notifications.recent.map(n=>[n.task_id,n.notification_type,n.channel,n.status,n.duration_ms+' ms',new Date(n.attempted_at).toLocaleString(),n.error_message]));fillTable('channels',['Channel','Kind','State','Count'],d.notifications.by_channel.map(x=>[x.channel,x.notification_type,x.status,x.count]));fillTable('integrations',['Integration','Provider','State','Last checked','Last error'],d.integrations.connections.map(x=>[x.integration_name,x.provider,x.status,x.last_checked_at?new Date(x.last_checked_at).toLocaleString():'Never',x.last_error]))}catch(e){$('health').textContent='Unavailable';$('health').className='degraded';$('notice').textContent='Could not load operations: '+e.message}}const saved=sessionStorage.getItem('radar-token');if(saved)$('token').value=saved;refresh();
</script></body></html>'''


_RENEWAL_RISK_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Renewal risk</title>
<style>
:root{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{background:#10243a;color:#fff;padding:18px 26px;display:flex;justify-content:space-between;align-items:center}header h1{font-size:20px;margin:0}.bar{padding:12px 26px;background:#fff;border-bottom:1px solid #dbe2ea;display:flex;gap:12px;align-items:center;flex-wrap:wrap}.bar input{padding:8px;border:1px solid #cbd5e1;border-radius:6px;min-width:260px}.bar a{color:#1677c8}.bar button{border:0;border-radius:6px;padding:8px 12px;font:inherit;font-weight:650;cursor:pointer;background:#1677c8;color:#fff}.wrap{max-width:1400px;margin:auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:12px}.card,.panel{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:16px}.card span{display:block;color:#667085;font-size:13px}.card strong{display:block;font-size:25px;margin-top:3px}.panel{margin-top:15px}.panel h2{font-size:16px;margin:0 0 12px}.columns{display:grid;grid-template-columns:1fr 1fr;gap:14px}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:8px;border-bottom:1px solid #e4e7ec;vertical-align:top}th{color:#667085}.error{color:#b42318}.muted{color:#667085}.high{color:#b42318;font-weight:700}.medium{color:#b54708;font-weight:700}.low{color:#027a48;font-weight:700}@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}.columns{grid-template-columns:1fr}.wrap{padding:12px}}
</style></head><body>
<header><h1>◈ Renewal Radar / portfolio risk</h1><span id="asof" class="muted">Not connected</span></header>
<div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token"><button onclick="refresh()">Refresh</button><a href="/auth/oidc/login">Sign in with SSO</a><a href="/review">Contract review</a><a href="/tasks/inbox">Task inbox</a><a href="/operations">Operations</a></div>
<main class="wrap"><div id="notice" class="error" role="status"></div><section class="grid"><div class="card"><span>Pending reviews</span><strong id="reviews">—</strong></div><div class="card"><span>Overdue tasks</span><strong id="overdue">—</strong></div><div class="card"><span>Unassigned tasks</span><strong id="unassigned">—</strong></div><div class="card"><span>Deadlines · 30 days</span><strong id="due30">—</strong></div><div class="card"><span>Expirations · 90 days</span><strong id="exp90">—</strong></div><div class="card"><span>High-risk tasks</span><strong id="high">—</strong></div></section>
<section class="panel"><h2>Annual spend exposure</h2><p class="muted">Confirmed annual contract values for open renewal tasks. Values stay grouped by currency.</p><div class="columns"><div><strong>All open renewals</strong><div id="spend" class="muted">No confirmed values.</div></div><div><strong>High-risk renewals</strong><div id="highSpend" class="muted">No confirmed values.</div></div></div></section><section class="panel"><h2>Renewal tasks at risk</h2><div id="tasks" class="tablewrap muted">Connect to load tenant risk.</div></section><section class="panel"><h2>Contracts awaiting review</h2><div id="pending" class="tablewrap muted">No data loaded.</div></section></main>
<script>
const $=id=>document.getElementById(id);function auth(){const t=$('token').value.trim()||sessionStorage.getItem('radar-token')||'';return t?{Authorization:'Bearer '+t}:{}}function table(headers,rows){const t=document.createElement('table'),head=document.createElement('thead'),hrow=document.createElement('tr');headers.forEach(x=>{const th=document.createElement('th');th.textContent=x;hrow.append(th)});head.append(hrow);t.append(head);const body=document.createElement('tbody');for(const values of rows){const tr=document.createElement('tr');values.forEach(value=>{const td=document.createElement('td');if(value&&typeof value==='object'){td.textContent=value.text;td.className=value.level||''}else td.textContent=value===null||value===undefined?'—':String(value);tr.append(td)});body.append(tr)}t.append(body);return t}function fill(id,headers,rows){const el=$(id);el.replaceChildren();el.className=rows.length?'tablewrap':'muted';if(!rows.length){el.textContent='No items in this category.';return}el.append(table(headers,rows))}function spendLines(id,values){const el=$(id);el.replaceChildren();const entries=Object.entries(values||{});if(!entries.length){el.textContent='No confirmed values.';el.className='muted';return}el.className='';for(const [currency,amount] of entries){const line=document.createElement('div');line.textContent=currency+' '+Number(amount).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});el.append(line)}}async function refresh(){try{if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());const response=await fetch('/renewal-risk/summary',{headers:auth()});if(!response.ok){let error={};try{error=await response.json()}catch{}throw Error(error.detail||response.status)}const data=await response.json(),s=data.summary;$('notice').textContent='';$('asof').textContent='As of '+data.as_of;spendLines('spend',s.annual_spend_at_risk);spendLines('highSpend',s.annual_spend_high_risk);for(const [id,key] of [['reviews','pending_review_contracts'],['overdue','overdue_tasks'],['unassigned','unassigned_open_tasks'],['due30','renewals_due_30_days'],['exp90','expirations_next_90_days'],['high','high_risk_tasks']])$(id).textContent=s[key];fill('tasks',['Risk','Score','Contract','Owner','Workflow','Notice deadline','Expiration','Factors'],data.tasks.map(t=>[{text:t.risk_level,level:t.risk_level},t.risk_score,t.contract,t.owner_email||'Unassigned',t.workflow_state,t.due_date,t.expiration_date,t.risk_factors.join(', ')]));fill('pending',['Review age','Risk','Contract','Uploaded'],data.pending_reviews.map(c=>[c.age_days+' days',{text:c.risk_level,level:c.risk_level},c.contract,new Date(c.created_at).toLocaleDateString()]))}catch(e){$('asof').textContent='Unavailable';$('notice').textContent='Could not load renewal risk: '+e.message}}const saved=sessionStorage.getItem('radar-token');if(saved)$('token').value=saved;refresh();
</script></body></html>'''


_REVIEWER_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Review</title>
<style>
:root{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{background:#10243a;color:white;padding:20px 28px;display:flex;justify-content:space-between;align-items:center}header h1{font-size:20px;margin:0}.bar{padding:14px 28px;background:#fff;border-bottom:1px solid #dbe2ea;display:flex;gap:8px}.shell{display:grid;grid-template-columns:300px 1fr;min-height:calc(100vh - 110px)}aside{padding:18px;border-right:1px solid #dbe2ea;background:white}.item{display:block;width:100%;text-align:left;background:#fff;border:1px solid #dbe2ea;border-radius:8px;padding:12px;margin:0 0 9px;cursor:pointer}.item:hover,.item.selected{border-color:#1677c8;background:#f2f8ff}.item small{display:block;color:#64748b;margin-top:3px}main{padding:24px;display:grid;grid-template-columns:minmax(320px,1fr) minmax(320px,1fr);gap:20px}.panel{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:18px;min-width:0}.panel h2{font-size:17px;margin:0 0 14px}.field{margin:0 0 11px}.field label{display:block;font-size:12px;color:#526174;font-weight:650;margin-bottom:3px}.field input,.field select{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font:inherit}.source{white-space:pre-wrap;max-height:68vh;overflow:auto;background:#f8fafc;padding:14px;border-radius:7px;font:13px/1.6 ui-monospace,monospace}.evidence{font-size:13px;border-left:3px solid #22a06b;padding:6px 10px;margin:8px 0;background:#f4fbf7}.actions{display:flex;gap:9px;margin-top:16px}.actions button,.bar button{border:0;border-radius:6px;padding:9px 14px;font:inherit;font-weight:650;cursor:pointer}.primary{background:#1677c8;color:white}.danger{background:#fff0ef;color:#b42318}.muted{color:#667085}.status{margin-left:auto;color:#9dd8ff}#notice{margin:0;padding:0 28px;color:#b42318}.empty{padding:32px;color:#667085;text-align:center}@media(max-width:850px){.shell{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid #dbe2ea}main{grid-template-columns:1fr;padding:14px}}
</style></head><body>
<header><h1>◈ Renewal Radar <span class="muted" style="color:#b7c7d9">/ contract review</span></h1><span id="who" class="status">Not connected</span></header>
<div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token" style="flex:1;max-width:520px;padding:8px;border:1px solid #cbd5e1;border-radius:6px"><button class="primary" onclick="connect()">Connect</button><a href="/auth/oidc/login" style="align-self:center;color:#1677c8">Sign in with SSO</a><a href="/tasks/inbox" style="align-self:center;color:#1677c8">Renewal task inbox →</a><a href="/renewal-risk" style="align-self:center;color:#1677c8">Portfolio risk →</a><a href="/operations" style="align-self:center;color:#1677c8">Operations →</a><button onclick="disconnect()">Disconnect</button></div><p id="notice" role="status"></p>
<div class="shell"><aside><h2>Pending review</h2><div id="queue" class="muted">Connect to load contracts.</div></aside><main><section class="panel"><h2>Extracted terms</h2><div id="fields" class="muted">Choose a contract to inspect its proposal.</div><div id="amendmentChoice"><label for="amendmentCandidate"><strong>Possible amendment</strong></label><select id="amendmentCandidate" style="width:100%;padding:8px;margin:6px 0"><option value="">New contract (no amendment)</option></select><small class="muted">Suggestions are name-based. Confirm the relationship before previewing.</small></div><div id="amendmentPreview" class="source muted" style="max-height:30vh;margin:12px 0">Choose a contract and run a preview to see version and task changes.</div><div id="evidence"></div><div class="actions"><button class="secondary" onclick="previewAmendment()">Preview amendment</button><button class="primary" id="confirmButton" onclick="confirmContract()" disabled>Confirm terms</button><button class="danger" onclick="rejectContract()">Reject</button></div></section><section class="panel"><h2>Contract text</h2><div id="source" class="source muted">Source text appears here.</div></section></main></div>
<script>
const keys=['contract','start_date','expiration_date','renewal_notice_days','notice_day_type','notice_timezone','notice_holidays','notice_method','notice_recipient','auto_renew','termination_notice','owner_name','owner_email','annual_contract_value','currency'];let selected=null,contracts=[],previewedPayload=null;const $=id=>document.getElementById(id);function say(s){$('notice').textContent=s}function auth(){let t=$('token').value.trim();return t?{Authorization:'Bearer '+t}:{}}function disconnect(){sessionStorage.removeItem('radar-token');$('token').value='';fetch('/auth/logout',{method:'POST'});$('who').textContent='Not connected';$('queue').textContent='Connect to load contracts.';selected=null;contracts=[]}async function connect(){if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());try{let r=await fetch('/contracts?status=pending_review',{headers:auth()});if(!r.ok)throw Error((await r.json()).detail||r.status);contracts=await r.json();$('who').textContent='Connected · '+contracts.length+' awaiting review';say('');renderQueue();if(contracts.length)openContract(contracts[0].id)}catch(e){say('Could not connect: '+e.message)}}function renderQueue(){let q=$('queue');q.replaceChildren();if(!contracts.length){q.textContent='No contracts awaiting review.';return}contracts.forEach(c=>{let b=document.createElement('button');b.className='item'+(selected===c.id?' selected':'');b.onclick=()=>openContract(c.id);let title=document.createElement('strong');title.textContent=c.extracted.contract||c.filename;let meta=document.createElement('small');meta.textContent=c.filename+' · '+new Date(c.created_at).toLocaleDateString();b.append(title,meta);q.append(b)})}async function openContract(id){selected=id;previewedPayload=null;$('confirmButton').disabled=true;$('amendmentPreview').textContent='Choose a contract and run a preview to see version and task changes.';renderQueue();let c=contracts.find(x=>x.id===id);let data=c.extracted;let fields=$('fields');fields.replaceChildren();keys.forEach(k=>{let wrap=document.createElement('div');wrap.className='field';let label=document.createElement('label');label.htmlFor='f-'+k;label.textContent=k.replaceAll('_',' ')+(k==='notice_holidays'?' (comma-separated ISO dates)':k==='supersedes_contract_id'?' (active contract ID to replace)':'');let input;if(k==='notice_day_type'||k==='auto_renew'){input=document.createElement('select');let options=k==='auto_renew'?[['','Unknown'],['true','Yes'],['false','No']]:[['calendar','Calendar days'],['business','Business days']];options.forEach(([v,t])=>{let o=document.createElement('option');o.value=v;o.textContent=t;input.append(o)})}else{input=document.createElement('input');input.type=k.endsWith('_date')?'date':['renewal_notice_days','annual_contract_value'].includes(k)?'number':'text';if(['renewal_notice_days','annual_contract_value'].includes(k))input.min='0';if(k==='annual_contract_value')input.step='0.01'}input.id='f-'+k;let value=data[k];input.value=value===null||value===undefined?'':k==='auto_renew'?String(value):k==='notice_holidays'?value.join(', '):String(value);wrap.append(label,input);fields.append(wrap)});await loadAmendmentCandidates(c);let e=$('evidence');e.replaceChildren();(data.evidence||[]).forEach(item=>{let div=document.createElement('div');div.className='evidence';div.textContent=item.field+': “'+item.quote+'” · confidence '+Math.round(item.confidence*100)+'%';e.append(div)});$('source').textContent='Loading…';try{let r=await fetch('/contracts/'+id+'/source',{headers:auth()});if(!r.ok)throw Error((await r.json()).detail||r.status);$('source').textContent=(await r.json()).text}catch(err){$('source').textContent='Source unavailable: '+err.message}}async function loadAmendmentCandidates(contract){const select=$('amendmentCandidate');select.replaceChildren();const blank=document.createElement('option');blank.value='';blank.textContent='New contract (no amendment)';select.append(blank);try{const response=await fetch('/contracts/'+contract.id+'/amendment-candidates',{headers:auth()});if(!response.ok)throw Error((await response.json()).detail||response.status);const result=await response.json();for(const candidate of result.candidates){const option=document.createElement('option');option.value=candidate.contract_id;option.textContent=candidate.contract+' · match '+Math.round(candidate.match_score*100)+'% · '+candidate.match_reasons.join(', ');select.append(option)}if(contract.extracted.supersedes_contract_id)select.value=contract.extracted.supersedes_contract_id;else if(result.automatic_match)select.value=result.automatic_match.contract_id}catch(error){say('Could not load amendment suggestions: '+error.message)}}function formData(){let x={};keys.forEach(k=>{let v=$('f-'+k).value;x[k]=k==='notice_holidays'?v.split(',').map(x=>x.trim()).filter(Boolean):v===''?null:['renewal_notice_days','annual_contract_value'].includes(k)?Number(v):k==='auto_renew'?v==='true'?true:v==='false'?false:null:v});x.supersedes_contract_id=$('amendmentCandidate').value||null;x.evidence=contracts.find(c=>c.id===selected).extracted.evidence;x.obligations=contracts.find(c=>c.id===selected).extracted.obligations||[];return x}function renderAmendmentPreview(preview){const box=$('amendmentPreview');box.replaceChildren();box.className='source';const heading=document.createElement('strong');heading.textContent=preview.amendment?'Amendment impact · '+preview.supersedes.contract:'New contract · no existing version will be superseded';box.append(heading);if(preview.amendment){const table=document.createElement('table'),head=document.createElement('thead'),headerRow=document.createElement('tr');for(const label of ['Term','Existing','Proposed']){const cell=document.createElement('th');cell.textContent=label;headerRow.append(cell)}head.append(headerRow);table.append(head);const body=document.createElement('tbody');for(const [field,change] of Object.entries(preview.changes)){const row=document.createElement('tr');for(const value of [field.replaceAll('_',' '),change.before??'—',change.after??'—']){const cell=document.createElement('td');cell.textContent=value&&typeof value==='object'?JSON.stringify(value):String(value);row.append(cell)}body.append(row)}table.append(body);box.append(table);const source=document.createElement('section'),sourceTitle=document.createElement('h3');sourceTitle.textContent='Source clause comparison · exact quotes';source.append(sourceTitle);const sourceChanges=preview.source_comparison?.changes||[];if(!sourceChanges.length){const unchanged=document.createElement('p');unchanged.textContent='No source clause text differences detected.';source.append(unchanged)}else{const note=document.createElement('p');note.textContent=sourceChanges.length+' changed source excerpt(s) require reviewer attention, including any differences not represented in structured terms.';source.append(note);const inferred=document.createElement('p');inferred.textContent='Detected clause categories for playbook routing: '+(preview.detected_clause_categories||[]).join(', ');source.append(inferred);const sourceList=document.createElement('ul');for(const change of sourceChanges){const item=document.createElement('li'),label=document.createElement('strong'),quote=document.createElement('blockquote');label.textContent=change.change+' · '+change.categories.join(', ');quote.textContent='“'+change.source_quote+'”';item.append(label,quote);sourceList.append(item)}source.append(sourceList)}box.append(source);const tasks=document.createElement('p');tasks.textContent=preview.impact.open_tasks_to_resolve.length+' open task(s) will be resolved for the prior contract.';box.append(tasks);const list=document.createElement('ul');for(const oldTask of preview.impact.open_tasks_to_resolve){const item=document.createElement('li');item.textContent=oldTask.title+' · due '+oldTask.due_date+' · '+oldTask.workflow_state;list.append(item)}if(preview.impact.open_tasks_to_resolve.length)box.append(list);const priorObligations=preview.impact.open_obligations_to_review||[],proposedObligations=preview.impact.proposed_obligations_to_review||[];const obligationNote=document.createElement('p');obligationNote.textContent=priorObligations.length+' open obligation occurrence(s) will close with the prior version; '+proposedObligations.length+' extracted obligation proposal(s) need review and scheduling on the replacement.';box.append(obligationNote);if(proposedObligations.length){const proposedList=document.createElement('ul');for(const obligation of proposedObligations){const item=document.createElement('li');item.textContent=obligation.title+' · '+obligation.category.replaceAll('_',' ')+' · '+(obligation.due_date||'relative date to resolve')+(obligation.recurrence_interval_months?' · repeats every '+obligation.recurrence_interval_months+' month(s)':'');proposedList.append(item)}box.append(proposedList)}}const task=document.createElement('p');task.textContent='New task: '+preview.impact.new_task.title+' · due '+preview.impact.new_task.due_date+' · expiration '+preview.impact.new_task.expiration_date;box.append(task)}async function previewAmendment(){if(!selected)return say('Select a contract first.');try{const payload=formData(),response=await fetch('/contracts/'+selected+'/amendment-preview',{method:'POST',headers:{...auth(),'Content-Type':'application/json'},body:JSON.stringify({contract:payload})});if(!response.ok)throw Error((await response.json()).detail||response.status);renderAmendmentPreview(await response.json());previewedPayload=JSON.stringify(payload);$('confirmButton').disabled=false;say('Preview is current. Confirm only if the version and task changes look right.')}catch(error){previewedPayload=null;$('confirmButton').disabled=true;say('Could not preview amendment: '+error.message)}}async function action(path,body){if(!selected)return say('Select a contract first.');try{let r=await fetch('/contracts/'+selected+'/'+path,{method:'POST',headers:{...auth(),'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});if(!r.ok)throw Error((await r.json()).detail||r.status);say(path==='confirm'?'Contract confirmed and renewal task created.':'Contract rejected.');contracts=contracts.filter(c=>c.id!==selected);selected=null;renderQueue();$('fields').textContent='Choose a contract to inspect its proposal.';$('source').textContent='Source text appears here.';$('evidence').replaceChildren();if(contracts.length)openContract(contracts[0].id)}catch(e){say('Could not '+path+': '+e.message)}}function confirmContract(){const payload=formData();if(!previewedPayload||previewedPayload!==JSON.stringify(payload)){return say('Run a fresh preview before confirming; the reviewed terms have changed.')}action('confirm',{contract:payload})}function rejectContract(){action('reject')}const saved=sessionStorage.getItem('radar-token');if(saved){$('token').value=saved;connect()}else{connect()}
</script></body></html>'''


_TASK_INBOX_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Task inbox</title>
<style>
:root{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{background:#10243a;color:#fff;padding:18px 26px;display:flex;justify-content:space-between;align-items:center}header h1{font-size:20px;margin:0}a{color:#1677c8}.bar{padding:12px 26px;background:#fff;border-bottom:1px solid #dbe2ea;display:flex;gap:9px;align-items:center;flex-wrap:wrap}.bar input{flex:1;max-width:500px;padding:8px;border:1px solid #cbd5e1;border-radius:6px}.layout{display:grid;grid-template-columns:minmax(280px,360px) 1fr;min-height:calc(100vh - 108px)}aside{padding:18px;background:#fff;border-right:1px solid #dbe2ea}.filters{display:grid;gap:9px;margin-bottom:14px}.filters select{padding:8px;border:1px solid #cbd5e1;border-radius:6px;font:inherit}.filters label{font-size:13px;color:#526174}.task{display:block;width:100%;text-align:left;border:1px solid #dbe2ea;border-radius:8px;padding:11px;margin-bottom:8px;background:#fff;cursor:pointer}.task.selected,.task:hover{border-color:#1677c8;background:#f2f8ff}.task strong,.task small{display:block}.task small{color:#64748b;margin-top:3px}.pill{display:inline-block;border-radius:99px;padding:2px 8px;background:#e9eff6;font-size:12px}.overdue{color:#b42318;background:#fff0ef}.main{padding:20px;display:grid;grid-template-columns:minmax(280px,1.05fr) minmax(280px,.95fr);gap:14px;align-content:start}.panel{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:17px;min-width:0}.panel h2{font-size:16px;margin:0 0 12px}.panel h3{font-size:14px;margin:16px 0 6px}.panel input,.panel select,.panel textarea{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font:inherit;margin:4px 0 9px}.panel textarea{min-height:75px;resize:vertical}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.actions{display:flex;gap:8px;flex-wrap:wrap}button{border:0;border-radius:6px;padding:8px 12px;font:inherit;font-weight:650;cursor:pointer}.primary{background:#1677c8;color:white}.secondary{background:#e9eff6;color:#182230}.event{border-left:3px solid #9bb3ca;padding:6px 9px;margin:7px 0;background:#f8fafc;font-size:13px}.muted{color:#667085}.status{color:#a9d8f5}.empty{padding:24px;color:#667085;text-align:center}#notice{padding:6px 26px;color:#b42318;margin:0}.check{display:flex;align-items:center;gap:7px}.check input{width:auto;margin:0}@media(max-width:850px){.layout{grid-template-columns:1fr}aside{border:0;border-bottom:1px solid #dbe2ea}.main{grid-template-columns:1fr;padding:12px}}
</style></head><body>
<header><h1>◈ Renewal Radar / task inbox</h1><span id="who" class="status">Not connected</span></header>
<div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token"><button class="primary" onclick="loadTasks()">Connect / refresh</button><a href="/auth/oidc/login">Sign in with SSO</a><a href="/review">Extraction review</a><a href="/renewal-risk">Portfolio risk</a><a href="/portfolio/workspace">Portfolio operations</a><a href="/operations">Operations</a><a href="/integrations">Integrations</a><a href="/suppliers/workspace">Suppliers</a><a href="/obligations/workspace">Obligations</a><a href="/compliance/workspace">Compliance evidence</a><a href="/renewal-forecast">Forecast</a></div><p id="notice" role="status"></p>
<div class="layout"><aside><div class="filters"><label>Workflow state<select id="state"><option value="">All open work</option><option>review</option><option>needs_changes</option><option>pending_approval</option><option>notice_in_progress</option></select></label><label class="check"><input id="due" type="checkbox">Due or overdue today</label><label class="check"><input id="unassigned" type="checkbox">Unassigned only</label><button class="secondary" onclick="loadTasks()">Apply filters</button></div><div id="queue" class="muted">Connect to load renewal tasks.</div></aside>
<main class="main"><section class="panel"><h2 id="title">Select a task</h2><div id="summary" class="muted">Review ownership, deadlines, and workflow state.</div><div id="controls" hidden><h3>Assignment</h3><div class="grid"><input id="ownerName" placeholder="Owner name"><input id="ownerEmail" type="email" placeholder="Owner email"></div><button class="secondary" onclick="assignTask()">Save assignment</button><h3>Workflow</h3><div class="grid"><select id="nextState"><option value="review">In review</option><option value="needs_changes">Needs changes</option><option value="pending_approval">Pending approval</option><option value="notice_in_progress">Notice in progress</option><option value="renewed">Renewed</option><option value="terminated">Terminated</option><option value="cancelled">Cancelled</option></select><input id="transitionComment" placeholder="Decision note (optional)"></div><div class="actions"><button class="primary" onclick="transitionTask()">Update workflow</button><button class="secondary" onclick="resolveTask()">Resolve task</button></div><h3>Record final renewal outcome</h3><div class="grid"><select id="outcome"><option value="renewed">Renewed</option><option value="renegotiated">Renegotiated</option><option value="terminated">Terminated</option><option value="expired">Expired</option><option value="no_action">No action</option></select><input id="actualExpiration" type="date" aria-label="Actual expiration date"></div><div class="grid"><input id="actualValue" type="number" min="0" step="0.01" placeholder="Actual annual value"><input id="annualSavings" type="number" min="0" step="0.01" placeholder="Annual savings"></div><div class="grid"><input id="outcomeCurrency" maxlength="3" placeholder="Currency (e.g. USD)"><input id="successorContract" placeholder="Successor contract ID (optional)"></div><textarea id="outcomeNotes" placeholder="Decision notes"></textarea><button class="primary" onclick="recordOutcome()">Save outcome and close task</button><h3>Comments</h3><div id="comments"></div><textarea id="commentBody" placeholder="Add a team comment"></textarea><button class="secondary" onclick="addComment()">Add comment</button></div></section>
<section class="panel"><h2>Task history</h2><div id="history" class="muted">Audit events appear when a task is selected.</div><h3>Notice drafts</h3><div id="notices" class="muted">No task selected.</div></section></main></div>
<script>
let tasks=[],selected=null;const $=id=>document.getElementById(id);function say(s){$('notice').textContent=s}function auth(){const t=$('token').value.trim()||sessionStorage.getItem('radar-token')||'';return t?{Authorization:'Bearer '+t}:{}}async function api(path,options={}){const r=await fetch(path,{...options,headers:{...auth(),...(options.headers||{})}});if(!r.ok){let e={};try{e=await r.json()}catch{}throw Error(e.detail||r.status)}return r.status===204?null:r.json()}function localDate(){const d=new Date();return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,10)}async function loadTasks(){try{if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());const q=new URLSearchParams({status:'open'});if($('state').value)q.set('workflow_state',$('state').value);if($('due').checked)q.set('due_before',localDate());if($('unassigned').checked)q.set('unassigned','true');tasks=await api('/tasks?'+q);$('who').textContent=tasks.length+' open task'+(tasks.length===1?'':'s');say('');renderQueue();if(selected&&tasks.some(t=>t.id===selected))openTask(selected);else if(tasks.length)openTask(tasks[0].id);else clearTask()}catch(e){say('Could not load tasks: '+e.message)}}function renderQueue(){const q=$('queue');q.replaceChildren();if(!tasks.length){q.textContent='No tasks match these filters.';q.className='empty';return}q.className='';for(const t of tasks){const b=document.createElement('button');b.className='task'+(t.id===selected?' selected':'');b.onclick=()=>openTask(t.id);const s=document.createElement('strong');s.textContent=(t.kind==='obligation'?'Obligation · ':'')+t.title;const d=document.createElement('small');d.textContent='Due '+t.due_date+' · '+(t.owner_name||t.owner_email||'Unassigned');const p=document.createElement('span');p.className='pill'+(t.due_date<localDate()?' overdue':'');p.textContent=t.workflow_state.replaceAll('_',' ');b.append(s,d,p);q.append(b)}}function clearTask(){selected=null;$('title').textContent='Select a task';$('summary').textContent='No open tasks match these filters.';$('controls').hidden=true;$('history').textContent='';$('notices').textContent='';renderQueue()}async function openTask(id){selected=id;renderQueue();const t=tasks.find(x=>x.id===id);$('title').textContent=t.title;$('summary').textContent='Notice deadline '+t.due_date+' · Contract expiration '+t.expiration_date+' · '+(t.owner_email||'Unassigned');$('controls').hidden=false;$('ownerName').value=t.owner_name||'';$('ownerEmail').value=t.owner_email||'';$('nextState').value=t.workflow_state;$('transitionComment').value='';try{const [comments,notices]=await Promise.all([api('/tasks/'+id+'/comments'),api('/tasks/'+id+'/notices')]);let events=[];try{events=await api('/contracts/'+t.contract_id+'/audit')}catch{}renderEvents('comments',comments.map(c=>({title:c.actor+' · '+new Date(c.created_at).toLocaleString(),detail:c.body})));renderEvents('history',events.slice(-20).reverse().map(e=>({title:e.event_type.replaceAll('.',' · ')+' · '+e.actor+' · '+new Date(e.created_at).toLocaleString(),detail:JSON.stringify(e.details)})));renderEvents('notices',notices.map(n=>({title:n.status+' · '+n.subject,detail:n.recipient+' · '+n.delivery_method})));}catch(e){say('Could not load task history: '+e.message)}}function renderEvents(id,events){const el=$(id);el.replaceChildren();if(!events.length){el.textContent='No history yet.';el.className='muted';return}el.className='';for(const event of events){const div=document.createElement('div');div.className='event';const strong=document.createElement('strong');strong.textContent=event.title;const detail=document.createElement('div');detail.textContent=event.detail;div.append(strong,detail);el.append(div)}}async function assignTask(){try{await api('/tasks/'+selected+'/assign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({owner_name:$('ownerName').value||null,owner_email:$('ownerEmail').value||null})});say('Assignment saved.');await loadTasks()}catch(e){say('Could not assign task: '+e.message)}}async function transitionTask(){try{await api('/tasks/'+selected+'/transition',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workflow_state:$('nextState').value,comment:$('transitionComment').value||null})});say('Workflow updated.');selected=null;await loadTasks()}catch(e){say('Could not update workflow: '+e.message)}}async function resolveTask(){if(!confirm('Resolve this renewal task?'))return;try{await api('/tasks/'+selected+'/resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({comment:$('transitionComment').value||null})});say('Task resolved.');selected=null;await loadTasks()}catch(e){say('Could not resolve task: '+e.message)}}async function recordOutcome(){const payload={outcome:$('outcome').value,actual_expiration_date:$('actualExpiration').value||null,actual_annual_value:$('actualValue').value||null,annual_savings:$('annualSavings').value||null,currency:$('outcomeCurrency').value.trim().toUpperCase()||null,successor_contract_id:$('successorContract').value.trim()||null,notes:$('outcomeNotes').value||null};for(const key of ['actual_expiration_date','actual_annual_value','annual_savings','currency','successor_contract_id','notes'])if(payload[key]===null)delete payload[key];try{await api('/tasks/'+selected+'/outcome',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});say('Outcome recorded and task closed.');selected=null;await loadTasks()}catch(e){say('Could not record outcome: '+e.message)}}async function addComment(){try{await api('/tasks/'+selected+'/comments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({body:$('commentBody').value})});$('commentBody').value='';say('Comment added.');await openTask(selected)}catch(e){say('Could not add comment: '+e.message)}}const saved=sessionStorage.getItem('radar-token');if(saved)$('token').value=saved;loadTasks();
</script></body></html>'''


_COMPLIANCE_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Obligation compliance</title><style>
body{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa;margin:0}header{background:#10243a;color:white;padding:20px 28px}main{max-width:1050px;margin:24px auto;padding:0 18px}.bar,.panel{background:white;border:1px solid #dbe2ea;border-radius:9px;padding:16px;margin:12px 0}.bar{display:flex;gap:10px;flex-wrap:wrap}.bar a{color:#1677c8}input,select,textarea,button{font:inherit;padding:9px;border:1px solid #cbd5e1;border-radius:6px;margin:4px 0}input,select,textarea{width:100%;box-sizing:border-box}button{background:#1677c8;color:white;border:0;cursor:pointer;font-weight:650;width:auto}.secondary{background:#e9eff6;color:#182230}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.entry{border-top:1px solid #e5eaf0;padding:10px 0}.muted{color:#64748b}#notice{color:#b42318;min-height:22px}@media(max-width:700px){.row{grid-template-columns:1fr}}</style></head><body><header><h1>Obligation evidence and compliance history</h1></header><main><div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)"><button onclick="loadObligations()">Connect / refresh</button><a href="/auth/oidc/login">Sign in with SSO</a><a href="/obligations/workspace">Obligations</a><a href="/compliance/workspace">Compliance evidence</a><a href="/tasks/inbox">Task inbox</a></div><p id="notice" role="status"></p><section class="panel"><h2>Occurrence</h2><select id="obligation" onchange="loadHistory()"><option value="">Load an obligation</option></select><div id="summary" class="muted">Choose an occurrence to see its proof and compliance history.</div></section><section class="panel"><h2>Submit completion evidence</h2><p>Proof is stored with a SHA-256 checksum. Submission requests reviewer sign-off; the recurring schedule advances after approval.</p><input id="file" type="file" accept=".pdf,.png,.jpg,.jpeg,.txt,application/pdf,image/png,image/jpeg,text/plain"><textarea id="comment" maxlength="2000" placeholder="What does this evidence show?"></textarea><button onclick="submitEvidence()">Upload proof for reviewer sign-off</button></section><section class="panel"><h2>Record an exception</h2><p>Record a failed or missed obligation with a note. A missed occurrence can only be recorded after its due date.</p><select id="outcome"><option value="failed">Failed / did not meet requirement</option><option value="missed">Missed deadline</option></select><textarea id="failureComment" maxlength="2000" placeholder="Explain what happened"></textarea><button class="secondary" onclick="recordException()">Record compliance exception</button></section><section class="panel"><h2>Evidence and review history</h2><div id="history" class="muted">No occurrence selected.</div></section></main><script>
const $=id=>document.getElementById(id);function auth(){const t=$('token').value.trim()||sessionStorage.getItem('radar-token')||'';return t?{Authorization:'Bearer '+t}:{}}async function api(path,options={}){const r=await fetch(path,{...options,headers:{...auth(),...(options.headers||{})}});if(!r.ok){let e={};try{e=await r.json()}catch{}throw Error(e.detail||r.status)}return r.status===204?null:r.json()}function say(x){$('notice').textContent=x}async function loadObligations(){try{if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());const rows=await api('/obligations'),select=$('obligation');select.replaceChildren();for(const o of rows){const option=document.createElement('option');option.value=o.id;option.textContent=o.title+' · '+o.due_date+' · '+(o.owner_email||'unassigned')+' · '+o.status;select.append(option)}if(rows.length){const requested=new URLSearchParams(location.search).get('obligation');select.value=rows.some(row=>row.id===requested)?requested:rows[0].id;await loadHistory()}else{$('summary').textContent='No obligations are available for this account.';$('history').textContent=''}}catch(e){say('Could not load obligations: '+e.message)}}async function submitEvidence(){const id=$('obligation').value,file=$('file').files[0];if(!id||!file)return say('Choose an obligation and an evidence file.');if(file.size>5*1024*1024)return say('Evidence must be 5 MB or smaller.');try{const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file)});await api('/obligations/'+encodeURIComponent(id)+'/evidence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:file.name,content_type:file.type||'application/octet-stream',content_base64:encoded,comment:$('comment').value.trim()||null})});say('Evidence submitted. A reviewer must sign off before the occurrence is complete.');$('file').value='';await loadHistory()}catch(e){say('Could not submit evidence: '+e.message)}}async function recordException(){const id=$('obligation').value,comment=$('failureComment').value.trim();if(!id||!comment)return say('Choose an obligation and describe the exception.');try{await api('/obligations/'+encodeURIComponent(id)+'/compliance',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({outcome:$('outcome').value,comment})});$('failureComment').value='';say('Compliance exception recorded in the audit history.');await loadHistory()}catch(e){say('Could not record exception: '+e.message)}}async function loadHistory(){const id=$('obligation').value;if(!id)return;try{const o=(await api('/obligations')).find(row=>row.id===id),data=await api('/obligations/'+encodeURIComponent(id)+'/compliance');$('summary').textContent=o.title+' · due '+o.due_date+' · '+o.status+' · owner '+(o.owner_email||'unassigned');const root=$('history');root.replaceChildren();for(const event of data.history){const entry=document.createElement('div');entry.className='entry';entry.textContent=event.outcome+' · '+event.actor+' · '+new Date(event.created_at).toLocaleString()+' · '+event.comment;root.append(entry)}for(const item of data.evidence){const entry=document.createElement('div');entry.className='entry';const details=document.createElement('span');details.textContent=item.filename+' · '+item.status+' · SHA-256 '+item.sha256+(item.comment?' · '+item.comment:'');entry.append(details);const download=document.createElement('button');download.className='secondary';download.textContent='Download proof';download.onclick=async()=>{const response=await fetch('/obligation-evidence/'+item.id+'/download',{headers:auth()});if(!response.ok)return say('Could not download proof.');const link=document.createElement('a');link.href=URL.createObjectURL(await response.blob());link.download=item.filename;link.click();URL.revokeObjectURL(link.href)};entry.append(download);if(item.status==='pending_signoff'){for(const decision of ['approved','rejected']){const button=document.createElement('button');button.className='secondary';button.textContent=decision==='approved'?'Approve proof':'Reject proof';button.onclick=async()=>{try{await api('/obligation-evidence/'+item.id+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision,comment:prompt('Optional reviewer note')||null})});say('Evidence '+decision+'.');await loadHistory();await loadObligations()}catch(e){say('Could not review proof: '+e.message)}};entry.append(button)}}root.append(entry)}if(!data.history.length&&!data.evidence.length)root.textContent='No evidence or compliance history has been recorded.'}catch(e){say('Could not load compliance history: '+e.message)}}const saved=sessionStorage.getItem('radar-token');if(saved)$('token').value=saved;loadObligations();
</script></body></html>'''

_PORTFOLIO_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Portfolio operations</title><style>
body{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa;margin:0}header{background:#10243a;color:white;padding:20px 28px}main{max-width:1050px;margin:24px auto;padding:0 18px}.bar,.panel{background:white;border:1px solid #dbe2ea;border-radius:9px;padding:16px;margin:12px 0}.bar{display:flex;gap:10px;flex-wrap:wrap}.bar a{color:#1677c8}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}input,select,textarea,button{font:inherit;padding:9px;border:1px solid #cbd5e1;border-radius:6px;margin:4px 0}input,select,textarea{width:100%;box-sizing:border-box}button{background:#1677c8;color:white;border:0;cursor:pointer;font-weight:650;width:auto}.secondary{background:#e9eff6;color:#182230}.entry{border-top:1px solid #e5eaf0;padding:10px 0}#notice{color:#b42318;min-height:22px}@media(max-width:700px){.grid{grid-template-columns:1fr}}</style></head><body><header><h1>Portfolio operations</h1></header><main><div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)"><button onclick="loadPlaybooks()">Connect / refresh</button><a href="/auth/oidc/login">Sign in with SSO</a><a href="/tasks/inbox">Task inbox</a><a href="/renewal-risk">Portfolio risk</a><a href="/portfolio/workspace">Portfolio operations</a><a href="/compliance/workspace">Obligation compliance</a><a href="/approvals">Approval inbox</a><a href="/approvals">Approval inbox</a></div><p id="notice" role="status"></p><section class="panel"><h2>Bulk portfolio action</h2><p>Apply one assignment or reviewer workflow action to up to 100 records. Supplier assignment accepts contract IDs; other actions accept task IDs.</p><label>Action<select id="action"><option value="assign_tasks">Assign tasks</option><option value="transition_tasks">Move tasks to pending approval</option><option value="assign_supplier">Assign supplier to contracts</option></select></label><textarea id="ids" placeholder="One record ID per line"></textarea><div class="grid"><input id="ownerName" placeholder="Owner name"><input id="ownerEmail" placeholder="Owner email"></div><input id="supplierId" placeholder="Supplier ID"><select id="riskLevel"><option>low</option><option selected>medium</option><option>high</option><option>critical</option></select><button class="secondary" onclick="updateSupplierRisk()">Save supplier risk level</button><button onclick="bulk()">Apply bulk action</button></section><section class="panel"><h2>Portfolio exports</h2><div class="grid"><select id="dataset"><option>contracts</option><option>tasks</option><option>suppliers</option><option>obligations</option></select><select id="format"><option>csv</option><option>xlsx</option></select></div><button class="secondary" onclick="downloadExport()">Download export</button></section><section class="panel"><h2>Renewal approval playbooks</h2><p>Rules match all configured criteria. A matching task enters pending approval and remains blocked until every approver signs and every checklist item is complete.</p><input id="playbookName" placeholder="Playbook name"><div class="grid"><input id="minimum" type="number" min="0" step="0.01" placeholder="Minimum annual value"><input id="currency" maxlength="3" placeholder="Currency, e.g. USD"></div><input id="risks" placeholder="Supplier risk levels, comma separated"><input id="clauses" placeholder="Changed clause categories, comma separated"><input id="approvers" placeholder="Approver email addresses, comma separated"><textarea id="checklist" placeholder="Checklist items, one per line"></textarea><button onclick="createPlaybook()" id="savePlaybook">Create playbook</button><button class="secondary" onclick="retirePlaybook()">Retire selected</button><button class="secondary" onclick="loadPlaybookHistory()">Version history</button><div id="playbooks"></div><div id="playbookHistory"></div></section><section class="panel"><h2>Test rules against the portfolio</h2><p>Simulation shows the matching criteria, any failed conditions, and the approver route without changing tasks.</p><button class="secondary" onclick="simulatePlaybooks()">Test against portfolio</button><div id="simulation"></div></section><section class="panel"><h2>Apply a playbook</h2><div class="grid"><select id="playbookSelect"></select><select id="taskSelect"></select></div><input id="applyClauses" placeholder="Optional clause override · amendment clauses are detected automatically"><button onclick="applyPlaybook()">Evaluate and apply</button><h3>Approve and complete checklist</h3><input id="reviewTask" placeholder="Renewal task ID"><button class="secondary" onclick="loadReview()">Load approval status</button><div id="review"></div><div id="reviewActions" hidden><button onclick="vote(true)">Approve</button><button class="secondary" onclick="vote(false)">Reject</button><input id="checkIndex" type="number" min="0" value="0" placeholder="Checklist item index"><button class="secondary" onclick="completeChecklist()">Complete checklist item</button></div></section><section class="panel"><h2>Provider evaluation and drift</h2><p>Run the included redacted fixture set against the rules baseline and configured LLM providers. Drift alerts trigger when exact field, evidence support, or amendment-clause scores fall by at least 10 points versus the prior run on the same fixtures.</p><button onclick="evaluateProviders()">Run provider evaluation</button><button class="secondary" onclick="loadDrift()">Refresh drift history</button><div id="drift"></div></section></main><script>
const $=id=>document.getElementById(id);function auth(){const t=$('token').value.trim()||sessionStorage.getItem('radar-token')||'';return t?{Authorization:'Bearer '+t}:{}}async function api(path,options={}){const response=await fetch(path,{...options,headers:{...auth(),...(options.headers||{})}});if(!response.ok){let e={};try{e=await response.json()}catch{}throw Error(e.detail||response.status)}return response.status===204?null:response.json()}function say(text){$('notice').textContent=text}function list(id){return $(id).value.split(/[\n,]/).map(x=>x.trim()).filter(Boolean)}async function loadPlaybooks(){try{if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());const [playbooks,tasks]=await Promise.all([api('/renewal-playbooks'),api('/tasks?status=open&kind=renewal')]);for(const [id,rows,label] of [['playbookSelect',playbooks,p=>p.name],['taskSelect',tasks,t=>t.title+' · '+t.due_date+' · '+t.id]]){const select=$(id);select.replaceChildren();for(const row of rows){const option=document.createElement('option');option.value=row.id;option.textContent=label(row);select.append(option)}}const root=$('playbooks');root.replaceChildren();for(const p of playbooks){const line=document.createElement('div');line.className='entry';line.textContent=p.name+' · v'+p.version+' · '+p.status+' · risk '+(p.supplier_risk_levels.join(', ')||'any')+' · clause '+(p.changed_clause_categories.join(', ')||'any')+' · approvers '+p.required_approvers.join(', ');const edit=document.createElement('button');edit.className='secondary';edit.textContent='Edit';edit.onclick=()=>editPlaybook(p);line.append(edit);root.append(line)}await loadDrift();const requestedTask=new URLSearchParams(location.search).get('task');if(requestedTask){$('reviewTask').value=requestedTask;await loadReview()}}catch(e){say('Could not load workspace: '+e.message)}}async function createPlaybook(){const payload={name:$('playbookName').value.trim(),min_contract_value:$('minimum').value||null,currency:$('currency').value.trim().toUpperCase()||null,supplier_risk_levels:list('risks'),changed_clause_categories:list('clauses'),required_approvers:list('approvers'),checklist:$('checklist').value.split('\n').map(x=>x.trim()).filter(Boolean)};try{await api(selectedPlaybook?'/renewal-playbooks/'+encodeURIComponent(selectedPlaybook.id):'/renewal-playbooks',{method:selectedPlaybook?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});say(selectedPlaybook?'Playbook version saved. Earlier task decisions retain their original snapshot.':'Approval playbook created.');selectedPlaybook=null;$('savePlaybook').textContent='Create playbook';await loadPlaybooks()}catch(e){say('Could not save playbook: '+e.message)}}function editPlaybook(p){selectedPlaybook=p;$('playbookName').value=p.name;$('minimum').value=p.min_contract_value||'';$('currency').value=p.currency||'';$('risks').value=p.supplier_risk_levels.join(', ');$('clauses').value=p.changed_clause_categories.join(', ');$('approvers').value=p.required_approvers.join(', ');$('checklist').value=p.checklist.join('\n');$('savePlaybook').textContent='Save as new version';say('Editing '+p.name+' · version '+p.version)}async function retirePlaybook(){if(!selectedPlaybook)return say('Choose Edit on a playbook first.');try{await api('/renewal-playbooks/'+encodeURIComponent(selectedPlaybook.id)+'/retire',{method:'POST'});say('Playbook retired. Existing task decisions remain available in history.');selectedPlaybook=null;await loadPlaybooks()}catch(e){say('Could not retire playbook: '+e.message)}}async function loadPlaybookHistory(){if(!selectedPlaybook)return say('Choose Edit on a playbook first.');try{const rows=await api('/renewal-playbooks/'+encodeURIComponent(selectedPlaybook.id)+'/history'),root=$('playbookHistory');root.replaceChildren();for(const row of rows){const entry=document.createElement('div');entry.className='entry';entry.textContent='v'+row.version+' · '+row.action+' · '+row.actor+' · '+new Date(row.created_at).toLocaleString()+' · '+row.snapshot.name;root.append(entry)}}catch(e){say('Could not load playbook history: '+e.message)}}async function simulatePlaybooks(){try{const result=await api('/renewal-playbooks/simulate'),root=$('simulation');root.replaceChildren();for(const route of result.routes){const entry=document.createElement('div');entry.className='entry';const matches=route.matches.map(m=>m.name+': '+(m.matched?'MATCH → '+m.approvers.join(', '):m.unmatched_reasons.join('; '))).join(' | ');entry.textContent=route.title+' · '+(route.route_name?'route '+route.route_name+' → '+route.route.join(', '):'no approval route')+' · detected '+(route.detected_clause_categories.join(', ')||'no amendment changes')+' · '+matches;root.append(entry)}if(!result.routes.length)root.textContent='No open renewal tasks to simulate.'}catch(e){say('Could not simulate playbooks: '+e.message)}}async function applyPlaybook(){try{const result=await api('/tasks/'+encodeURIComponent($('taskSelect').value)+'/playbooks/'+encodeURIComponent($('playbookSelect').value),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({changed_clause_categories:list('applyClauses')})});$('reviewTask').value=result.task_id;say('Playbook applied. Task is '+result.status+'.');await loadReview()}catch(e){say('Could not apply playbook: '+e.message)}}async function loadReview(){try{const id=$('reviewTask').value.trim(),review=await api('/tasks/'+encodeURIComponent(id)+'/playbook-review'),root=$('review');root.replaceChildren();const status=document.createElement('p');status.textContent='Status: '+review.status+' · Required approvers: '+review.required_approvers.join(', ');root.append(status);for(const item of review.checklist){const line=document.createElement('div');line.className='entry';line.textContent=(item.completed?'✓ ':'○ ')+item.item+(item.completed_by?' · '+item.completed_by:'');root.append(line)}for(const [email,vote] of Object.entries(review.approvals)){const line=document.createElement('div');line.className='entry';line.textContent=email+' · '+(vote===null?'awaiting decision':vote.approved?'approved':'rejected');root.append(line)}$('reviewActions').hidden=false}catch(e){$('reviewActions').hidden=true;say('Could not load approval status: '+e.message)}}async function vote(approved){try{await api('/tasks/'+encodeURIComponent($('reviewTask').value.trim())+'/playbook-review/approval',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approved})});say('Decision saved.');await loadReview()}catch(e){say('Could not save approval: '+e.message)}}async function completeChecklist(){try{await api('/tasks/'+encodeURIComponent($('reviewTask').value.trim())+'/playbook-review/checklist',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:Number($('checkIndex').value),completed:true})});say('Checklist updated.');await loadReview()}catch(e){say('Could not update checklist: '+e.message)}}async function updateSupplierRisk(){try{await api('/suppliers/'+encodeURIComponent($('supplierId').value.trim())+'/risk',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({risk_level:$('riskLevel').value})});say('Supplier risk level updated.')}catch(e){say('Could not update supplier risk: '+e.message)}}async function bulk(){const action=$('action').value,payload={action,ids:list('ids')};if(action==='assign_tasks'){payload.owner_name=$('ownerName').value.trim()||null;payload.owner_email=$('ownerEmail').value.trim()}if(action==='transition_tasks')payload.workflow_state='pending_approval';if(action==='assign_supplier')payload.supplier_id=$('supplierId').value.trim();try{const result=await api('/portfolio/bulk',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});say('Updated '+result.count+' portfolio records.');await loadPlaybooks()}catch(e){say('Bulk action failed: '+e.message)}}async function downloadExport(){try{const response=await fetch('/portfolio/export?'+new URLSearchParams({dataset:$('dataset').value,format:$('format').value}),{headers:auth()});if(!response.ok){const e=await response.json();throw Error(e.detail||response.status)}const link=document.createElement('a');link.href=URL.createObjectURL(await response.blob());link.download='renewal-radar-'+$('dataset').value+'.'+$('format').value;link.click();URL.revokeObjectURL(link.href);say('Portfolio export downloaded.')}catch(e){say('Could not download export: '+e.message)}}async function evaluateProviders(){try{say('Running evaluations. Configured providers may take a few minutes and incur provider charges.');const result=await api('/evaluations/runs',{method:'POST'});say('Saved '+result.runs.length+' provider run(s).');renderDrift(result.runs)}catch(e){say('Could not run evaluations: '+e.message)}}function renderDrift(rows){const root=$('drift');root.replaceChildren();for(const row of rows){const item=document.createElement('div');item.className='entry';item.textContent=row.provider+' · exact '+row.exact_accuracy+' · evidence '+row.evidence_support_accuracy+' · clauses '+row.clause_accuracy+' · amendment comparison '+row.amendment_comparison_accuracy+' · '+(row.alerts.length?row.alerts.length+' drift alert(s)':'no drift alert');root.append(item)}}async function loadDrift(){try{const data=await api('/evaluations/drift');renderDrift(data.runs);for(const alert of data.alerts){const line=document.createElement('div');line.className='entry';line.textContent='ALERT '+alert.provider+' '+alert.metric+' dropped '+alert.drop+' (from '+alert.previous+' to '+alert.current+')';$('drift').prepend(line)}}catch(e){say('Could not load evaluation history: '+e.message)}}const saved=sessionStorage.getItem('radar-token');if(saved)$('token').value=saved;loadPlaybooks();
</script></body></html>'''


_APPROVALS_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Approval inbox</title><style>
body{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa;margin:0}header{background:#10243a;color:white;padding:20px 28px}main{max-width:1100px;margin:24px auto;padding:0 18px}.bar,.panel,.item{background:white;border:1px solid #dbe2ea;border-radius:9px;padding:15px;margin:12px 0}.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.bar input{flex:1;min-width:240px}.bar a{color:#1677c8}input,button{font:inherit;padding:9px;border:1px solid #cbd5e1;border-radius:6px}button{background:#1677c8;color:white;border:0;cursor:pointer;font-weight:650;margin:3px}.secondary{background:#e9eff6;color:#182230}.item{display:flex;gap:14px;align-items:center;justify-content:space-between}.meta{color:#64748b;font-size:13px}.breach{color:#b42318;font-weight:700}.good{color:#027a48}.actions{white-space:nowrap}.delivery{border-top:1px solid #e5eaf0;padding:8px 0;font-size:13px}#notice{color:#b42318}@media(max-width:700px){.item{display:block}.actions{margin-top:8px}}</style></head><body><header><h1>◈ Renewal Radar / approval inbox</h1></header><main><div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token"><button onclick="load()">Refresh</button><a href="/auth/oidc/login">Sign in with SSO</a><a href="/portfolio/workspace">Playbooks</a><a href="/compliance/workspace">Obligation evidence</a><a href="/tasks/inbox">Task inbox</a></div><p id="notice" role="status"></p><section class="panel"><h2>Pending reviewer decisions</h2><p class="meta" id="asof">Loading…</p><div id="items"></div></section><section class="panel"><h2>Notification delivery audit</h2><div id="deliveries" class="meta">No delivery attempts.</div></section></main><script>
const $=id=>document.getElementById(id);function auth(){const t=$('token').value.trim()||sessionStorage.getItem('radar-token')||'';return t?{Authorization:'Bearer '+t}:{}}async function api(path,options={}){const r=await fetch(path,{...options,headers:{...auth(),...(options.headers||{})}});if(!r.ok){let e={};try{e=await r.json()}catch{}throw Error(e.detail||r.status)}return r.json()}function node(tag,text,cls){const e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e}async function decide(item,decision){try{if(item.type==='obligation_signoff'){await api('/obligation-evidence/'+encodeURIComponent(item.target_id)+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision})})}else{await api('/tasks/'+encodeURIComponent(item.task_id)+'/playbook-review/approval',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approved:decision==='approved'})})}await load()}catch(e){$('notice').textContent='Could not record decision: '+e.message}}async function load(){try{if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());const data=await api('/approvals/inbox');$('notice').textContent='';$('asof').textContent='As of '+data.as_of+' · SLA '+data.sla_days+' days · '+data.items.length+' pending';const root=$('items');root.replaceChildren();if(!data.items.length)root.append(node('p','No pending approval decisions.','meta'));for(const item of data.items){const card=document.createElement('article');card.className='item';const info=document.createElement('div'),age=node('div',item.age_days+' day(s) waiting · '+(item.sla_breached?'SLA breached':'SLA in '+Math.max(0,item.sla_days-item.age_days)+' day(s)'),item.sla_breached?'breach':'good');info.append(node('strong',item.subject+' · '+item.type.replaceAll('_',' ')),node('div',(item.contract||item.contract_id)+' · task '+item.task_id,'meta'),age,node('div','Reviewers: '+(item.reviewers.join(', ')||'configured reviewer group'),'meta'));const open=node('a',item.type==='obligation_signoff'?'Inspect evidence and compliance history':'Open playbook review');open.href=item.type==='obligation_signoff'?'/compliance/workspace?obligation='+encodeURIComponent(item.obligation_id):'/portfolio/workspace?task='+encodeURIComponent(item.task_id);info.append(open);const actions=document.createElement('div');actions.className='actions';for(const [label,value,cls] of [['Approve','approved',''],['Reject','rejected','secondary']]){const b=node('button',label,cls);b.onclick=()=>decide(item,value);actions.append(b)}card.append(info,actions);root.append(card)}const deliveries=$('deliveries');deliveries.replaceChildren();if(!data.deliveries.length)deliveries.textContent='No approval reminders or escalations have been attempted.';for(const d of data.deliveries){const row=node('div',new Date(d.attempted_at).toLocaleString()+' · '+d.target_type+' · '+d.status+' · '+(d.recipient||'log')+' · '+d.channel+(d.error_message?' · '+d.error_message:''),'delivery');deliveries.append(row)}}catch(e){$('notice').textContent='Could not load approval inbox: '+e.message}}const saved=sessionStorage.getItem('radar-token');if(saved)$('token').value=saved;load();
</script></body></html>'''


app = create_app()
