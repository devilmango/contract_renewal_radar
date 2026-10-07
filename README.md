# Contract Renewal Radar

**Turn contract dates into reviewed, owned renewal work.** Contract Renewal Radar ingests vendor agreement PDFs, extracts key terms with evidence, validates them, and waits for a person to confirm them before creating reminders.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Contract owners should not have to rediscover renewal dates by searching through a folder of PDFs. This self-hosted MVP turns expiration and notice terms into an actionable review task, then tracks reminders, escalations, calendar dates, and decisions in an audit history.

## At a glance

```text
vendor-agreement.pdf
        ↓
PDF text extraction / optional OCR
        ↓
LLM extraction or deterministic rules fallback
        ↓
Typed schema + deterministic validation + evidence
        ↓
Human confirmation and owner assignment
        ↓
Renewal task → reminder → escalation → audit trail
                      └────────────→ calendar export
```

An extraction is always a proposal. The service does not activate a contract or create its workflow until a reviewer confirms the terms.

## Features

- **PDF ingestion:** Accept PDF agreements up to 25 MB and extract selectable text.
- **OCR support:** Optionally recognize scanned pages with Tesseract.
- **Provider choice:** Use OpenAI, Anthropic Claude, Google Gemini, Mistral, Cohere, or xAI Grok with the same extraction schema, or use the deterministic rules fallback.
- **Evidence-backed proposals:** Return extracted fields with supporting text and confidence values for reviewer context.
- **Deterministic validation:** Validate date order, data types, notice-period ranges, and required terms before a contract can be activated.
- **Human review:** Correct extracted fields, assign an owner, then confirm or reject each proposal.
- **Reviewer workspace:** Open `/review` for a side-by-side proposal, evidence quotes, extracted source text, editable terms, and confirmation or rejection actions.
- **Renewal tasks:** Calculate the action date using calendar or business days, an IANA time zone, and reviewer-specified holiday dates.
- **Amendment handling:** A confirmed amendment can supersede an active agreement and close its outstanding tasks with an audit record.
- **Reminder and escalation runner:** Send one reminder when the task is due and one escalation if it remains open after the configured delay.
- **Calendar export:** Download open tasks in iCalendar format.
- **Live calendar sync:** Upsert renewal events in Microsoft 365 or Google Calendar and remove events for resolved tasks.
- **Cloud document intake:** Poll Google Drive and Microsoft Graph delta feeds or receive push notifications that queue delta reconciliation, with revision deduplication and source links.
- **Renewal workflow:** Move tasks through review, change requests, approval, notice work, renewal, termination, and cancellation with permission checks and audit history.
- **Notice preparation and delivery evidence:** Draft notice content, require reviewer approval, record dispatch details, and attach delivery confirmation evidence.
- **Durable background jobs:** Persist reminder, calendar-sync, document-reconciliation, and retention work with deduplication keys, worker leases, bounded retries, and dead-letter recovery.
- **Reviewer task inbox:** Filter upcoming work by workflow stage, deadline, and assignment; assign owners, update workflow, comment, resolve, and inspect contract history and notice drafts.
- **Operations dashboard:** Inspect queue health, retries, dead letters, stuck work, calendar-sync jobs, and email/log notification delivery attempts with threshold-based alerts.
- **Reviewer feedback evaluation:** Capture proposal-to-confirmed corrections, approve de-identified evidence-backed cases, and export them for repeatable provider comparison.
- **Contract-data governance:** Enforce an LLM provider allowlist, record authenticated access history, support legal holds, and redact eligible contract records on demand or by retention policy.
- **Evidence-quality evaluation:** Measure whether field values are supported by their evidence, track synthetic and OCR fixture results separately, and gate evidence quality in CI.
- **Organization isolation:** Contract, task, source text, audit, and calendar records are scoped by tenant ID.
- **OIDC token validation:** Verify signed RS256 or ES256 JWT access tokens against configured issuer, audience, and JWKS settings.
- **Audit trail:** Record ingestion, confirmation, rejection, task creation, notifications, escalation, and resolution.
- **Self-hosted storage:** Persist records in SQLite for a single host or PostgreSQL for multi-instance API and worker deployments; run directly with Python or with Docker Compose.

## Example

Given a confirmed agreement with a January 1, 2028 expiration date and a 90-day notice period, Radar schedules the renewal review for October 3, 2027.

```json
{
  "contract": "AWS Enterprise Agreement",
  "start_date": "2026-01-01",
  "expiration_date": "2028-01-01",
  "renewal_notice_days": 90,
  "auto_renew": true,
  "termination_notice": "90 days"
}
```

See [the complete example record](examples/contract-extraction.json).

## Quick start

### Requirements

- Python 3.11 or newer
- Optional for scanned PDFs: Tesseract executable and the Python `ocr` extra
- Optional for LLM extraction: an API key and the matching provider extra (OpenAI, Anthropic, Google, Mistral, Cohere, or xAI)
- Optional for email delivery: SMTP server credentials

