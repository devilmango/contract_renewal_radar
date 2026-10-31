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
- **Amendment handling:** Preview changes to terms and obligations before confirmation, supersede an active agreement, close its outstanding tasks, and expose version lineage with deterministic differences.
- **Reminder and escalation runner:** Send scheduled reminders before the notice deadline and tiered follow-ups when the task remains open.
- **Configurable escalation policies:** Set tenant-specific reminder offsets, a backup owner, and multiple named escalation tiers with independent recipients.
- **Calendar export:** Download open tasks in iCalendar format.
- **Live calendar sync:** Upsert renewal events in Microsoft 365 or Google Calendar, automatically enqueue updates when contracts are confirmed or tasks close, and remove events for resolved tasks.
- **Email integrations:** Deliver reminders through SMTP, Microsoft Graph, or Gmail API with tracked attempts and bounded background retries.
- **Cloud document intake:** Poll Google Drive and Microsoft Graph delta feeds or receive push notifications that queue delta reconciliation, with revision deduplication and source links.
- **Renewal workflow:** Move tasks through review, change requests, approval, notice work, renewal, termination, and cancellation with permission checks and audit history.
- **Notice preparation and delivery evidence:** Draft notice content, require reviewer approval, record dispatch details, and attach delivery confirmation evidence.
- **Durable background jobs:** Persist reminder, calendar-sync, document-reconciliation, and retention work with deduplication keys, worker leases, bounded retries, and dead-letter recovery.
- **Reviewer task inbox:** Filter upcoming work by workflow stage, deadline, and assignment; assign owners, update workflow, comment, resolve, and inspect contract history and notice drafts.
- **Operations dashboard:** Inspect queue health, retries, dead letters, stuck work, integration checks, calendar-sync jobs, and email/log notification attempts with actionable retry controls and alerts.
- **Renewal risk dashboard:** Triage overdue and near-deadline work, unassigned tasks, upcoming expirations, and aging contract reviews in a tenant-scoped portfolio view.
- **Reviewer feedback evaluation:** Capture proposal-to-confirmed corrections, approve de-identified evidence-backed cases, and export them for repeatable provider comparison.
- **Obligation proof and sign-off:** Attach checksummed completion evidence, require reviewer approval before recurring work advances, and retain failed, missed, submitted, and reviewed outcomes in compliance history.
- **Approval inbox with SLAs:** Review pending obligation evidence and playbook decisions in one age-ranked queue. Scheduled reminder runs notify assigned reviewers, escalate work after the approval SLA, and retain delivery attempts and outcomes in an audit history.
- **Renewal playbooks:** Route tasks to named approvers using contract-value, supplier-risk, and amendment-clause conditions; edit, version, simulate against open renewals, and retire rules while retaining the applied version snapshot for past decisions.
- **Automatic amendment pairing:** Rank pending uploads against active contracts, preselect strong name matches for reviewer confirmation, compare quoted source clauses and structured terms, and infer amendment categories for playbook routing.
- **Portfolio operations:** Bulk-assign task owners, move eligible tasks into review, associate contracts with suppliers, and export tenant-scoped portfolios as CSV or Excel workbooks.
- **Provider drift monitoring:** Compare rules and configured LLM providers on the same redacted fixtures, including field accuracy, evidence support, amendment clause coverage, and provider-extracted amendment term changes; surface score drops against each provider's prior run.
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

Optional email configuration (SMTP remains the default):

```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=renewal-radar
SMTP_PASSWORD=replace-me
SMTP_FROM=contracts@example.com
SMTP_STARTTLS=true
EMAIL_PROVIDER=smtp
ESCALATION_EMAIL=legal-lead@example.com
ESCALATION_AFTER_DAYS=7
```

`EMAIL_PROVIDER` accepts `smtp`, `microsoft`, or `gmail`. Microsoft Graph email reuses `MS_GRAPH_TENANT_ID`, `MS_GRAPH_CLIENT_ID`, `MS_GRAPH_CLIENT_SECRET`, and `MS_GRAPH_USER_ID`; grant the app the Graph `Mail.Send` application permission with tenant admin consent. Gmail uses `GMAIL_ACCESS_TOKEN` from an OAuth token broker with the `gmail.send` scope. Set `SMTP_FROM` to a sender address verified for the selected provider. Microsoft and Gmail provider errors are recorded as failed notification attempts; the token itself is never written to the job or audit record. Keep provider credentials in a secret manager.

`ESCALATION_EMAIL` receives an unresolved-task escalation when configured; otherwise the task owner receives it. Without the selected provider's credentials, notifications are recorded through the local log channel.

Approval decisions share the scheduled reminder runner. Set `APPROVAL_SLA_DAYS` (default `3`) to define when a pending reviewer item is overdue, `APPROVAL_REVIEWER_EMAILS` to route obligation evidence sign-offs (comma-separated), and `APPROVAL_ESCALATION_EMAIL` to receive overdue approval escalations. Playbook reminders go to approvers who have not yet decided. Run `renewal-radar run-reminders` daily or schedule the same durable reminder job used for renewal notices. The `/approvals` workspace shows current age, SLA status, and recent delivery attempts.

### Tenant escalation policies

Reviewers can inspect the active policy with `GET /escalation-policy`; only administrators can update it with `PUT /escalation-policy`. Policies are tenant-scoped and stored in the configured database. `reminder_days_before` schedules reminders relative to the task's notice deadline, `backup_owner_email` receives reminders for unassigned tasks, and `escalation_steps` sends named follow-ups after the deadline to the configured escalation recipient. Each reminder offset and escalation day is persisted as a completed action so daily job retries do not resend a completed step. If a worker misses a scheduled day, all eligible unsent actions are caught up on its next run.

The same policy controls `reminder_recipients`, `escalation_recipients`, `approval_recipients`, and `approval_escalation_recipients`, plus a channel list for each event (`email`, `slack`, `teams`, or `log`). Empty task recipient lists retain owner/backup-owner routing, and empty approval lists retain assigned reviewer routing. `APPROVAL_REVIEWER_EMAILS`, escalation-step recipients, and the existing approval escalation environment setting remain defaults when no tenant override is set. Each selected channel/recipient route has an idempotency key; delivery success and failure are recorded in the operations dashboard and contract audit trail.

