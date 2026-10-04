from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import Cookie, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .auth import AuthRegistry, Principal
from .calendar import export_ics
from .calendar_sync import CalendarSyncError
from .calendar_sync import sync_calendar
from .documents import DocumentError, extract_pdf_text
from .extractor import ExtractionError, get_extractor
from .reminders import run_reminders
from .schemas import AuditEvent, ConfirmationRequest, ContractData, ContractRecord, ReminderTask, ResolveTaskRequest
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

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/review", response_class=HTMLResponse, include_in_schema=False)
    def reviewer_workspace() -> HTMLResponse:
        return HTMLResponse(_REVIEWER_HTML)

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
        db.save_contract(contract_id, filename, provider, extracted, text, principal.actor, principal.tenant_id)
        return _contract_record(db.get_contract(contract_id, principal.tenant_id))

    @app.get("/contracts", response_model=list[ContractRecord])
    def list_contracts(
        status: str | None = Query(default=None, pattern="^(pending_review|active|rejected|superseded)$"),
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
        principal: Principal = Depends(authenticate),
    ) -> list[ReminderTask]:
        require_scope(principal, "tasks:read")
        owner_only = "owner" in principal.roles and not (principal.is_admin or "reviewer" in principal.roles)
        if owner_only and not principal.email:
            raise HTTPException(status_code=403, detail="An email address is required for owner-scoped task access.")
        rows = db.list_tasks(status, owner_email=principal.email if owner_only else None, tenant_id=principal.tenant_id)
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

    @app.post("/reminders/run")
    def trigger_reminders(principal: Principal = Depends(authenticate)) -> dict[str, int]:
        require_scope(principal, "reminders:run")
        return run_reminders(db)

    @app.post("/calendar/sync")
    def calendar_sync(principal: Principal = Depends(authenticate)) -> dict[str, int | str]:
        require_scope(principal, "calendar:sync")
        try:
            return sync_calendar(db, principal.tenant_id)
        except CalendarSyncError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

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


def _task_record(row) -> ReminderTask:
    return ReminderTask(
        id=row["id"], contract_id=row["contract_id"], title=row["title"], owner_name=row["owner_name"], owner_email=row["owner_email"],
        due_date=row["due_date"], expiration_date=row["expiration_date"], status=row["status"],
        reminder_sent_at=row["reminder_sent_at"], escalated_at=row["escalated_at"], created_at=row["created_at"],
        tenant_id=row["tenant_id"],
        notice_day_type=row["notice_day_type"], notice_timezone=row["notice_timezone"],
        notice_holidays=json.loads(row["notice_holidays_json"]),
    )


def _secure_cookies() -> bool:
    return os.getenv("RADAR_COOKIE_SECURE", "true").strip().casefold() not in {"0", "false", "no"}