### Run locally

```bash
git clone https://github.com/YOUR-ORG/contract_renewal_radar.git
cd contract_renewal_radar
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
renewal-radar serve
```

The API starts at `http://127.0.0.1:8000`. Open [`/docs`](http://127.0.0.1:8000/docs) for interactive API documentation. By default, the service creates `renewal_radar.db` in the current directory.

Workflow and contract-data endpoints require a bearer token. Create an administrator token before using them; see [Authentication and roles](#authentication-and-roles).

### Run with Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

Compose stores the database in a named volume. Configure integrations in `.env` before starting. The default container includes PDF text extraction; add OCR support to the Docker image if your deployment needs scanned PDFs.

## Configure extraction

### Rules-based extraction (default)

If no supported provider API key is configured, Radar uses its built-in rules extractor. This is useful for local evaluation and has no external model dependency. It recognizes common date labels and renewal language; contract wording varies, so the extractor may leave a value blank. Reviewers can supply or correct missing values during confirmation.

### Select an LLM provider

Set `LLM_PROVIDER` and the provider's API key. The model name can be set with `LLM_MODEL` or the provider-specific model variable.

| Provider | `LLM_PROVIDER` | Install extra | API key | Model override |
| --- | --- | --- | --- | --- |
| OpenAI | `openai` | `llm` | `OPENAI_API_KEY` | `OPENAI_MODEL` |
| Anthropic Claude | `anthropic` | `llm-anthropic` | `ANTHROPIC_API_KEY` | `ANTHROPIC_MODEL` |
| Google Gemini | `google` | `llm-google` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` | `GOOGLE_MODEL` or `GEMINI_MODEL` |
| Mistral | `mistral` | `llm-mistral` | `MISTRAL_API_KEY` | `MISTRAL_MODEL` |
| Cohere | `cohere` | `llm-cohere` | `COHERE_API_KEY` | `COHERE_MODEL` |
| xAI Grok | `xai` | `llm-xai` | `XAI_API_KEY` | `XAI_MODEL` |

Install the extra for the selected provider. For example:

```bash
pip install -e '.[llm-anthropic]'
export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY='your-api-key'
export ANTHROPIC_MODEL='claude-sonnet-5'
renewal-radar serve
```

Use `pip install -e '.[llm-all]'` to install all provider clients; xAI uses the OpenAI-compatible Python SDK. With `LLM_PROVIDER=auto` (the default), Radar selects the first configured key in this order: OpenAI, Anthropic, Google, Mistral, Cohere, xAI. Set a provider explicitly when more than one key is present. Set `LLM_PROVIDER=rules` to force the local rules extractor even when provider keys are available.

The provider adapters request schema-constrained JSON where supported and parse the result into the shared Pydantic model. Radar then applies its own deterministic validation. Model names and structured-output availability are controlled by each provider and can change; override the defaults with the model variables above. Do not treat model output as legal advice or an authoritative interpretation of a contract.

Provider API references: [OpenAI structured outputs](https://platform.openai.com/docs/guides/structured-outputs), [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs), [Gemini structured outputs](https://ai.google.dev/gemini-api/docs/structured-output), [Mistral custom structured outputs](https://docs.mistral.ai/studio/conversations/structured-output/custom), [Cohere structured outputs](https://docs.cohere.com/v2/docs/structured-outputs), and [xAI structured outputs](https://docs.x.ai/developers/model-capabilities/text/structured-outputs).

### Optional OCR

```bash
pip install -e '.[ocr]'
# Install the Tesseract executable with your operating system's package manager.
```

OCR is attempted for PDF pages without selectable text. The OCR extra installs Python packages; it does not install the Tesseract system executable.

## Contract workflow

All `curl` examples below assume `RADAR_TOKEN` contains a bearer token with the required role.

### 1. Upload a PDF

```bash
curl -X POST http://127.0.0.1:8000/contracts \
  -H "Authorization: Bearer $RADAR_TOKEN" \
  -F 'file=@vendor-agreement.pdf'
```

The service returns the proposal, its evidence, the extraction provider, a contract ID, and `pending_review` status. Non-PDF filenames are rejected. Uploads are limited to 25 MB.

### 2. Inspect the proposal

```bash
curl -H "Authorization: Bearer $RADAR_TOKEN" 'http://127.0.0.1:8000/contracts?status=pending_review'
curl -H "Authorization: Bearer $RADAR_TOKEN" http://127.0.0.1:8000/contracts/CONTRACT_ID
```

The extraction contains only terms identified in the document. Unknown values remain `null`; the reviewer is responsible for checking the PDF and supplying missing or corrected values.

### 3. Confirm terms and assign an owner

```bash
curl -X POST http://127.0.0.1:8000/contracts/CONTRACT_ID/confirm \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $RADAR_TOKEN" \
  -d '{
    "contract": {
      "contract": "AWS Enterprise Agreement",
      "start_date": "2026-01-01",
      "expiration_date": "2028-01-01",
      "renewal_notice_days": 90,
      "auto_renew": true,
      "termination_notice": "90 days",
      "owner_name": "Jane Legal",
      "owner_email": "jane@example.com"
    }
  }'
```

Confirmation creates one open task due on October 3, 2027 for this 90 calendar-day example. For business-day clauses, set `notice_day_type` to `business`; weekends and explicitly supplied `notice_holidays` are skipped. Set `notice_timezone` to an IANA zone such as `America/New_York` so reminder eligibility and escalation use the contract's local date. An expiration date is required. If `auto_renew` is true, the confirmation must also include a renewal notice period. The authenticated actor is written to the audit trail. To reject an unconfirmed proposal, call `POST /contracts/CONTRACT_ID/reject` with the same bearer header.

### 4. Send due reminders

Schedule the runner daily with cron, a systemd timer, or your job scheduler:

```bash
renewal-radar run-reminders
```

The command queues a durable, tenant-scoped reminder job and processes one job immediately. It is safe to repeat during the same day: the queue uses an idempotency key, and the task records reminder and escalation completion. With SMTP unset, it writes a notification to standard output and records the event in the audit trail.

For a separate worker process, run `renewal-radar process-jobs --loop --limit 20` under your process supervisor. Queued work survives process restarts; expired worker leases can be reclaimed, transient errors retry with backoff, and jobs that exhaust `JOB_MAX_ATTEMPTS` move to `dead`. Inspect them with `GET /jobs`, and an administrator can requeue a dead job with `POST /jobs/{job_id}/retry`. Reminder and calendar API requests now return `202 Accepted` with a job record; run a worker to execute them. Stable calendar event identifiers and notification message IDs reduce duplicate side effects when a worker retries after a process interruption.

Optional email configuration:

```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=renewal-radar
SMTP_PASSWORD=replace-me
SMTP_FROM=contracts@example.com
SMTP_STARTTLS=true
ESCALATION_EMAIL=legal-lead@example.com
ESCALATION_AFTER_DAYS=7
```

`ESCALATION_EMAIL` receives an unresolved-task escalation when configured; otherwise the task owner receives it. SMTP password handling should use your deployment's secret manager rather than a committed `.env` file.

The service also exposes `POST /reminders/run` for a scheduler that enqueues the run through HTTP. Use a token with the `scheduler` or `admin` role.

### 5. Resolve work, export dates, and review history

```bash
curl -H "Authorization: Bearer $RADAR_TOKEN" http://127.0.0.1:8000/tasks

curl -X POST http://127.0.0.1:8000/tasks/TASK_ID/resolve \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $RADAR_TOKEN" \
  -d '{"comment":"Renewed for one year."}'

curl -H "Authorization: Bearer $RADAR_TOKEN" -o renewals.ics http://127.0.0.1:8000/calendar.ics
curl -H "Authorization: Bearer $RADAR_TOKEN" http://127.0.0.1:8000/contracts/CONTRACT_ID/audit
```

Open [the reviewer workspace](http://127.0.0.1:8000/review) in a browser, sign in with OIDC or paste a reviewer bearer token, and review the proposal beside its extracted contract text. OIDC uses a short-lived HttpOnly cookie; a manually entered token stays in tab session storage.

To synchronize open tasks into a configured live calendar, call `POST /calendar/sync` with a reviewer or admin token.

## Authentication and roles

The health endpoint and generated API documentation remain public. Contract, task, calendar, audit, and reminder operations require an authorized bearer token or OIDC session. Unknown credentials receive `401`; a valid identity without the required role receives `403`. If neither static tokens nor OIDC are configured, protected endpoints fail closed with `503`.

Generate a high-entropy bearer token and a corresponding SHA-256 configuration entry:

```bash
renewal-radar create-token --actor contract-admin --role admin --email admin@example.com --tenant-id acme
```

The command prints the bearer token once and a JSON entry to add to `RADAR_AUTH_USERS_JSON`. Store the bearer token with the client or secret manager; configure only its digest in the service environment. Add multiple JSON entries to the array to provision multiple users. Example shape (replace the digest with the generated value):

```dotenv
RADAR_AUTH_USERS_JSON='[{"actor":"contract-admin","token_sha256":"<64-character-sha256>","roles":["admin"],"email":"admin@example.com","tenant_id":"acme"}]'
```

| Role | Permissions |
| --- | --- |
| `admin` | All API operations |
| `reviewer` | Upload and review contracts, view contract/access history, manage all tasks, and place or release legal holds |
| `owner` | View assigned tasks, request workflow changes, comment, prepare notice drafts, and resolve assigned tasks |
| `scheduler` | Trigger the reminder/escalation runner and poll configured document sources |

The authenticated actor name, rather than a caller-supplied `X-Actor` or request-body field, is recorded in the audit history. Tokens are compared by digest and are not stored in SQLite. Rotate a token by generating a replacement, removing the old digest from `RADAR_AUTH_USERS_JSON`, and reloading the service.

The built-in role configuration is intentionally small. An `owner` entry must include the same email address assigned to that owner's tasks. Each static token can set `tenant_id` (default `default`); principals only see data in their tenant. Provision a distinct ID for each organization.

For OIDC single sign-on, configure the issuer, JWKS URL, audience, authorization and token endpoints, client ID and secret, and an exact callback URI of `/auth/oidc/callback`. The login uses authorization code flow with state and nonce checks; the ID token is signature-validated and held in a short-lived, HttpOnly session cookie. Radar reads roles from `OIDC_ROLES_CLAIM` (default `roles`) and tenant from `OIDC_TENANT_CLAIM` (default `tenant_id`). The ID token must contain those claims and use `OIDC_AUDIENCE`. Configure the same callback URI in the IdP. `RADAR_COOKIE_SECURE=true` is correct behind HTTPS; set it to `false` only for local HTTP development. Deploy behind TLS and protect client credentials.

Reviewer permissions include source-text access so the reviewer can verify extraction evidence. Keep reviewer tokens scoped and assign role and tenant claims intentionally.
Reviewers and administrators also manage assignments, workflow transitions, comments, and notice approval. Owners can read and create notices for assigned tasks; only reviewers or administrators can approve and record dispatch/delivery evidence.

### Live calendar synchronization

Set `CALENDAR_PROVIDER=microsoft` or `google`. Microsoft accepts a short-lived `MS_GRAPH_ACCESS_TOKEN` or client credentials (`MS_GRAPH_TENANT_ID`, `MS_GRAPH_CLIENT_ID`, `MS_GRAPH_CLIENT_SECRET`), plus `MS_GRAPH_USER_ID` and `MS_GRAPH_CALENDAR_ID`. Google uses `GOOGLE_CALENDAR_ACCESS_TOKEN` and `GOOGLE_CALENDAR_ID`; provide a current access token through a secret manager or token broker. Limit provider permissions to the intended calendar.

Call `POST /calendar/sync` after configuration to enqueue a sync job. The worker creates or updates open task events and removes events when tasks are resolved or superseded. Event IDs are retained in the configured database and synchronization actions are recorded in the contract audit trail. ICS export remains available for clients that do not need live updates.

### Cloud document intake

Radar can poll a Google Drive folder or Microsoft Graph drive folder and ingest PDF files into the same extraction and human-review flow as direct uploads. Configure a provider in `.env`, then run `renewal-radar sync-documents --tenant-id acme` or call `POST /document-sources/sync` with an administrator, reviewer, or scheduler token. The poller stores provider cursors and source revisions in the configured database, so repeat runs do not create duplicate proposals. Google Drive uses its [changes feed](https://developers.google.com/workspace/drive/api/guides/manage-changes); Microsoft Graph uses [drive delta](https://learn.microsoft.com/graph/api/driveitem-delta).

| Provider | Required settings | Optional folder setting |
| --- | --- | --- |
| Google Drive | `GOOGLE_DRIVE_ACCESS_TOKEN` | `GOOGLE_DRIVE_FOLDER_ID` (defaults to root) |
| Microsoft Graph | `MS_GRAPH_DRIVE_ID` and either `MS_GRAPH_ACCESS_TOKEN` or client credentials | `MS_GRAPH_FOLDER_ITEM_ID` (defaults to drive root) |

Use least-privilege read access for the selected drive. Polling remains supported and should be scheduled as a reconciliation safety net. To use push callbacks, register `/webhooks/google-drive` as a Google Drive watch channel address or `/webhooks/microsoft-graph` as a Microsoft Graph notification URL, and set `GOOGLE_DRIVE_WEBHOOK_TOKEN` or `MS_GRAPH_WEBHOOK_CLIENT_STATE` to the matching high-entropy shared secret. Configure `DOCUMENT_SOURCE_WEBHOOK_TENANT_ID` for the tenant whose provider credentials and delta cursors should be used. Microsoft Graph's validation challenge is handled by the callback URL; callback notifications are authenticated with `clientState`. Google callbacks validate `X-Goog-Channel-Token`. Valid notifications enqueue durable delta-feed reconciliation jobs; they do not contain contract content and are safe to retry. Run `renewal-radar process-jobs --loop` to process those jobs. Provider subscriptions and their expiration/renewal remain managed by your integration deployment; keep the scheduled poller enabled to recover after missed, expired, or delayed notifications. PDF revisions become new proposals with source metadata; reviewers decide whether to confirm them as amendments.

### Reviewer task inbox

Open `/tasks/inbox` to work from the renewal queue, or use the **Renewal task inbox** link in `/review`. The inbox supports workflow-state, due-today/overdue, and unassigned filters. Reviewers can assign owners, update workflow state, resolve tasks, add comments, and inspect the contract audit history and notice drafts. Owners see only tasks assigned to their authenticated email and can use the permitted comment/workflow actions. Filtering is also available through `GET /tasks` with `workflow_state`, `due_before=YYYY-MM-DD`, and `unassigned=true` query parameters.

### Operations and delivery health

Open `/operations` or request `GET /operations/health` with a reviewer, scheduler, or administrator token. The dashboard summarizes queued/running/retrying/dead jobs, overdue queue items, expired leases, calendar-sync job results, and email/log notification attempts including their durations and failures. It raises alerts for dead letters, stuck jobs, expired leases, and three or more notification failures in the past hour. Set the `stuck_after_minutes` query parameter to adjust the queue-wait threshold (1–1440 minutes). Notification history stores task IDs and status diagnostics, not message bodies or recipient addresses. Delivery attempts are recorded before the reminder workflow marks the task notified; email remains at-least-once if a process stops after a successful send but before its state update.

### Renewal workflow and notice tracking

Confirmed renewal tasks start in `review`. Reviewers can assign an owner and transition tasks through `needs_changes`, `pending_approval`, and `notice_in_progress`, then record an outcome as `renewed`, `terminated`, or `cancelled`. Owners can request changes and submit notice work for approval. Every transition, assignment, and comment is written to the audit trail.

Create a notice draft on a task with `POST /tasks/{task_id}/notices`, including a subject, human-reviewed body, recipient, and delivery method. A reviewer must approve it before dispatch can be recorded. `POST /notices/{notice_id}/dispatch` records the sent timestamp, reference, and optional note; it does not send email or submit the notice to a vendor. Record receipt evidence with `POST /notices/{notice_id}/delivery`. Legal content and actual delivery remain under human control while Radar provides a searchable audit record. List notices with `GET /tasks/{task_id}/notices`.

### Durable jobs

Reminder runs, live calendar synchronization, document-source reconciliation, and retention sweeps are stored in the configured database as jobs. Enqueue operations return a job ID; workers claim jobs using a database transaction and expiring lease, persist outcomes, and retry failures with bounded exponential backoff. PostgreSQL workers claim jobs using `FOR UPDATE SKIP LOCKED` so multiple worker processes can safely share the queue. An expired lease makes interrupted work eligible for another worker. Exhausted jobs remain visible in the dead-letter state for review and administrator retry. Run a separate worker process under a supervisor with `renewal-radar process-jobs --loop --limit 20`.

The queue is durable across process restarts and supports a separate worker process. SQLite still serializes writers and is intended for a single host. To use PostgreSQL, install `pip install -e '.[database]'` and set `DATABASE_URL` (for example, `postgresql://radar:secret@db:5432/radar`); it takes precedence over `DATABASE_PATH`. The service creates its schema idempotently and applies additive compatibility upgrades at startup. Use a managed PostgreSQL service, backups, and a tested restore procedure for production. SMTP is at-least-once around process crashes: the stable message ID helps downstream deduplication, but SMTP itself does not guarantee exactly-once delivery.

### Data governance

Set `LLM_ALLOWED_PROVIDERS` to an explicit comma-separated list such as `rules,anthropic` to control which extraction providers may receive contract text. `rules` runs locally. The `.env.example` defaults this allowlist to `rules`; if the setting is omitted, existing deployments remain compatible and all configured providers are allowed. `LLM_PROVIDER=auto` and `renewal-radar evaluate --provider all` honor the allowlist too.

Authenticated API requests are recorded in a tenant-scoped access history without storing response bodies. Reviewers and administrators can place or release legal holds; held contracts cannot be redacted. Administrators can redact a contract with `DELETE /contracts/{contract_id}`. Redaction removes source text, extracted and confirmed terms, notices, tasks, comments, external source links, and prior contract audit details while retaining a minimal redaction audit marker and access history.

Set `CONTRACT_RETENTION_DAYS` to enable scheduled redaction of eligible old records, then run `renewal-radar run-retention` or call `POST /data-retention/run`; `0` disables retention runs. The retention job targets old rejected or superseded agreements and expired active agreements whose tasks are resolved. Pending-review agreements, upcoming active agreements, and all agreements under an active hold are excluded. Retention policy is organization-specific; confirm legal and regulatory schedules before enabling automatic redaction.

## Extraction evaluation

The `evaluation/cases/` directory contains fictional, redacted contract excerpts and OCR-style text fixtures, including amendments, ambiguous extensions, explicit no-notice cases, and calendar versus business-day terms. The evaluator reports exact-match field and case accuracy, source-type breakdowns, evidence quote grounding, whether each extracted value is supported by its field evidence, latency, provider-reported token usage, and an optional cost estimate using configured per-token rates.

Run the rules baseline locally:

```bash
renewal-radar evaluate --provider rules
```

Evaluate one configured LLM provider and save its JSON report:

```bash
renewal-radar evaluate --provider anthropic --output evaluation/reports/anthropic.json
```

Compare the rules baseline with every provider API key configured in the environment:

```bash
renewal-radar evaluate --provider all --output evaluation/reports/all-providers.json
```

Use `--min-exact-accuracy`, `--min-field-accuracy`, and `--min-evidence-support` to make quality thresholds fail the command. GitHub Actions runs the rules baseline with 0.70 exact-case and field thresholds and a 0.90 evidence-support threshold for pull requests and pushes to `main` or `master`.

`all` always includes the rules baseline, then includes configured and allowlisted LLM providers in provider-selection order. Each provider receives the same fixture texts. LLM evaluations make external API requests and may incur provider charges. This small fixture set is a regression signal, not a statistically representative measure of legal extraction quality; review the cases and add organization-approved, de-identified examples before using scores to select a provider. See the evaluation guide for an approval/de-identification record format. Do not commit confidential contracts or identifiers to a public repository.

See [evaluation/README.md](evaluation/README.md) for scoring details and fixture conventions.

### Reviewer-corrected evaluation cases

Every reviewer confirmation captures the proposed and confirmed scored terms plus the changed fields in the tenant database. Captured feedback is not automatically sent to evaluation providers or exported. A reviewer or administrator must separately approve a redacted sample with a legal approval reference, explicit de-identification/approval attestations, selected fields, clause categories, and evidence quotes that occur in the submitted redacted text and support the reviewed values.

After confirming a contract, inspect `GET /contracts/CONTRACT_ID/evaluation-feedback`, then approve a case:

```bash
curl -X POST http://127.0.0.1:8000/contracts/CONTRACT_ID/evaluation-feedback/approve \
  -H "Authorization: Bearer $RADAR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "redacted_text":"[VENDOR] Services Agreement. Effective Date: January 1, 2025. Expiration Date: February 1, 2026.",
    "approval_reference":"LEGAL-1234",
    "fields":["start_date","expiration_date"],
    "clause_categories":["effective_term","expiration"],
    "evidence":[
      {"field":"start_date","quote":"Effective Date: January 1, 2025"},
      {"field":"expiration_date","quote":"Expiration Date: February 1, 2026"}
    ],
    "attest_approved":true,
    "attest_deidentified":true
  }'
```

Export only approved cases to an access-controlled evaluation directory, then compare all configured providers against the approved examples:

```bash
renewal-radar export-feedback --tenant-id acme --output evaluation/internal-cases
renewal-radar evaluate --provider all --dataset evaluation/internal-cases --output evaluation/reports/internal-provider-comparison.json
```

The service rejects common email and US government-ID patterns and checks evidence grounding/value support. This automated scan is only a guardrail; the approving reviewer remains responsible for broader de-identification and governance. Contract redaction and retention also remove captured feedback for that contract.

## API reference

| Method | Endpoint | Permission |
| --- | --- | --- |
| `GET` | `/health` | Public health check |
| `POST` | `/contracts` | `reviewer` or `admin`: upload and extract a PDF |
| `GET` | `/contracts?status=pending_review\|active\|rejected\|superseded\|redacted` | `reviewer` or `admin`: list contracts |
| `GET` | `/contracts/{contract_id}` | `reviewer` or `admin`: view proposal and confirmed terms |
| `DELETE` | `/contracts/{contract_id}` | `admin`: redact contract data unless a legal hold is active |
| `GET/POST` | `/contracts/{contract_id}/legal-holds` | `reviewer` or `admin`: list active holds or place a hold |
| `POST` | `/legal-holds/{hold_id}/release` | `reviewer` or `admin`: release a hold |
| `GET` | `/access-history` | `reviewer` or `admin`: read authenticated access events in the current tenant |
| `GET` | `/contracts/{contract_id}/source` | `reviewer` or `admin`: view extracted contract text |
| `POST` | `/contracts/{contract_id}/confirm` | `reviewer` or `admin`: confirm terms and create a task |
| `POST` | `/contracts/{contract_id}/reject` | `reviewer` or `admin`: reject a pending proposal |
| `GET` | `/contracts/{contract_id}/audit` | `reviewer` or `admin`: read contract event history |
| `GET` | `/contracts/{contract_id}/evaluation-feedback` | `reviewer` or `admin`: inspect captured reviewer corrections and approval status |
| `POST` | `/contracts/{contract_id}/evaluation-feedback/approve` | `reviewer` or `admin`: approve a de-identified evidence-backed evaluation case |
| `GET` | `/tasks?status=open\|resolved` | `reviewer` or `admin`: list all tasks; `owner`: list assigned tasks; filter with `workflow_state`, `due_before`, or `unassigned` |
| `POST` | `/tasks/{task_id}/resolve` | `reviewer` or `admin`: resolve any task; `owner`: resolve assigned tasks |
| `POST` | `/reminders/run` | `scheduler` or `admin`: enqueue a durable reminder job (202) |
| `GET` | `/jobs` or `/jobs/{job_id}` | `reviewer`, `scheduler`, or `admin`: inspect tenant jobs |
| `GET` | `/operations/health` | `reviewer`, `scheduler`, or `admin`: inspect job and notification health with alerts |
| `POST` | `/jobs/{job_id}/retry` | `admin`: requeue a dead job |
| `POST` | `/data-retention/run` | `admin`: enqueue a configured retention sweep (202) |
| `GET` | `/calendar.ics` | `reviewer` or `admin`: export all tasks; `owner`: export assigned tasks |
| `POST` | `/calendar/sync` | `reviewer` or `admin`: enqueue live calendar synchronization (202) |
| `POST` | `/document-sources/sync` | `reviewer`, `admin`, or `scheduler`: poll configured cloud drives |
| `POST` | `/tasks/{task_id}/assign` | `reviewer` or `admin`: assign a task owner |
| `POST` | `/tasks/{task_id}/transition` | `reviewer` or `admin`: advance workflow; owners can request changes or submit notice work |
| `GET/POST` | `/tasks/{task_id}/comments` | `reviewer`, `admin`, or assigned `owner`: list/add comments |
| `GET/POST` | `/tasks/{task_id}/notices` | `reviewer`, `admin`, or assigned `owner`: list/prepare notice drafts |
| `POST` | `/notices/{notice_id}/approve` | `reviewer` or `admin`: approve a draft |
| `POST` | `/notices/{notice_id}/dispatch` | `reviewer` or `admin`: record dispatch of an approved notice |
| `POST` | `/notices/{notice_id}/delivery` | `reviewer` or `admin`: record delivery evidence |
| `GET` | `/review` | Public UI shell; API calls require a reviewer token |
| `GET` | `/tasks/inbox` | Public UI shell; API calls require task permissions |
| `GET` | `/operations` | Public UI shell; health API calls require `jobs:read` |
| `POST` | `/webhooks/google-drive` | Google Drive push callback; requires configured channel token |
| `GET/POST` | `/webhooks/microsoft-graph` | Graph validation challenge and push callback; requires configured `clientState` |

OpenAPI and interactive request examples are available at `/docs` while the service is running.

## Configuration reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | `renewal_radar.db` | SQLite database location |
| `DATABASE_URL` | unset | PostgreSQL connection URL; overrides `DATABASE_PATH` and requires the `database` extra |
| `RADAR_AUTH_USERS_JSON` | `[]` | Array of actor, token SHA-256 digest, role list, and optional email records |
| `OIDC_JWKS_URL` | unset | Enables verification of upstream RS256/ES256 JWT access tokens |
| `OIDC_ISSUER` / `OIDC_AUDIENCE` | unset | Required OIDC token issuer and API audience |
| `OIDC_AUTHORIZATION_ENDPOINT` / `OIDC_TOKEN_ENDPOINT` | unset | OIDC authorization-code endpoints |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | unset | OIDC confidential client credentials |
| `OIDC_REDIRECT_URI` | local callback example | Exact redirect URI registered at the IdP |
| `OIDC_SCOPES` | `openid profile email` | Requested OIDC scopes |
| `OIDC_ROLES_CLAIM` | `roles` | JWT claim containing Radar role names |
| `OIDC_TENANT_CLAIM` | `tenant_id` | JWT claim used to scope organization data |
| `RADAR_COOKIE_SECURE` | `true` | Mark the OIDC session cookie Secure; disable only for local HTTP |
| `LLM_PROVIDER` | `auto` | Provider selection: `auto`, `rules`, `openai`, `anthropic`, `google`, `mistral`, `cohere`, or `xai` |
| `LLM_ALLOWED_PROVIDERS` | `*` when unset | Comma-separated allowlist of extraction providers permitted to receive contract text; `rules` is local |
| `LLM_MODEL` | provider default | Optional model override for the selected provider |
| `OPENAI_API_KEY` | unset | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model override |
| `ANTHROPIC_API_KEY` | unset | Anthropic API key |
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | Anthropic model override |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | unset | Google AI Studio API key |
| `GOOGLE_MODEL` / `GEMINI_MODEL` | `gemini-3.8-flash` | Gemini model override |
| `MISTRAL_API_KEY` | unset | Mistral API key |
| `MISTRAL_MODEL` | `ministral-8b-latest` | Mistral model override |
| `COHERE_API_KEY` | unset | Cohere API key |
| `COHERE_MODEL` | `command-a-plus-05-2026` | Cohere model override |
| `XAI_API_KEY` | unset | xAI API key |
| `XAI_MODEL` | `grok-4.7` | xAI model override |
| `SMTP_HOST` | unset | Enables email delivery |
| `SMTP_PORT` | `587` | SMTP server port |
| `SMTP_USERNAME` | unset | Optional SMTP authentication username |
| `SMTP_PASSWORD` | unset | Optional SMTP authentication password |
| `SMTP_FROM` | `renewal-radar@example.com` | Sender address |
| `SMTP_STARTTLS` | `true` | Start TLS before SMTP login and send |
| `ESCALATION_EMAIL` | task owner | Optional escalation recipient |
| `ESCALATION_AFTER_DAYS` | `7` | Days after the initial reminder before escalation |
| `JOB_MAX_ATTEMPTS` | `5` | Attempts before a background job moves to dead-letter status |
| `JOB_RETRY_BASE_SECONDS` | `30` | Base exponential retry delay in seconds (capped at one hour) |
| `JOB_LEASE_SECONDS` | `120` | Worker lease duration before interrupted work can be reclaimed |
| `CONTRACT_RETENTION_DAYS` | `0` (disabled) | Age threshold for eligible contract-data redaction |
| `CALENDAR_PROVIDER` | unset | Optional live event sync target: `microsoft` or `google` |
| `MS_GRAPH_*` | unset | Microsoft Graph access token or app credentials, user ID, and calendar ID |
| `GOOGLE_CALENDAR_ACCESS_TOKEN` / `GOOGLE_CALENDAR_ID` | unset | Google Calendar access token and target calendar ID |
| `GOOGLE_DRIVE_ACCESS_TOKEN` | unset | Google Drive token with read access to the selected source |
| `GOOGLE_DRIVE_FOLDER_ID` | unset | Optional Google Drive folder to poll; defaults to root |
| `MS_GRAPH_DRIVE_ID` | unset | Microsoft Graph drive identifier to poll |
| `MS_GRAPH_FOLDER_ITEM_ID` | unset | Optional Graph folder item to poll; defaults to drive root |
| `GOOGLE_DRIVE_WEBHOOK_TOKEN` | unset | Shared token expected in the Google Drive callback's `X-Goog-Channel-Token` header |
| `MS_GRAPH_WEBHOOK_CLIENT_STATE` | unset | Shared `clientState` value required on every Graph notification |
| `DOCUMENT_SOURCE_WEBHOOK_TENANT_ID` | `default` | Tenant whose configured source credentials and delta cursors are reconciled by webhook-triggered jobs |

Copy [.env.example](.env.example) to `.env` to start configuring local integrations. Do not commit real credentials.

## Repository layout

```text
contract_renewal_radar/
├── examples/                 # Sample normalized contract data
├── evaluation/
│   ├── cases/                 # Redacted fictional evaluation fixtures and expected terms
│   └── README.md              # Evaluation metrics and workflow
├── src/renewal_radar/
│   ├── auth.py                # Bearer-token authentication and role scopes
│   ├── api.py                 # FastAPI routes and request handling
│   ├── calendar.py            # iCalendar export
│   ├── calendar_sync.py       # Microsoft Graph and Google Calendar upserts
│   ├── documents.py           # PDF text extraction and OCR
│   ├── extractor.py            # Rules and optional LLM extraction
│   ├── jobs.py                 # Durable background-job enqueue and worker loop
│   ├── notifications.py       # SMTP and log notification delivery
│   ├── reminders.py           # Reminder and escalation runner
│   ├── schemas.py              # Typed request, response, and evidence models
│   └── store.py                # Tenant-scoped SQLite persistence and audit events
├── .github/workflows/         # Deterministic extraction quality gate
├── Dockerfile
├── docker-compose.yml
├── LICENSE
├── pyproject.toml
└── README.md
```

## Design notes and limitations

- The deterministic fallback uses patterns, not legal-language understanding. It will not reliably interpret every contract or extract every clause.
- The LLM path can misread contract language. Evidence quotes are provided to help the reviewer check each proposal against the source.
- Human confirmation is a required workflow boundary, not an optional quality check.
- SQLite is intended for a single host. PostgreSQL enables shared multi-instance API/worker storage, but production deployments still need managed database operations, backups, and restore testing.
- OIDC login uses a short-lived ID-token session and does not refresh expired sessions or provision users. Configure role and tenant claims at the identity provider; users sign in again after token expiration.
- Business-day counting skips weekends and reviewer-entered holidays. Holiday calendars vary by contract and jurisdiction; reviewers must enter applicable dates before confirming a business-day clause.
- Calendar access tokens need rotation. Use a secret manager or token broker and least-privilege permissions for the target calendar.
- Tenant isolation is enforced by application queries. For regulated multi-customer deployments, review the hosting boundary and consider separate databases per organization. Webhook receiver routes currently use one configured `DOCUMENT_SOURCE_WEBHOOK_TENANT_ID`; deploy separate callback configuration or add tenant-aware subscription provisioning before using a shared receiver for multiple organizations.
- When an external LLM provider is enabled, extracted contract text is sent to that provider's API. Review its data handling, retention, and contractual terms before processing confidential agreements.
- The reminder runner sends the first notification on or after the calculated task date. Run it on a reliable schedule to avoid missed notifications.

## Contributing

Issues and pull requests are welcome. For changes, describe the user problem, the behavior being changed, and any configuration or migration impact. Keep extraction proposals reviewable and preserve deterministic validation before contract activation.

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for details.

## README references

This README uses the clear project summary, quick-start, and usage progression common in widely used GitHub projects such as [FastAPI](https://github.com/fastapi/fastapi), alongside GitHub's README guidance on communicating project setup and expectations. The wording and project documentation here are original to Contract Renewal Radar.
