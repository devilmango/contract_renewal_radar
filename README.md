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
- **Two extraction paths:** Use an optional LLM for contract language or a small, transparent rules extractor when no LLM is configured.
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
- Optional for LLM extraction: an OpenAI API key and the Python `llm` extra
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

### Run with Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

Compose stores the database in a named volume. Configure integrations in `.env` before starting. The default container includes PDF text extraction; add OCR support to the Docker image if your deployment needs scanned PDFs.

## Configure extraction

### Rules-based extraction (default)

If `OPENAI_API_KEY` is unset, Radar uses its built-in rules extractor. This is useful for local evaluation and has no external model dependency. It recognizes common date labels and renewal language; contract wording varies, so the extractor may leave a value blank. Reviewers can supply or correct missing values during confirmation.

### Optional LLM extraction

```bash
pip install -e '.[llm]'
export OPENAI_API_KEY='your-api-key'
export OPENAI_MODEL='gpt-4o-mini'
renewal-radar serve
```

The configured model returns JSON constrained by a schema. Radar parses that result into its Pydantic model and applies deterministic validation. Do not treat model output as legal advice or an authoritative interpretation of a contract.

### Optional OCR

```bash
pip install -e '.[ocr]'
# Install the Tesseract executable with your operating system's package manager.
```

OCR is attempted for PDF pages without selectable text. The OCR extra installs Python packages; it does not install the Tesseract system executable.

## Contract workflow

### 1. Upload a PDF

```bash
curl -X POST http://127.0.0.1:8000/contracts \
  -H 'X-Actor: procurement-import' \
  -F 'file=@vendor-agreement.pdf'
```

The service returns the proposal, its evidence, the extraction provider, a contract ID, and `pending_review` status. Non-PDF filenames are rejected. Uploads are limited to 25 MB.

### 2. Inspect the proposal

```bash
curl 'http://127.0.0.1:8000/contracts?status=pending_review'
curl http://127.0.0.1:8000/contracts/CONTRACT_ID
```

The extraction contains only terms identified in the document. Unknown values remain `null`; the reviewer is responsible for checking the PDF and supplying missing or corrected values.

### 3. Confirm terms and assign an owner

```bash
curl -X POST http://127.0.0.1:8000/contracts/CONTRACT_ID/confirm \
  -H 'Content-Type: application/json' \
  -d '{
    "actor": "jane.legal",
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

Confirmation creates one open task due on October 3, 2027. An expiration date is required. If `auto_renew` is true, the confirmation must also include a renewal notice period. To reject an unconfirmed proposal, call `POST /contracts/CONTRACT_ID/reject` with an optional `X-Actor` header.

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

The service also exposes `POST /reminders/run` for a scheduler that triggers the run through HTTP. Protect this endpoint with the rest of the API.

### 5. Resolve work, export dates, and review history

```bash
curl http://127.0.0.1:8000/tasks

curl -X POST http://127.0.0.1:8000/tasks/TASK_ID/resolve \
  -H 'Content-Type: application/json' \
  -d '{"actor":"jane.legal","comment":"Renewed for one year."}'

curl -o renewals.ics http://127.0.0.1:8000/calendar.ics
curl http://127.0.0.1:8000/contracts/CONTRACT_ID/audit
```

## API reference

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/health` | Service health |
| `POST` | `/contracts` | Upload and extract a PDF |
| `GET` | `/contracts?status=pending_review\|active\|rejected` | List contracts, optionally filtered by status |
| `GET` | `/contracts/{contract_id}` | View proposal and confirmed terms |
| `POST` | `/contracts/{contract_id}/confirm` | Confirm terms, assign an owner, and create a task |
| `POST` | `/contracts/{contract_id}/reject` | Reject a pending proposal |
| `GET` | `/contracts/{contract_id}/audit` | Read contract event history |
| `GET` | `/tasks?status=open\|resolved` | List tasks, optionally filtered by status |
| `POST` | `/tasks/{task_id}/resolve` | Resolve a task and add an audit event |
| `POST` | `/reminders/run` | Send due reminders and escalations |
| `GET` | `/calendar.ics` | Download open tasks as iCalendar |

OpenAPI and interactive request examples are available at `/docs` while the service is running.

## Configuration reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | `renewal_radar.db` | SQLite database location |
| `OPENAI_API_KEY` | unset | Enables the optional OpenAI extractor |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model used by the optional extractor |
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
├── src/renewal_radar/
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
- This MVP does not implement user authentication, authorization, multi-tenant isolation, document retention policies, or legal advice. Put it behind an authenticated reverse proxy and follow your organization's contract-data policies before exposing it to a wider network.
- The reminder runner sends the first notification on or after the calculated task date. Run it on a reliable schedule to avoid missed notifications.

## Contributing

Issues and pull requests are welcome. For changes, describe the user problem, the behavior being changed, and any configuration or migration impact. Keep extraction proposals reviewable and preserve deterministic validation before contract activation.

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for details.

## README references

This README uses the clear project summary, quick-start, and usage progression common in widely used GitHub projects such as [FastAPI](https://github.com/fastapi/fastapi), alongside GitHub's README guidance on communicating project setup and expectations. The wording and project documentation here are original to Contract Renewal Radar.