```json
{
  "reminder_days_before": [90, 30, 7, 0],
  "backup_owner_email": "contracts-ops@example.com",
  "escalation_steps": [
    {"after_due_days": 0, "recipient_email": "team-lead@example.com", "label": "Team lead"},
    {"after_due_days": 7, "recipient_email": "legal@example.com", "label": "Legal escalation"}
  ]
}
```

The default policy sends one reminder on the notice deadline and escalates after `ESCALATION_AFTER_DAYS`, preserving the original behavior. Configured tiers are evaluated by each task's local notice date and use calendar-day offsets.

Configure Slack and Teams incoming webhooks from `/integrations` or `PUT /integrations/connections/slack|teams`. Slack hooks must use `hooks.slack.com`; Teams hooks must use Microsoft `webhook.office.com`, `webhook.office365.com`, or `logic.azure.com` HTTPS endpoints. URLs are encrypted with the tenant integration key and never returned by the connection listing. The settings page's Send test action sends a small test card/message. Production notifications include only the task title and due date. Delivery failures are visible in `/operations` and are written to the audit trail. Configure channel preferences only after the matching webhook connection is saved.

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
| `reviewer` | Upload and review contracts, approve obligation evidence, manage playbooks and portfolio exports, view contract/access history, manage all tasks, and place or release legal holds |
| `owner` | View assigned tasks, upload obligation proof, record compliance exceptions, request workflow changes, comment, prepare notice drafts, and resolve assigned renewal tasks |
| `scheduler` | Trigger the reminder/escalation runner and poll configured document sources |

The authenticated actor name, rather than a caller-supplied `X-Actor` or request-body field, is recorded in the audit history. Tokens are compared by digest and are not stored in SQLite. Rotate a token by generating a replacement, removing the old digest from `RADAR_AUTH_USERS_JSON`, and reloading the service.

### Renewal risk dashboard

Open `/renewal-risk` or request `GET /renewal-risk/summary`. The dashboard is tenant-scoped and shows pending contract reviews, overdue tasks, unassigned open tasks, notice deadlines within 30 days, expirations within 90 days, and high-risk task counts. Task risk scores are deterministic: overdue status, deadlines within 7/30 days, missing owners, selected blocked workflow states, and expirations within 90 days add risk points. Pending-review contracts are listed separately and marked high risk after seven days. Pass `as_of=YYYY-MM-DD` to the summary endpoint to produce a reproducible snapshot.

Spend exposure uses only reviewer-confirmed `annual_contract_value` and ISO-style three-letter `currency` values. The summary reports `annual_spend_at_risk` for all open renewal tasks and `annual_spend_high_risk` for tasks with a deterministic risk score of 50 or more. Amounts are grouped by currency; they are never converted or summed across currencies. Contracts without confirmed values do not affect these totals.

The built-in role configuration is intentionally small. An `owner` entry must include the same email address assigned to that owner's tasks. Each static token can set `tenant_id` (default `default`); principals only see data in their tenant. Provision a distinct ID for each organization.

For OIDC single sign-on, configure the issuer, JWKS URL, audience, authorization and token endpoints, client ID and secret, and an exact callback URI of `/auth/oidc/callback`. The login uses authorization code flow with state and nonce checks; the ID token is signature-validated and held in a short-lived, HttpOnly session cookie. Radar reads roles from `OIDC_ROLES_CLAIM` (default `roles`) and tenant from `OIDC_TENANT_CLAIM` (default `tenant_id`). The ID token must contain those claims and use `OIDC_AUDIENCE`. Configure the same callback URI in the IdP. `RADAR_COOKIE_SECURE=true` is correct behind HTTPS; set it to `false` only for local HTTP development. Deploy behind TLS and protect client credentials.

Reviewer permissions include source-text access so the reviewer can verify extraction evidence. Keep reviewer tokens scoped and assign role and tenant claims intentionally.
Reviewers and administrators also manage assignments, workflow transitions, comments, and notice approval. Reviewers can read the tenant's escalation policy; only administrators can change it. Owners can read and create notices for assigned tasks; only reviewers or administrators can approve and record dispatch/delivery evidence.

### Live calendar synchronization

Set `CALENDAR_PROVIDER=microsoft` or `google`. Microsoft accepts a short-lived `MS_GRAPH_ACCESS_TOKEN` or client credentials (`MS_GRAPH_TENANT_ID`, `MS_GRAPH_CLIENT_ID`, `MS_GRAPH_CLIENT_SECRET`), plus `MS_GRAPH_USER_ID` and `MS_GRAPH_CALENDAR_ID`. Google uses `GOOGLE_CALENDAR_ACCESS_TOKEN` and `GOOGLE_CALENDAR_ID`; provide a current access token through a secret manager or token broker. Limit provider permissions to the intended calendar.

Call `POST /calendar/sync` after configuration to enqueue a sync job. Contract confirmation, amendment confirmation, and task resolution automatically enqueue a sync when `CALENDAR_PROVIDER` is configured. The worker creates or updates open task events and removes events when tasks are resolved or superseded. Event IDs are retained in the configured database and synchronization actions are recorded in the contract audit trail. ICS export remains available for clients that do not need live updates.

#### Tenant-owned calendar and email connections

Administrators can manage organization-specific provider settings through `GET /integrations/connections`, `PUT /integrations/connections/{calendar|email|slack|teams}`, `POST /integrations/connections/{calendar|email|slack|teams}/check`, and `DELETE /integrations/connections/{calendar|email|slack|teams}`. Reviewer accounts can read connection status; only administrators can save, check, or remove a connection. Supported providers are Google Calendar, Microsoft Calendar, SMTP, Microsoft Graph email, Gmail, Slack incoming webhooks, and Microsoft Teams webhooks. OAuth refresh tokens are used to request fresh short-lived access tokens when available. Saving a connection replaces its credential bundle; credentials are encrypted with AES-GCM and never returned by the API. Connection changes and removals are recorded in the integration audit table.

Administrators can also use the `/integrations` setup page to configure calendar, email, Slack, and Teams providers, view saved connection health, run provider checks (webhook checks send a test message), and remove connections. Secret fields are cleared after save and never populated from stored credentials. Updating a connection replaces the full credential bundle; keep the original credential set available when rotating a provider setup.

