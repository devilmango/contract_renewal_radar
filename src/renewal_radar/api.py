from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response

from .auth import AuthRegistry, Principal
from .calendar import export_ics
from .documents import DocumentError, extract_pdf_text
from .extractor import ExtractionError, get_extractor
from .reminders import run_reminders
from .schemas import AuditEvent, ConfirmationRequest, ContractData, ContractRecord, ReminderTask, ResolveTaskRequest
from .store import Store


def create_app(store: Store | None = None, auth_registry: AuthRegistry | None = None) -> FastAPI:
    db = store or Store()
    auth = auth_registry or AuthRegistry.from_env()
    app = FastAPI(title="Contract Renewal Radar", version="0.1.0", description="Extract, review, and track contract renewal obligations.")
    app.state.store = db
    app.state.auth_registry = auth

    def authenticate(authorization: str | None = Header(default=None)) -> Principal:
        if not auth.users:
            raise HTTPException(status_code=503, detail="Authentication is not configured. Set RADAR_AUTH_USERS_JSON.")
        principal = auth.authenticate(authorization)
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

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/contracts", response_model=ContractRecord, status_code=201)
    async def ingest_contract(
        file: UploadFile = File(...), principal: Principal = Depends(authenticate)
    ) -> ContractRecord:
        require_scope(principal, "contracts:upload")
        filename = Path(file.filename or "contract.pdf").name
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=415, detail="Only PDF files are supported")
        content = await file.read()
        if len(content) > 25 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="PDF must be 25 MB or smaller")
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp:
                temp.write(content)
                temp_path = Path(temp.name)
            text, used_ocr = extract_pdf_text(temp_path)
            extractor = get_extractor()
            extracted = extractor.extract(text)
        except (DocumentError, ExtractionError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)
        provider = extractor.name + ("+ocr" if used_ocr else "")
        contract_id = str(uuid4())
        db.save_contract(contract_id, filename, provider, extracted, text, principal.actor)
        return _contract_record(db.get_contract(contract_id))

    @app.get("/contracts", response_model=list[ContractRecord])
    def list_contracts(
        status: str | None = Query(default=None, pattern="^(pending_review|active|rejected)$"),
        principal: Principal = Depends(authenticate),
    ) -> list[ContractRecord]:
        require_scope(principal, "contracts:read")
        return [_contract_record(row) for row in db.list_contracts(status)]

    @app.get("/contracts/{contract_id}", response_model=ContractRecord)
    def get_contract(contract_id: str, principal: Principal = Depends(authenticate)) -> ContractRecord:
        require_scope(principal, "contracts:read")
        row = db.get_contract(contract_id)
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        return _contract_record(row)

    @app.post("/contracts/{contract_id}/confirm", response_model=ContractRecord)
    def confirm_contract(
        contract_id: str,
        request: ConfirmationRequest,
        principal: Principal = Depends(authenticate),
    ) -> ContractRecord:
        require_scope(principal, "contracts:review")
        try:
            db.confirm_contract(contract_id, request.contract, principal.actor)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Contract not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _contract_record(db.get_contract(contract_id))

    @app.post("/contracts/{contract_id}/reject", response_model=ContractRecord)
    def reject_contract(contract_id: str, principal: Principal = Depends(authenticate)) -> ContractRecord:
        require_scope(principal, "contracts:review")
        try:
            db.reject_contract(contract_id, principal.actor)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Contract not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _contract_record(db.get_contract(contract_id))

    @app.get("/contracts/{contract_id}/audit", response_model=list[AuditEvent])
    def get_audit(contract_id: str, principal: Principal = Depends(authenticate)) -> list[AuditEvent]:
        require_scope(principal, "audit:read")
        if not db.get_contract(contract_id):
            raise HTTPException(status_code=404, detail="Contract not found")
        return [_audit_record(row) for row in db.audit(contract_id)]

    @app.get("/tasks", response_model=list[ReminderTask])
    def list_tasks(
        status: str | None = Query(default=None, pattern="^(open|resolved)$"),
        principal: Principal = Depends(authenticate),
    ) -> list[ReminderTask]:
        require_scope(principal, "tasks:read")
        owner_only = "owner" in principal.roles and not (principal.is_admin or "reviewer" in principal.roles)
        if owner_only and not principal.email:
            raise HTTPException(status_code=403, detail="An email address is required for owner-scoped task access.")
        rows = db.list_tasks(status, owner_email=principal.email if owner_only else None)
        return [_task_record(row) for row in rows]

    @app.post("/tasks/{task_id}/resolve", response_model=ReminderTask)
    def resolve_task(
        task_id: str,
        request: ResolveTaskRequest,
        principal: Principal = Depends(authenticate),
    ) -> ReminderTask:
        require_scope(principal, "tasks:resolve")
        existing = db.get_task(task_id)
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
            row = db.resolve_task(task_id, principal.actor, request.comment)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        updated = db.get_task(task_id)
        return _task_record(updated)

    @app.get("/calendar.ics")
    def calendar(principal: Principal = Depends(authenticate)) -> Response:
        require_scope(principal, "calendar:read")
        owner_only = "owner" in principal.roles and not (principal.is_admin or "reviewer" in principal.roles)
        if owner_only and not principal.email:
            raise HTTPException(status_code=403, detail="An email address is required for owner-scoped calendar access.")
        content = export_ics(db, owner_email=principal.email if owner_only else None)
        return Response(content, media_type="text/calendar", headers={"Content-Disposition": "attachment; filename=renewal-radar.ics"})

    @app.post("/reminders/run")
    def trigger_reminders(principal: Principal = Depends(authenticate)) -> dict[str, int]:
        require_scope(principal, "reminders:run")
        return run_reminders(db)

    return app


def _contract_record(row) -> ContractRecord:
    return ContractRecord(
        id=row["id"], filename=row["filename"], status=row["status"], extraction_provider=row["provider"],
        extracted=ContractData.model_validate_json(row["extracted_json"]),
        confirmed=ContractData.model_validate_json(row["confirmed_json"]) if row["confirmed_json"] else None,
        created_at=row["created_at"], confirmed_at=row["confirmed_at"],
    )


def _audit_record(row) -> AuditEvent:
    import json

    return AuditEvent(id=row["id"], contract_id=row["contract_id"], event_type=row["event_type"], actor=row["actor"], details=json.loads(row["details_json"]), created_at=row["created_at"])


def _task_record(row) -> ReminderTask:
    return ReminderTask(
        id=row["id"], contract_id=row["contract_id"], title=row["title"], owner_name=row["owner_name"], owner_email=row["owner_email"],
        due_date=row["due_date"], expiration_date=row["expiration_date"], status=row["status"],
        reminder_sent_at=row["reminder_sent_at"], escalated_at=row["escalated_at"], created_at=row["created_at"],
    )


app = create_app()