_REVIEWER_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Renewal Radar · Review</title>
<style>
:root{font:15px/1.5 system-ui,sans-serif;color:#182230;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{background:#10243a;color:white;padding:20px 28px;display:flex;justify-content:space-between;align-items:center}header h1{font-size:20px;margin:0}.bar{padding:14px 28px;background:#fff;border-bottom:1px solid #dbe2ea;display:flex;gap:8px}.shell{display:grid;grid-template-columns:300px 1fr;min-height:calc(100vh - 110px)}aside{padding:18px;border-right:1px solid #dbe2ea;background:white}.item{display:block;width:100%;text-align:left;background:#fff;border:1px solid #dbe2ea;border-radius:8px;padding:12px;margin:0 0 9px;cursor:pointer}.item:hover,.item.selected{border-color:#1677c8;background:#f2f8ff}.item small{display:block;color:#64748b;margin-top:3px}main{padding:24px;display:grid;grid-template-columns:minmax(320px,1fr) minmax(320px,1fr);gap:20px}.panel{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:18px;min-width:0}.panel h2{font-size:17px;margin:0 0 14px}.field{margin:0 0 11px}.field label{display:block;font-size:12px;color:#526174;font-weight:650;margin-bottom:3px}.field input,.field select{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font:inherit}.source{white-space:pre-wrap;max-height:68vh;overflow:auto;background:#f8fafc;padding:14px;border-radius:7px;font:13px/1.6 ui-monospace,monospace}.evidence{font-size:13px;border-left:3px solid #22a06b;padding:6px 10px;margin:8px 0;background:#f4fbf7}.actions{display:flex;gap:9px;margin-top:16px}.actions button,.bar button{border:0;border-radius:6px;padding:9px 14px;font:inherit;font-weight:650;cursor:pointer}.primary{background:#1677c8;color:white}.danger{background:#fff0ef;color:#b42318}.muted{color:#667085}.status{margin-left:auto;color:#9dd8ff}#notice{margin:0;padding:0 28px;color:#b42318}.empty{padding:32px;color:#667085;text-align:center}@media(max-width:850px){.shell{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid #dbe2ea}main{grid-template-columns:1fr;padding:14px}}
</style></head><body>
<header><h1>◈ Renewal Radar <span class="muted" style="color:#b7c7d9">/ contract review</span></h1><span id="who" class="status">Not connected</span></header>
<div class="bar"><input id="token" type="password" placeholder="Bearer token (optional with SSO)" aria-label="Bearer token" style="flex:1;max-width:520px;padding:8px;border:1px solid #cbd5e1;border-radius:6px"><button class="primary" onclick="connect()">Connect</button><a href="/auth/oidc/login" style="align-self:center;color:#1677c8">Sign in with SSO</a><button onclick="disconnect()">Disconnect</button></div><p id="notice" role="status"></p>
<div class="shell"><aside><h2>Pending review</h2><div id="queue" class="muted">Connect to load contracts.</div></aside><main><section class="panel"><h2>Extracted terms</h2><div id="fields" class="muted">Choose a contract to inspect its proposal.</div><div id="evidence"></div><div class="actions"><button class="primary" onclick="confirmContract()">Confirm terms</button><button class="danger" onclick="rejectContract()">Reject</button></div></section><section class="panel"><h2>Contract text</h2><div id="source" class="source muted">Source text appears here.</div></section></main></div>
<script>
const keys=['contract','start_date','expiration_date','renewal_notice_days','notice_day_type','notice_timezone','notice_holidays','notice_method','notice_recipient','auto_renew','termination_notice','owner_name','owner_email','supersedes_contract_id'];let selected=null,contracts=[];const $=id=>document.getElementById(id);function say(s){$('notice').textContent=s}function auth(){let t=$('token').value.trim();return t?{Authorization:'Bearer '+t}:{}}function disconnect(){sessionStorage.removeItem('radar-token');$('token').value='';fetch('/auth/logout',{method:'POST'});$('who').textContent='Not connected';$('queue').textContent='Connect to load contracts.';selected=null;contracts=[]}async function connect(){if($('token').value.trim())sessionStorage.setItem('radar-token',$('token').value.trim());try{let r=await fetch('/contracts?status=pending_review',{headers:auth()});if(!r.ok)throw Error((await r.json()).detail||r.status);contracts=await r.json();$('who').textContent='Connected · '+contracts.length+' awaiting review';say('');renderQueue();if(contracts.length)openContract(contracts[0].id)}catch(e){say('Could not connect: '+e.message)}}function renderQueue(){let q=$('queue');q.replaceChildren();if(!contracts.length){q.textContent='No contracts awaiting review.';return}contracts.forEach(c=>{let b=document.createElement('button');b.className='item'+(selected===c.id?' selected':'');b.onclick=()=>openContract(c.id);let title=document.createElement('strong');title.textContent=c.extracted.contract||c.filename;let meta=document.createElement('small');meta.textContent=c.filename+' · '+new Date(c.created_at).toLocaleDateString();b.append(title,meta);q.append(b)})}async function openContract(id){selected=id;renderQueue();let c=contracts.find(x=>x.id===id);let data=c.extracted;let fields=$('fields');fields.replaceChildren();keys.forEach(k=>{let wrap=document.createElement('div');wrap.className='field';let label=document.createElement('label');label.htmlFor='f-'+k;label.textContent=k.replaceAll('_',' ')+(k==='notice_holidays'?' (comma-separated ISO dates)':k==='supersedes_contract_id'?' (active contract ID to replace)':'');let input;if(k==='notice_day_type'||k==='auto_renew'){input=document.createElement('select');let options=k==='auto_renew'?[['','Unknown'],['true','Yes'],['false','No']]:[['calendar','Calendar days'],['business','Business days']];options.forEach(([v,t])=>{let o=document.createElement('option');o.value=v;o.textContent=t;input.append(o)})}else{input=document.createElement('input');input.type=k.endsWith('_date')?'date':k==='renewal_notice_days'?'number':'text';if(k==='renewal_notice_days')input.min='0'}input.id='f-'+k;let value=data[k];input.value=value===null||value===undefined?'':k==='auto_renew'?String(value):k==='notice_holidays'?value.join(', '):String(value);wrap.append(label,input);fields.append(wrap)});let e=$('evidence');e.replaceChildren();(data.evidence||[]).forEach(item=>{let div=document.createElement('div');div.className='evidence';div.textContent=item.field+': “'+item.quote+'” · confidence '+Math.round(item.confidence*100)+'%';e.append(div)});$('source').textContent='Loading…';try{let r=await fetch('/contracts/'+id+'/source',{headers:auth()});if(!r.ok)throw Error((await r.json()).detail||r.status);$('source').textContent=(await r.json()).text}catch(err){$('source').textContent='Source unavailable: '+err.message}}function formData(){let x={};keys.forEach(k=>{let v=$('f-'+k).value;x[k]=k==='notice_holidays'?v.split(',').map(x=>x.trim()).filter(Boolean):v===''?null:k==='renewal_notice_days'?Number(v):k==='auto_renew'?v==='true'?true:v==='false'?false:null:v});x.evidence=contracts.find(c=>c.id===selected).extracted.evidence;return x}async function action(path,body){if(!selected)return say('Select a contract first.');try{let r=await fetch('/contracts/'+selected+'/'+path,{method:'POST',headers:{...auth(),'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});if(!r.ok)throw Error((await r.json()).detail||r.status);say(path==='confirm'?'Contract confirmed and renewal task created.':'Contract rejected.');contracts=contracts.filter(c=>c.id!==selected);selected=null;renderQueue();$('fields').textContent='Choose a contract to inspect its proposal.';$('source').textContent='Source text appears here.';$('evidence').replaceChildren();if(contracts.length)openContract(contracts[0].id)}catch(e){say('Could not '+path+': '+e.message)}}function confirmContract(){action('confirm',{contract:formData()})}function rejectContract(){action('reject')}const saved=sessionStorage.getItem('radar-token');if(saved){$('token').value=saved;connect()}else{connect()}
</script></body></html>'''


app = create_app()