Set `INTEGRATION_ENCRYPTION_KEY` to a stable, deployment-managed 32-byte key (for example, generate one with `openssl rand -base64 32`) before saving connections. Back it up securely and keep it stable: losing or rotating the key without re-encrypting stored credentials makes those connections unreadable. Tenant credentials are used for that tenant's calendar sync and reminder email; legacy process-wide integration variables remain supported for tenants without a saved connection. The connection check performs a read-only provider request (or SMTP handshake/login) and records health without sending a message or calendar event.

### Amendments and contract versions

The reviewer workspace suggests active tenant contracts that resemble a pending upload's extracted contract name. Suggestions are ranked by normalized-name similarity and are only hints; a reviewer must select the intended prior contract. `GET /contracts/{contract_id}/amendment-candidates` returns the tenant-scoped suggestions and match reasons.

Before confirmation, use `POST /contracts/{contract_id}/amendment-preview` with the proposed `ConfirmationRequest`. The preview validates renewal terms and shows a field-by-field diff, open tasks and obligations that will close with the prior version, extracted obligation proposals that need review and scheduling on the replacement, and the replacement task's owner and notice deadline. It also compares source text clause-by-clause and displays exact added and removed quotes with risk categories for reviewer attention, including text changes that have no corresponding structured field change. The comparison is deterministic and read-only; it does not decide whether an amendment is legally acceptable. The review workspace requires a fresh preview of the current terms before enabling confirmation. Confirmation revalidates the selected `supersedes_contract_id`, atomically links the new version, marks the earlier contract superseded, resolves its open tasks, creates the new task, and records the relationship in the audit trail. `GET /contracts/{contract_id}/versions` returns the complete tenant-scoped lineage from the earliest known version through the latest, including deterministic changes to dates, renewal terms, and notice instructions. The endpoint works when requested with any version in the chain. Imported document revisions remain proposals until a reviewer explicitly confirms them as an amendment.

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

Open `/operations` or request `GET /operations/health` with a reviewer, scheduler, or administrator token. The dashboard summarizes queued/running/retrying/dead jobs, overdue queue items, expired leases, integration connection checks, calendar-sync job results, and email, Slack, Teams, or log notification attempts including their durations, recipients, and failures. It raises alerts for dead letters, stuck jobs, expired leases, failed integration checks, and three or more notification failures in the past hour. An administrator can retry a dead job from the dashboard or through `POST /jobs/{job_id}/retry`; a running worker must process the requeued job. Set the `stuck_after_minutes` query parameter to adjust the queue-wait threshold (1–1440 minutes). Notification history stores task IDs and delivery diagnostics, not message bodies or provider credentials. Delivery attempts are recorded before the reminder workflow marks the task notified; email and webhook delivery can be repeated if a process stops after successful send but before its state update.

### Renewal workflow and notice tracking

Confirmed renewal tasks start in `review`. Reviewers can assign an owner and transition tasks through `needs_changes`, `pending_approval`, and `notice_in_progress`, then record an outcome as `renewed`, `terminated`, or `cancelled`. Owners can request changes and submit notice work for approval. Every transition, assignment, and comment is written to the audit trail.

Reviewers can record a structured final outcome from the task inbox or `POST /tasks/{task_id}/outcome`. Supported outcomes are `renewed`, `renegotiated`, `terminated`, `expired`, and `no_action`. A renewal or renegotiation requires its actual expiration date; optional actual annual value and annual savings require a three-letter currency code. The record can also link a successor contract and retain decision notes. Recording an outcome closes the task and writes the outcome to the contract audit trail. `GET /renewal-outcomes/summary` reports counts and actual values/savings grouped by currency; values in different currencies are never combined.

Supplier master records are tenant-scoped and canonicalized by case-folding and collapsing whitespace. Reviewers can create suppliers and associate contracts through the supplier workspace or API. The directory reports contract counts, active contracts, open renewals, and annual spend at risk by currency. Assigning a supplier is auditable, and one supplier can group multiple contracts. Use `/suppliers/workspace` for the directory and contract assignment controls.

### Contract obligations beyond renewal

The extraction schema and LLM prompt support dated obligations for price increases, insurance certificates, audit rights, and termination windows. The rules extractor proposes only obligations whose category and date occur in the same sentence; it does not invent a date or owner. Each proposal includes a source quote and can use either an explicit date or an explicit offset from the contract start or expiration. Explicit annual, quarterly, monthly, or every-N-month schedules are captured as recurrence intervals. Reviewers can inspect proposals in `/obligations/workspace`, assign an owner, verify the schedule and evidence, then create a separate reminder task. Owners attach completion proof through `/compliance/workspace` or `POST /obligations/{obligation_id}/evidence`; accepted files are PDF, PNG, JPEG, plain text, or binary, up to 5 MB. The file content and SHA-256 checksum are persisted in tenant-scoped storage. A new submission enters `pending_signoff`; only reviewer approval resolves the occurrence and creates the next recurring task. Rejected evidence stays in the compliance timeline and can be replaced with a new submission. The direct task-resolution endpoint rejects obligation occurrences without reviewer-approved evidence. Owners and reviewers can record `failed` outcomes at any time and mark an occurrence `missed` only after the due date. `GET /obligations/{obligation_id}/compliance` returns the immutable outcome timeline and proof metadata, and `/obligation-evidence/{evidence_id}/download` streams the original file only to an authorized tenant user. Reviewers can pause or resume a schedule, end it permanently, change the cadence, or reschedule an open occurrence; all changes are tenant-scoped and audited. A paused or ended schedule does not create the next occurrence when its current task is signed off. `PATCH /obligations/{obligation_id}/schedule` updates schedule status and/or cadence; `POST /obligations/{obligation_id}/reschedule` moves an open occurrence within the contract term. Obligations and attached proof are deleted when their contract is redacted; open reminder tasks are resolved when an amendment supersedes the contract.

### Renewal approval playbooks

Open `/portfolio/workspace` or call `POST /renewal-playbooks` to define a named approval playbook. Optional conditions include a minimum annual value and currency, supplier-risk levels, and changed amendment-clause categories. Every configured condition must match when the playbook is applied to an open renewal task. The request includes all clause categories identified during amendment review, such as `renewal`, `termination`, `pricing`, `security`, and `liability`. A match creates a task review, changes the task to `pending_approval`, and snapshots the required approvers and checklist. Every required approver must record a decision using their authenticated email, and every checklist entry must be complete before the review moves from `blocked` to `approved`; a rejection is final. Application, approvals, and checklist edits are written to the contract audit history. Playbook application is a workflow gate and does not itself send a notice or modify contract terms.

