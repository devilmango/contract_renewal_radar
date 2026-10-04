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
- **Renewal tasks:** Calculate the action date as `expiration_date - renewal_notice_days` and associate the task with its owner.
- **Reminder and escalation runner:** Send one reminder when the task is due and one escalation if it remains open after the configured delay.
- **Calendar export:** Download open tasks in iCalendar format.
- **Audit trail:** Record ingestion, confirmation, rejection, task creation, notifications, escalation, and resolution.
- **Self-hosted storage:** Persist records in SQLite; run directly with Python or with Docker Compose.

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

Confirmation creates one open task due on October 3, 2027. An expiration date is required. If `auto_renew` is true, the confirmation must also include a renewal notice period. The authenticated actor is written to the audit trail. To reject an unconfirmed proposal, call `POST /contracts/CONTRACT_ID/reject` with the same bearer header.

### 4. Send due reminders

Schedule the runner daily with cron, a systemd timer, or your job scheduler:

```bash
renewal-radar run-reminders
```

The runner only processes confirmed contracts. It is safe to run repeatedly: it records each sent reminder and escalation to avoid resending them. With SMTP unset, it writes a notification to standard output and records the event in the audit trail.

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

The service also exposes `POST /reminders/run` for a scheduler that triggers the run through HTTP. Use a token with the `scheduler` or `admin` role.

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

## Authentication and roles

The health endpoint and generated API documentation remain public. Contract, task, calendar, audit, and reminder operations require an `Authorization: Bearer <token>` header. Unknown tokens receive `401`; a valid token without the required role receives `403`. If no users are configured, protected endpoints fail closed with `503`.

Generate a high-entropy bearer token and a corresponding SHA-256 configuration entry:

```bash
renewal-radar create-token --actor contract-admin --role admin --email admin@example.com
```

The command prints the bearer token once and a JSON entry to add to `RADAR_AUTH_USERS_JSON`. Store the bearer token with the client or secret manager; configure only its digest in the service environment. Add multiple JSON entries to the array to provision multiple users. Example shape (replace the digest with the generated value):

```dotenv
RADAR_AUTH_USERS_JSON='[{"actor":"contract-admin","token_sha256":"<64-character-sha256>","roles":["admin"],"email":"admin@example.com"}]'
```

| Role | Permissions |
| --- | --- |
| `admin` | All API operations |
| `reviewer` | Upload and review contracts, view contract and audit records, and manage all tasks |
| `owner` | View and resolve tasks assigned to the configured email address; export only those tasks to calendar |
| `scheduler` | Trigger the reminder and escalation runner |

The authenticated actor name, rather than a caller-supplied `X-Actor` or request-body field, is recorded in the audit history. Tokens are compared by digest and are not stored in SQLite. Rotate a token by generating a replacement, removing the old digest from `RADAR_AUTH_USERS_JSON`, and reloading the service.

The built-in role configuration is intentionally small. An `owner` entry must include the same email address assigned to that owner's tasks. Deploy behind TLS, protect environment variables, and use short-lived upstream credentials or an OIDC-aware gateway if your organization requires SSO, centralized revocation, or fine-grained identity lifecycle management.

## Extraction evaluation

The `evaluation/cases/` directory contains fictional, redacted contract excerpts and expected fields for six cases. The evaluation CLI reports exact-match accuracy for start date, expiration date, renewal notice days, auto-renewal status, and termination notice, plus whether extracted evidence quotes occur in the source excerpt.

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

`all` always includes the rules baseline, then includes configured LLM providers in provider-selection order. Each provider receives the same fixture texts. LLM evaluations make external API requests and may incur provider charges. This small fixture set is a regression signal, not a statistically representative measure of legal extraction quality; review the cases and add organization-approved, de-identified examples before using scores to select a provider.

See [evaluation/README.md](evaluation/README.md) for scoring details and fixture conventions.

## API reference

| Method | Endpoint | Permission |
| --- | --- | --- |
| `GET` | `/health` | Public health check |
| `POST` | `/contracts` | `reviewer` or `admin`: upload and extract a PDF |
| `GET` | `/contracts?status=pending_review\|active\|rejected` | `reviewer` or `admin`: list contracts |
| `GET` | `/contracts/{contract_id}` | `reviewer` or `admin`: view proposal and confirmed terms |
| `POST` | `/contracts/{contract_id}/confirm` | `reviewer` or `admin`: confirm terms and create a task |
| `POST` | `/contracts/{contract_id}/reject` | `reviewer` or `admin`: reject a pending proposal |
| `GET` | `/contracts/{contract_id}/audit` | `reviewer` or `admin`: read contract event history |
| `GET` | `/tasks?status=open\|resolved` | `reviewer` or `admin`: list all tasks; `owner`: list assigned tasks |
| `POST` | `/tasks/{task_id}/resolve` | `reviewer` or `admin`: resolve any task; `owner`: resolve assigned tasks |
| `POST` | `/reminders/run` | `scheduler` or `admin`: send due reminders and escalations |
| `GET` | `/calendar.ics` | `reviewer` or `admin`: export all tasks; `owner`: export assigned tasks |

OpenAPI and interactive request examples are available at `/docs` while the service is running.

## Configuration reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | `renewal_radar.db` | SQLite database location |
| `RADAR_AUTH_USERS_JSON` | `[]` | Array of actor, token SHA-256 digest, role list, and optional email records |
| `LLM_PROVIDER` | `auto` | Provider selection: `auto`, `rules`, `openai`, `anthropic`, `google`, `mistral`, `cohere`, or `xai` |
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
│   ├── documents.py           # PDF text extraction and OCR
│   ├── extractor.py            # Rules and optional LLM extraction
│   ├── notifications.py       # SMTP and log notification delivery
│   ├── reminders.py           # Reminder and escalation runner
│   ├── schemas.py              # Typed request, response, and evidence models
│   └── store.py                # SQLite persistence and audit events
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
- SQLite is intended for a single service instance. Use a managed database and durable job queue before scaling horizontally.
- Built-in bearer-token roles protect API operations, but the MVP does not implement SSO/OIDC, multi-tenant isolation, centralized credential lifecycle management, document retention policies, or legal advice. Put it behind TLS and follow your organization's contract-data policies before exposing it to a wider network.
- When an external LLM provider is enabled, extracted contract text is sent to that provider's API. Review its data handling, retention, and contractual terms before processing confidential agreements.
- The reminder runner sends the first notification on or after the calculated task date. Run it on a reliable schedule to avoid missed notifications.

## Contributing

Issues and pull requests are welcome. For changes, describe the user problem, the behavior being changed, and any configuration or migration impact. Keep extraction proposals reviewable and preserve deterministic validation before contract activation.

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for details.

## README references

This README uses the clear project summary, quick-start, and usage progression common in widely used GitHub projects such as [FastAPI](https://github.com/fastapi/fastapi), alongside GitHub's README guidance on communicating project setup and expectations. The wording and project documentation here are original to Contract Renewal Radar.
