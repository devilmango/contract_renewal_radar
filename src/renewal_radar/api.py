from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response

from .calendar import export_ics
from .documents import DocumentError, extract_pdf_text
from .extractor import ExtractionError, get_extractor
from .reminders import run_reminders
from .schemas import AuditEvent, ConfirmationRequest, ContractData, ContractRecord, ReminderTask, ResolveTaskRequest
from .store import Store


def create_app(store: Store | None = None) -> FastAPI:
    db = store or Store()
    app = FastAPI(title="Contract Renewal Radar", version="0.1.0", description="Extract, review, and track contract renewal obligations.")
    app.state.store = db

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/contracts", response_model=ContractRecord, status_code=201)
    async def ingest_contract(file: UploadFile = File(...), x_actor: str = Header(default="api-user")) -> ContractRecord:
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
        db.save_contract(contract_id, filename, provider, extracted, text, x_actor)
        return _contract_record(db.get_contract(contract_id))

    @app.get("/contracts", response_model=list[ContractRecord])
    def list_contracts(status: str | None = Query(default=None, pattern="^(pending_review|active|rejected)$")) -> list[ContractRecord]:
        return [_contract_record(row) for row in db.list_contracts(status)]

    @app.get("/contracts/{contract_id}", response_model=ContractRecord)
    def get_contract(contract_id: str) -> ContractRecord:
        row = db.get_contract(contract_id)
        if not row:
            raise HTTPException(status_code=404, detail="Contract not found")
        return _contract_record(row)

    @app.post("/contracts/{contract_id}/confirm", response_model=ContractRecord)
    def confirm_contract(contract_id: str, request: ConfirmationRequest) -> ContractRecord:
        try:
            db.confirm_contract(contract_id, request.contract, request.actor)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Contract not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _contract_record(db.get_contract(contract_id))

    @app.post("/contracts/{contract_id}/reject", response_model=ContractRecord)
    def reject_contract(contract_id: str, x_actor: str = Header(default="reviewer")) -> ContractRecord:
        try:
            db.reject_contract(contract_id, x_actor)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Contract not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _contract_record(db.get_contract(contract_id))

    @app.get("/contracts/{contract_id}/audit", response_model=list[AuditEvent])
    def get_audit(contract_id: str) -> list[AuditEvent]:
        if not db.get_contract(contract_id):
            raise HTTPException(status_code=404, detail="Contract not found")
        return [_audit_record(row) for row in db.audit(contract_id)]

    @app.get("/tasks", response_model=list[ReminderTask])
    def list_tasks(status: str | None = Query(default=None, pattern="^(open|resolved)$")) -> list[ReminderTask]:
        return [_task_record(row) for row in db.list_tasks(status)]

    @app.post("/tasks/{task_id}/resolve", response_model=ReminderTask)
    def resolve_task(task_id: str, request: ResolveTaskRequest) -> ReminderTask:
        try:
            row = db.resolve_task(task_id, request.actor, request.comment)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        updated = db.get_task(task_id)
        return _task_record(updated)

    @app.get("/calendar.ics")
    def calendar() -> Response:
        return Response(export_ics(db), media_type="text/calendar", headers={"Content-Disposition": "attachment; filename=renewal-radar.ics"})

    @app.post("/reminders/run")
    def trigger_reminders() -> dict[str, int]:
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