Example:

```json
{
  "name": "High-value cloud renewals",
  "min_contract_value": "100000.00",
  "currency": "USD",
  "supplier_risk_levels": ["high", "critical"],
  "changed_clause_categories": ["renewal", "pricing", "liability"],
  "required_approvers": ["legal@example.com", "finance@example.com"],
  "checklist": ["Review amendment differences", "Confirm notice instructions"]
}
```

Use `POST /tasks/{task_id}/playbooks/{playbook_id}` with the observed `changed_clause_categories`, then retrieve the review with `GET /tasks/{task_id}/playbook-review`. Required approvers record decisions at `POST /tasks/{task_id}/playbook-review/approval`; reviewers update checklist items at `PATCH /tasks/{task_id}/playbook-review/checklist`. Configure supplier risk with `POST /suppliers` or `PATCH /suppliers/{supplier_id}/risk`.

### Portfolio bulk actions and exports

Open `/portfolio/workspace` for bulk task and obligation actions, saved filters, export previews, and downloads. `POST /portfolio/bulk` accepts up to 100 unique IDs per request and applies an action atomically within the tenant: `assign_tasks` or `assign_obligations` updates open work owners, `transition_tasks` moves compatible open tasks into a selected review state, `assign_supplier` associates contracts with a tenant-owned supplier, and `review_obligations` approves or rejects pending evidence sign-offs. Obligation review IDs are evidence IDs; approving a batch resolves those occurrences and creates the next scheduled occurrence when applicable. The operation is rejected in full if any selected record is missing, closed, incompatible, already reviewed, or belongs to another tenant. Each changed obligation or contract receives an audit event.

Saved filters are named and tenant-scoped. Use `POST /portfolio/filters` with a `name`, `dataset`, and supported exact-match `filters`; list them at `GET /portfolio/filters`, preview their matching rows at `GET /portfolio/filters/{filter_id}/preview`, and remove one with `DELETE /portfolio/filters/{filter_id}`. `GET /portfolio/export/preview?dataset=tasks&saved_filter_id=...` shows the row count, columns, and at most ten sample rows before download. Downloads can apply the same saved filter with `GET /portfolio/export?dataset=...&format=csv|xlsx&saved_filter_id=...`. Exports exclude stored contract source text and evidence file contents and neutralize spreadsheet formula prefixes. Reviewer/admin access is required; owner-scoped exports are unavailable.

The same workspace supports CSV and XLSX migration imports for contracts, suppliers, and obligations. Upload a file to `POST /portfolio/import/preview` with `dataset` and an optional JSON `field_mapping` from canonical field names to source headers. The response lists normalized previews, validation errors, rows duplicated within the file or portfolio, and summary counts; preview does not write portfolio records. Commit only after reviewing the report with `POST /portfolio/import/{batch_id}/commit`. Valid rows are rechecked for duplicates at commit time, and each attempted row plus the final batch summary is retained in an auditable report available through `GET /portfolio/imports` and `GET /portfolio/import/{batch_id}`. Contract rows are confirmed as reviewer-submitted portfolio records; obligation rows must reference an active contract in the same tenant and include an evidence quote and owner email. Imports accept UTF-8 CSV or the first worksheet of XLSX, with a 10 MB upload, 25 MB expanded-workbook, and 5,000-row limit.

### Renewal decision workspace

Open `/renewal-decisions/workspace` to track a renewal from strategy through recorded outcome. A reviewer creates a decision for an open renewal task and selects `negotiate`, `renew`, or `terminate`, records the rationale, and can link a saved forecast run. Add dated milestones with owners and supplier quotes, then submit the decision for reviewer sign-off. Approved decisions can be completed with the actual outcome, annual value/savings, currency, and actual expiration date; completion records the outcome against the task, closes the decision, and adds both to the audit history. A linked forecast is used when scoring forecast accuracy, even if another forecast was run later. Decisions created through the task outcome endpoint must be approved first and the outcome must agree with the selected strategy.

Decision states are `draft`, `in_progress`, `pending_approval`, `approved`, `rejected`, and `completed`. Review decisions and quote/milestone changes are tenant-scoped and retained as events. The workspace tracks decisions and approvals; it does not send supplier communications or alter contract terms.

### Provider evaluation and drift monitoring

Use `/portfolio/workspace` or call `POST /evaluations/runs` to evaluate the built-in redacted fixture set with the local rules baseline and each configured, allowlisted provider. Provider credentials must already be configured; LLM runs make external API requests and may incur charges. Each run stores provider/model, fixture-set fingerprint, field exact-match accuracy, evidence-support accuracy, amendment-clause evidence accuracy, and amendment comparison accuracy. Amendment fixtures include paired active/amendment text and reviewed expected changed fields. Clause evidence scoring checks whether provider quotes cover expected term, expiration, renewal, auto-renewal, notice, and termination categories. `GET /evaluations/drift` compares a run with the same provider's previous run on the identical fixture fingerprint and reports drops of at least 0.10 (10 percentage points).

Configure automatic runs with `PUT /evaluations/schedule`, for example `{"enabled":true,"interval_hours":24,"run_on_provider_change":true,"alert_emails":["legal@example.com"]}`. `process-jobs --loop` checks due schedules and enqueues durable evaluation jobs; `run-reminders` also checks the selected tenant's schedule, so deployments that already invoke the daily reminder runner need no separate scheduler. Provider/model configuration changes trigger a run on the next scheduler check. Run state is tenant-scoped, and failed jobs use the worker retry policy. Drift alerts are delivered through the tenant's configured email integration or the environment email channel, and every attempt (including failures) is retained in `GET /evaluations/drift` under `deliveries`. `POST /evaluations/schedule/run` queues an evaluation only when due. These fixtures are regression signals rather than legal-quality certification.

### Renewal planning and forecasting

Open `/renewal-forecast` or call `POST /renewal-forecast` with `start_date`, `end_date`, `renewal_increase_pct`, and `renegotiation_savings_pct`. The deterministic report compares renew, renegotiate, and terminate scenarios across confirmed active contracts. It prorates annual values by calendar days, groups money by currency, and includes month-by-month projected costs, renewal notice deadlines, and expirations. Renewal growth defaults to 0%; renegotiation savings defaults to 10%. Termination includes spend through the current expiration date and no spend afterward. Contracts without confirmed annual value are listed as missing from monetary totals. Reviewer-generated forecasts are snapshotted with their assumptions. After outcomes are recorded, `GET /renewal-forecast/accuracy` compares predicted and actual annual values by supplier and scenario, reports signed and absolute variance, and recommends median renewal increase and negotiation savings assumptions from comparable historical outcomes. Reviewers can apply those assumptions to the planning form and generate a new forecast. Accuracy uses an explicitly linked decision forecast when present; otherwise it uses the latest forecast created before the outcome. The report is a planning aid; scenario assumptions do not change contract terms, task status, or provider calendars. The maximum planning period is five years.

Open `/renewal-scenarios` or use the reviewer API. For a single renewal task, create a saved, auditable comparison with `POST /tasks/{task_id}/scenario-plans` and list prior plans with `GET /tasks/{task_id}/scenario-plans`. Supply renewal increase, renegotiation savings, termination transition cost, a 1–60 month horizon, optional `as_of_date`, optional saved `forecast_id`, and a free-text assumptions note. The deterministic estimate uses the contract's annual spend and expiration, forecast history for that contract, its current supplier risk rating, the notice deadline, and milestones on its renewal decision. The response shows the inputs, three scenario costs, risk implications, and warnings for unfinished milestones scheduled after the notice deadline. Costs are prorated by calendar days; the termination scenario stops post-expiration spend and adds the supplied transition cost. Historical outcomes and forecasts are context for a human decision and do not silently change assumptions or recommend a legal action.

### Data governance and audit export

Open `/data-governance` or use the administrator API to inspect and set tenant retention periods with `GET /data-retention/policy` and `PUT /data-retention/policy`. The request accepts `contract_days`, `evidence_days`, and `provider_evaluation_days`; `0` means retain that category indefinitely. `POST /data-retention/run` queues a durable purge job when at least one configured period is positive. Contract content is redacted only when the existing lifecycle eligibility rules are met; evidence and provider evaluation records are removed after their configured age. Active legal holds prevent contract redaction and evidence deletion for the held contract. Each policy change and purge is recorded in the governance audit trail, while contract and evidence actions are also attached to their contract audit history. Existing `CONTRACT_RETENTION_DAYS` remains a fallback for installations that have not configured a tenant policy.

Reviewers and administrators can download a tenant-scoped JSON Lines audit export with `GET /audit/export`, optionally filtering with inclusive `from_date` and `to_date` ISO dates. The export combines contract audit events, evidence compliance history, provider evaluation metadata, access events, and governance changes, including policy updates and purge actions. It deliberately excludes contract source text, evidence file bytes, and full provider evaluation reports. Export activity itself is logged. Legal holds remain independently managed with the legal-hold endpoints; a hold must be explicitly released before protected contract or evidence data can be purged.

Open `/portfolio/performance` or call `GET /portfolio/performance/summary` for portfolio results over time. The report groups recorded renewal outcomes, completed/failed/missed obligations, and approval turnaround by month; current open overdue obligations and SLA-breached sign-offs are shown as backlog measures. Use `start_date`, `end_date`, `supplier_id`, and `owner_email` to filter the results. Approval turnaround includes obligation evidence sign-offs, playbook decisions, and renewal decision reviews. Amounts remain grouped by currency.

Create a notice draft on a task with `POST /tasks/{task_id}/notices`, including a subject, human-reviewed body, recipient, and delivery method. A reviewer must approve it before dispatch can be recorded. `POST /notices/{notice_id}/dispatch` records the sent timestamp, reference, and optional note; it does not send email or submit the notice to a vendor. Record receipt evidence with `POST /notices/{notice_id}/delivery`. Legal content and actual delivery remain under human control while Radar provides a searchable audit record. List notices with `GET /tasks/{task_id}/notices`.

### Durable jobs

Reminder runs, live calendar synchronization, document-source reconciliation, and retention sweeps are stored in the configured database as jobs. Enqueue operations return a job ID; workers claim jobs using a database transaction and expiring lease, persist outcomes, and retry failures with bounded exponential backoff. PostgreSQL workers claim jobs using `FOR UPDATE SKIP LOCKED` so multiple worker processes can safely share the queue. An expired lease makes interrupted work eligible for another worker. Exhausted jobs remain visible in the dead-letter state for review and administrator retry. Run a separate worker process under a supervisor with `renewal-radar process-jobs --loop --limit 20`.

The queue is durable across process restarts and supports a separate worker process. SQLite still serializes writers and is intended for a single host. To use PostgreSQL, install `pip install -e '.[database]'` and set `DATABASE_URL` (for example, `postgresql://radar:secret@db:5432/radar`); it takes precedence over `DATABASE_PATH`. The service creates its schema idempotently and applies additive compatibility upgrades at startup. Use a managed PostgreSQL service, backups, and a tested restore procedure for production. SMTP is at-least-once around process crashes: the stable message ID helps downstream deduplication, but SMTP itself does not guarantee exactly-once delivery.

### PostgreSQL integration test service

Run the PostgreSQL storage integration test locally against an ephemeral PostgreSQL 16 container. Install the test and database extras, start the standalone test service, wait for it to become healthy, and point `TEST_POSTGRES_URL` at its published port:

```bash
pip install -e '.[test,database]'
docker compose -f docker-compose.test.yml up -d --wait postgres-test
TEST_POSTGRES_URL=postgresql://radar:radar-test-password@localhost:5433/renewal_radar_test \
  pytest -q tests/test_postgres_store.py
docker compose -f docker-compose.test.yml down -v
```

Set `POSTGRES_TEST_PORT` to use a different local port. The integration test skips when `TEST_POSTGRES_URL` is unset; CI starts the same PostgreSQL major version as a job service and runs the test with the database extra installed.

### Data governance

Set `LLM_ALLOWED_PROVIDERS` to an explicit comma-separated list such as `rules,anthropic` to control which extraction providers may receive contract text. `rules` runs locally. The `.env.example` defaults this allowlist to `rules`; if the setting is omitted, existing deployments remain compatible and all configured providers are allowed. `LLM_PROVIDER=auto` and `renewal-radar evaluate --provider all` honor the allowlist too.

Authenticated API requests are recorded in a tenant-scoped access history without storing response bodies. Reviewers and administrators can place or release legal holds; held contracts cannot be redacted. Administrators can redact a contract with `DELETE /contracts/{contract_id}`. Redaction removes source text, extracted and confirmed terms, notices, tasks, comments, external source links, and prior contract audit details while retaining a minimal redaction audit marker and access history.

Configure tenant-specific retention periods with the governance endpoints described below, then run `renewal-radar run-retention` or call `POST /data-retention/run`. The legacy `CONTRACT_RETENTION_DAYS` environment variable remains available for tenants without a saved policy. The contract-retention job targets old rejected or superseded agreements and expired active agreements whose tasks are resolved. Pending-review agreements, upcoming active agreements, and all agreements under an active hold are excluded. Retention policy is organization-specific; confirm legal and regulatory schedules before enabling automatic redaction.

## Extraction evaluation

The `evaluation/cases/` directory contains fictional, redacted contract excerpts and OCR-style text fixtures, including paired active/amendment comparisons, ambiguous extensions, explicit no-notice cases, and calendar versus business-day terms. The evaluator reports exact-match field and case accuracy, source-type breakdowns, evidence quote grounding, field-evidence support, amendment clause evidence and changed-field comparison accuracy, latency, provider-reported token usage, and an optional cost estimate using configured per-token rates.

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

Use `--min-exact-accuracy`, `--min-field-accuracy`, `--min-evidence-support`, `--min-clause-accuracy`, and `--min-amendment-comparison-accuracy` to make quality thresholds fail the command. GitHub Actions runs the rules baseline with 0.70 exact-case and field thresholds, a 0.90 evidence-support threshold, and 0.75 floors for amendment-clause evidence and changed-field comparisons for pull requests and pushes to `main` or `master`.

`all` always includes the rules baseline, then includes configured and allowlisted LLM providers in provider-selection order. Each provider receives the same fixture texts; paired amendment fixtures require an additional extraction call for the active version. LLM evaluations make external API requests and may incur provider charges. This compact fixture set is a regression signal, not a statistically representative measure of legal extraction quality; review the cases and add organization-approved, de-identified examples before using scores to select a provider. See the evaluation guide for an approval/de-identification record format. Do not commit confidential contracts or identifiers to a public repository.

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
| `GET` | `/contracts/{contract_id}/versions` | `reviewer` or `admin`: view amendment lineage and term changes |
| `GET` / `POST` | `/contracts/{contract_id}/relationships` | `reviewer`: view graph/timeline and link master agreements, SOWs, amendments, or renewals |
| `POST` | `/deadlines/calculate` | `reviewer`: calculate and explain a timezone and jurisdiction-aware notice deadline |
| `GET` | `/contracts/{contract_id}/amendment-candidates` | `reviewer` or `admin`: get tenant-scoped likely prior agreements for a pending upload |
| `POST` | `/contracts/{contract_id}/amendment-preview` | `reviewer` or `admin`: preview term changes and task effects before confirmation |
| `GET` / `PUT` | `/escalation-policy` | `reviewer`: read tenant policy; `admin`: update recipients, reminder timing, and channels |
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
| `POST` | `/tasks/{task_id}/resolve` | `reviewer` or `admin`: resolve any task; `owner`: resolve assigned renewal tasks; obligation occurrences require approved evidence sign-off |
| `POST` | `/reminders/run` | `scheduler` or `admin`: enqueue a durable reminder job (202) |
| `GET` | `/jobs` or `/jobs/{job_id}` | `reviewer`, `scheduler`, or `admin`: inspect tenant jobs |
| `GET` | `/operations/health` | `reviewer`, `scheduler`, or `admin`: inspect job, notification, and integration health with alerts |
| `GET` | `/renewal-risk/summary` | `reviewer` or `admin`: inspect tenant-scoped renewal risk metrics and backlog |
| `POST` | `/tasks/{task_id}/outcome` | `reviewer` or `admin`: record a final outcome, actual value/savings, successor, and close the task |
| `GET` | `/renewal-outcomes/summary` | `reviewer` or `admin`: summarize realized outcomes and monetary metrics by currency |
| `GET` | `/portfolio/performance/summary` | `reviewer` or `admin`: report renewal, obligation, overdue sign-off, and approval turnaround metrics; filter by date, supplier, and owner |
| `GET` / `POST` | `/suppliers` | `reviewer` or `admin`: list supplier portfolio rollups or create a canonical supplier |
| `PUT` | `/suppliers/{supplier_id}/contracts/{contract_id}` | `reviewer` or `admin`: associate a tenant contract with a supplier |
| `GET` | `/obligations?status=open\|resolved` | `reviewer`, `admin`, or assigned `owner`: list scheduled obligations and evidence |
| `POST` | `/contracts/{contract_id}/obligations` | `reviewer` or `admin`: schedule an evidence-backed obligation with an assigned owner |
| `POST` | `/renewal-forecast` | `reviewer` or `admin`: compare tenant contract spend across planning scenarios |
| `POST` / `GET` | `/tasks/{task_id}/scenario-plans` | `reviewer` or `admin` to save; `reviewer`, `admin`, or authorized reader to list auditable renewal scenario comparisons |
| `GET` | `/audit/export` | `reviewer` or `admin`: download tenant-scoped JSONL audit history, optionally filtered by date |
| `GET` | `/renewal-scenarios` | Renewal scenario planner workspace |
| `GET` | `/data-governance` | Retention policy and audit export workspace; API mutations require admin role |
| `GET` / `PUT` | `/data-retention/policy` | `admin`: inspect or configure contract, evidence, and provider-evaluation retention periods |
| `POST` | `/data-retention/run` | `admin`: queue a legal-hold-aware retention and deletion run |
| `GET` | `/renewal-forecast/accuracy` | `reviewer` or `admin`: compare saved forecast assumptions with recorded outcomes and view historical recommendations |
| `GET` | `/renewal-forecast/runs` | `reviewer` or `admin`: list saved forecasts available for decision linkage |
| `GET` / `POST` | `/renewal-decisions` | `reviewer` or `admin`: list or create a strategy decision for an open renewal task |
| `GET` / `PATCH` | `/renewal-decisions/{decision_id}` | `reviewer` or `admin`: inspect or update a draft renewal decision |
| `POST` | `/renewal-decisions/{decision_id}/milestones` | `reviewer` or `admin`: add an owned, dated decision milestone |
| `PATCH` | `/renewal-decisions/{decision_id}/milestones/{milestone_id}` | `reviewer` or `admin`: complete or reopen a milestone with a note |
| `POST` | `/renewal-decisions/{decision_id}/quotes` | `reviewer` or `admin`: attach a supplier quote to a decision |
| `POST` | `/renewal-decisions/{decision_id}/submit` | `reviewer` or `admin`: submit a draft for reviewer decision |
| `POST` | `/renewal-decisions/{decision_id}/review` | `reviewer` or `admin`: approve or reject with an optional comment |
| `POST` | `/renewal-decisions/{decision_id}/complete` | `reviewer` or `admin`: record actual outcome and complete an approved decision |
| `PATCH` | `/obligations/{obligation_id}/schedule` | `reviewer` or `admin`: pause, resume, end, or change a recurring schedule cadence |
| `POST` | `/obligations/{obligation_id}/reschedule` | `reviewer` or `admin`: reschedule an open obligation occurrence within the contract term |
| `GET` | `/integrations/connections` | `reviewer` or `admin`: list tenant provider connections and health without credentials |
| `GET` | `/integrations/procurement` | Procurement connection setup and sync history page |
| `PUT` | `/integrations/connections/{calendar\|email\|procurement}` | `admin`: create or replace a tenant provider connection |
| `POST` | `/integrations/connections/{calendar\|email\|procurement}/check` | `admin`: run a read-only connection health check |
| `DELETE` | `/integrations/connections/{calendar\|email\|procurement}` | `admin`: remove a tenant provider connection |
| `GET` | `/portfolio/performance` | Portfolio performance dashboard with date, supplier, and owner filters |
| `PUT` | `/integrations/connections/{slack\|teams}` | `admin`: store an encrypted Slack or Teams incoming webhook |
| `POST` | `/integrations/connections/{slack\|teams}/check` | `admin`: send a test notification to the saved webhook |
| `POST` | `/jobs/{job_id}/retry` | `admin`: requeue a dead job |
| `POST` | `/data-retention/run` | `admin`: enqueue a configured retention sweep (202) |
| `GET` | `/calendar.ics` | `reviewer` or `admin`: export all tasks; `owner`: export assigned tasks |
| `POST` | `/calendar/sync` | `reviewer` or `admin`: enqueue live calendar synchronization (202) |
| `POST` | `/procurement/sync` | `admin`: enqueue Coupa or SAP Ariba contract/supplier reconciliation (202) |
| `GET` | `/procurement/sync/history` | `reviewer` or `admin`: inspect sync runs, duplicates, revisions, and conflicts |
| `POST` | `/document-sources/sync` | `reviewer`, `admin`, or `scheduler`: poll configured cloud drives |
| `POST` | `/tasks/{task_id}/assign` | `reviewer` or `admin`: assign a task owner |
| `GET/POST` | `/obligations/{obligation_id}/compliance` | `reviewer`, `admin`, or assigned owner: inspect history or record failed/missed compliance |
| `POST` | `/obligations/{obligation_id}/evidence` | `reviewer`, `admin`, or assigned owner: attach proof and request reviewer sign-off |
| `POST` | `/obligation-evidence/{evidence_id}/review` | `reviewer` or `admin`: approve or reject submitted proof |
| `GET` | `/obligation-evidence/{evidence_id}/download` | `reviewer`, `admin`, or assigned owner: download tenant-scoped proof |
| `GET/POST` | `/renewal-playbooks` | `reviewer` or `admin`: list or create approval routing rules |
| `POST` | `/tasks/{task_id}/playbooks/{playbook_id}` | `reviewer` or `admin`: evaluate rules and apply a playbook |
| `GET` | `/tasks/{task_id}/playbook-review` | `reviewer`, `admin`, or assigned owner: read approval/checklist state |
| `POST` | `/tasks/{task_id}/playbook-review/approval` | Required reviewer or admin approver: record a decision |
| `PATCH` | `/tasks/{task_id}/playbook-review/checklist` | `reviewer` or `admin`: update a playbook checklist item |
| `POST` | `/portfolio/bulk` | `reviewer` or `admin`: bulk assign, transition, or associate records |
| `POST` | `/portfolio/import/preview` | `reviewer` or `admin`: validate and preview a CSV/XLSX contract, supplier, or obligation import |
| `GET` | `/portfolio/imports` and `/portfolio/import/{batch_id}` | `reviewer` or `admin`: inspect tenant-scoped import reports and audit events |
| `POST` | `/portfolio/import/{batch_id}/commit` | `reviewer` or `admin`: commit valid rows from a reviewed import preview |
| `GET/POST/DELETE` | `/portfolio/filters` | `reviewer` or `admin`: manage tenant-scoped saved portfolio filters |
| `GET` | `/portfolio/export/preview` | `reviewer` or `admin`: inspect columns, count, and sample rows before export |
| `GET` | `/portfolio/export?dataset=contracts\|tasks\|suppliers\|obligations&format=csv\|xlsx` | `reviewer` or `admin`: download tenant-scoped CSV/XLSX, optionally with a saved filter |
| `POST` | `/evaluations/runs` | `reviewer` or `admin`: evaluate the redacted fixture set with configured providers |
| `GET/PUT` | `/evaluations/schedule` | `reviewer` or `admin`: configure recurring and provider-change-triggered evaluation |
| `POST` | `/evaluations/schedule/run` | `reviewer` or `admin`: queue an evaluation when due |
| `GET` | `/evaluations/drift` | `reviewer` or `admin`: inspect provider history and score-drop alerts |
| `POST` | `/tasks/{task_id}/transition` | `reviewer` or `admin`: advance workflow; owners can request changes or submit notice work |
| `GET/POST` | `/tasks/{task_id}/comments` | `reviewer`, `admin`, or assigned `owner`: list/add comments |
| `GET/POST` | `/tasks/{task_id}/notices` | `reviewer`, `admin`, or assigned `owner`: list/prepare notice drafts |
| `POST` | `/notices/{notice_id}/approve` | `reviewer` or `admin`: approve a draft |
| `POST` | `/notices/{notice_id}/dispatch` | `reviewer` or `admin`: record dispatch of an approved notice |
| `POST` | `/notices/{notice_id}/delivery` | `reviewer` or `admin`: record delivery evidence |
| `GET` | `/review` | Public UI shell; API calls require a reviewer token |
| `GET` | `/tasks/inbox` | Public UI shell; API calls require task permissions |
| `GET` | `/operations` | Public UI shell; health API calls require `jobs:read` |
| `GET` | `/renewal-risk` | Public UI shell; summary API calls require reviewer/admin contract and task read permissions |
| `GET` | `/integrations` | Public UI shell; configuration and health APIs require administrator permissions |
| `GET` | `/suppliers/workspace` | Public UI shell; data APIs require reviewer/admin contract permissions |
| `GET` | `/obligations/workspace` | Public UI shell; data APIs require reviewer or assigned owner permissions |
| `GET` | `/renewal-forecast` | Public UI shell; forecast API requires reviewer/admin contract and task read permissions |
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
| `EMAIL_PROVIDER` | `smtp` | Email delivery adapter: `smtp`, `microsoft`, or `gmail` |
| `SMTP_PORT` | `587` | SMTP server port |
| `SMTP_USERNAME` | unset | Optional SMTP authentication username |
| `SMTP_PASSWORD` | unset | Optional SMTP authentication password |
| `SMTP_FROM` | `renewal-radar@example.com` | Sender address |
| `SMTP_STARTTLS` | `true` | Start TLS before SMTP login and send |
| `GMAIL_ACCESS_TOKEN` | unset | Gmail API OAuth access token with `gmail.send` scope |
| `ESCALATION_EMAIL` | task owner | Optional escalation recipient |
| `ESCALATION_AFTER_DAYS` | `7` | Default-policy escalation delay after the notice deadline; tenant policies can override it |
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
│   ├── deadlines.py           # Deterministic timezone and jurisdiction-aware deadline math
│   ├── documents.py           # PDF text extraction and OCR
│   ├── extractor.py            # Rules and optional LLM extraction
│   ├── jobs.py                 # Durable background-job enqueue and worker loop
│   ├── notifications.py       # SMTP and log notification delivery
│   ├── procurement_sync.py    # Coupa and SAP Ariba portfolio reconciliation
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
- Business-day counting uses the configured weekend, supported jurisdiction holiday calendar, and reviewer-entered contract-specific holidays. The persisted task deadline includes its timezone, calendar source, and calculation trace; reviewers should still verify local or sector-specific holidays.
- Calendar access tokens need rotation. Use a secret manager or token broker and least-privilege permissions for the target calendar.
- Tenant isolation is enforced by application queries. For regulated multi-customer deployments, review the hosting boundary and consider separate databases per organization. Webhook receiver routes currently use one configured `DOCUMENT_SOURCE_WEBHOOK_TENANT_ID`; deploy separate callback configuration or add tenant-aware subscription provisioning before using a shared receiver for multiple organizations.
- When an external LLM provider is enabled, extracted contract text is sent to that provider's API. Review its data handling, retention, and contractual terms before processing confidential agreements.
- The reminder runner sends the first notification on or after the calculated task date. Run it on a reliable schedule to avoid missed notifications.

### Contract relationships and effective terms

`GET /contracts/{contract_id}/relationships` returns the tenant-scoped connected contract graph, including explicit master/SOW, amendment, renewal, and related-contract links plus automatically recorded supersession links. The response includes one chronological audit timeline and effective terms with `term_sources` identifying the contract that supplied each inherited value. A SOW inherits missing values from its linked master; explicit child terms take precedence. Create a relationship with `POST /contracts/{contract_id}/relationships` and one of `master_of`, `sow_under`, `amendment_of`, `renewal_of`, or `related_to`. The call is auditable and both contracts must belong to the caller's tenant. Existing `GET /contracts/{contract_id}/versions` remains the focused linear amendment history.

### Coupa and SAP Ariba portfolio synchronization

Configure the tenant's procurement connector with an administrator token. API endpoint paths are relative to `base_url`, and responses may be a JSON array or an array under `items`, `value`, `data`, `results`, or `records`.

```bash
curl -X PUT http://127.0.0.1:8000/integrations/connections/procurement \
  -H "Authorization: Bearer $RADAR_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  --data-binary @- <<JSON
{"provider":"coupa","settings":{"base_url":"https://acme.coupahost.com/","suppliers_path":"api/suppliers","contracts_path":"api/contracts"},"credentials":{"api_key":"${COUPA_API_KEY}"}}
JSON
```

For SAP Ariba, use `provider: sap_ariba`, a tenant-approved HTTPS API base URL, and `credentials.access_token`. Coupa uses its API key header; SAP Ariba uses a bearer token. The connector imports supplier references and structured contracts as **pending review** proposals, maps common name/date/value/currency/notice fields, and associates suppliers when possible. It does not activate or supersede a contract automatically. Use `POST /integrations/connections/procurement/check` to verify read access, `POST /procurement/sync` to enqueue reconciliation, and `GET /procurement/sync/history` to inspect imports, duplicates, failed runs, and changed revisions that need reviewer attention. Runs use durable jobs with bounded retries. Configure narrow read-only credentials and endpoint scopes. The connector rejects non-HTTPS and private/local API targets and does not follow redirects.

### Jurisdiction-aware notice calculations

Reviewers can set `notice_timezone`, `notice_jurisdiction`, `notice_weekend_days` (Monday is `0`, Sunday is `6`), and contract-specific `notice_holidays` on contract confirmation. Built-in public holiday calendars are `US-FEDERAL`, `GB-ENG-WLS`, and `IN-NATIONAL`; add local or contractual dates explicitly. `POST /deadlines/calculate` returns the proposed deadline and a deterministic explanation with the counting convention, timezone, jurisdiction, weekend days, holiday source, and skipped dates. The same calculation trace is retained on the resulting renewal task and included in task API responses and audit events. The deadline is a review aid; confirm jurisdiction-specific exceptions against the contract.

## Contributing

Issues and pull requests are welcome. For changes, describe the user problem, the behavior being changed, and any configuration or migration impact. Keep extraction proposals reviewable and preserve deterministic validation before contract activation.

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for details.

## README references

This README uses the clear project summary, quick-start, and usage progression common in widely used GitHub projects such as [FastAPI](https://github.com/fastapi/fastapi), alongside GitHub's README guidance on communicating project setup and expectations. The wording and project documentation here are original to Contract Renewal Radar.
