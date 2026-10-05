from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .auth import SUPPORTED_ROLES, create_token_config
from .drive_ingestion import DocumentSourceError, sync_document_sources
from .evaluation import evaluate_extractor, load_cases
from .extractor import configured_llm_providers, get_extractor
from .jobs import enqueue_reminder_run, enqueue_retention_run, job_record, process_jobs
from .store import Store


PROVIDERS = ("auto", "rules", "openai", "anthropic", "google", "mistral", "cohere", "xai", "all")


def main() -> int:
    parser = argparse.ArgumentParser(prog="renewal-radar", description="Contract Renewal Radar")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the API service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)

    run_reminder = subparsers.add_parser("run-reminders", help="Queue and process one tenant-scoped reminder run")
    run_reminder.add_argument("--tenant-id", default="default")

    worker = subparsers.add_parser("process-jobs", help="Process due durable background jobs once")
    worker.add_argument("--limit", type=int, default=20)
    worker.add_argument("--worker-id")
    worker.add_argument("--loop", action="store_true", help="Keep polling as a long-running worker")
    worker.add_argument("--poll-interval", type=float, default=2.0, help="Idle sleep interval in seconds when --loop is enabled")

    retention = subparsers.add_parser("run-retention", help="Queue and process a configured data-retention run")
    retention.add_argument("--tenant-id", default="default")

    sync_documents = subparsers.add_parser("sync-documents", help="Poll configured Drive sources for new or changed PDFs")
    sync_documents.add_argument("--tenant-id", default="default", help="Organization whose configured Drive cursors to advance")

    create_token = subparsers.add_parser("create-token", help="Create a bearer token and hashed user configuration entry")
    create_token.add_argument("--actor", required=True, help="Identity recorded in the audit trail")
    create_token.add_argument("--role", required=True, choices=sorted(SUPPORTED_ROLES), action="append", help="Role to grant; may be repeated")
    create_token.add_argument("--email", help="Email used to scope owner tasks and receive notifications")
    create_token.add_argument("--tenant-id", default="default", help="Organization/tenant isolation key")

    evaluate = subparsers.add_parser("evaluate", help="Measure extraction accuracy on the included redacted contract set")
    evaluate.add_argument("--provider", choices=PROVIDERS, default="auto", help="Provider to evaluate, or 'all' configured providers plus rules baseline")
    evaluate.add_argument("--dataset", type=Path, default=Path("evaluation/cases"), help="Directory containing JSON evaluation cases")
    evaluate.add_argument("--output", type=Path, help="Optional path for the JSON report")
    evaluate.add_argument("--min-exact-accuracy", type=float, help="Exit non-zero when any evaluated provider is below this exact-case accuracy")
    evaluate.add_argument("--min-field-accuracy", type=float, help="Exit non-zero when any scored field is below this accuracy")
    evaluate.add_argument("--min-evidence-support", type=float, help="Exit non-zero when evidence does not support the extracted values often enough")

    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn

        uvicorn.run("renewal_radar.api:app", host=args.host, port=args.port)
        return 0

    if args.command == "run-reminders":
        store = Store()
        job = enqueue_reminder_run(store, args.tenant_id)
        worker_result = process_jobs(store, limit=1, job_id=job["id"])
        current = store.get_job(job["id"], args.tenant_id)
        print(json.dumps({"job": job_record(current) if current else job, "worker": worker_result}, indent=2))
        return 0

    if args.command == "process-jobs":
        store = Store()
        if args.loop:
            try:
                while True:
                    result = process_jobs(store, limit=max(1, args.limit), worker_id=args.worker_id)
                    if result["processed"]:
                        print(json.dumps(result), flush=True)
                    else:
                        time.sleep(max(0.1, args.poll_interval))
            except KeyboardInterrupt:
                return 0
        print(json.dumps(process_jobs(store, limit=max(0, args.limit), worker_id=args.worker_id), indent=2))
        return 0

    if args.command == "run-retention":
        retention_days = int(os.getenv("CONTRACT_RETENTION_DAYS", "0"))
        if retention_days <= 0:
            print(json.dumps({"error": "Set CONTRACT_RETENTION_DAYS to a positive number first."}), file=sys.stderr)
            return 2
        store = Store()
        job = enqueue_retention_run(store, args.tenant_id, retention_days)
        worker_result = process_jobs(store, limit=1, job_id=job["id"])
        current = store.get_job(job["id"], args.tenant_id)
        print(json.dumps({"job": job_record(current) if current else job, "worker": worker_result}, indent=2))
        return 0

    if args.command == "sync-documents":
        try:
            print(json.dumps(sync_document_sources(Store(), args.tenant_id), indent=2))
            return 0
        except DocumentSourceError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 1

    if args.command == "create-token":
        token, user_entry = create_token_config(args.actor, args.role, args.email, args.tenant_id)
        print("Bearer token (copy it now; Radar stores only its SHA-256 digest):")
        print(token)
        print("\nAdd this object to the RADAR_AUTH_USERS_JSON array:")
        print(json.dumps(user_entry, indent=2))
        return 0

    try:
        cases = load_cases(args.dataset)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2

    if args.provider == "all":
        providers = ["rules", *configured_llm_providers()]
    else:
        providers = [args.provider]

    reports = []
    failed = False
    for provider in providers:
        try:
            extractor = get_extractor(None if provider == "auto" else provider)
            report = evaluate_extractor(extractor, cases)
            report["status"] = "partial" if report["errors"] else "ok"
            failed = failed or bool(report["errors"])
            gate_failures = []
            if args.min_exact_accuracy is not None:
                score = report["exact_case_match"]["accuracy"]
                if score is None or score < args.min_exact_accuracy:
                    gate_failures.append(f"exact_case_match={score} < {args.min_exact_accuracy}")
            if args.min_field_accuracy is not None:
                below = [name for name, values in report["fields"].items()
                         if values["accuracy"] is None or values["accuracy"] < args.min_field_accuracy]
                gate_failures.extend(f"{name} below {args.min_field_accuracy}" for name in below)
            if args.min_evidence_support is not None:
                score = report["field_evidence_support"]["accuracy"]
                if score is None or score < args.min_evidence_support:
                    gate_failures.append(f"field_evidence_support={score} < {args.min_evidence_support}")
            if gate_failures:
                report["status"] = "gate_failed"
                report["gate_failures"] = gate_failures
                failed = True
        except Exception as exc:
            report = {"provider": provider, "status": "error", "error": str(exc)}
            failed = True
        reports.append(report)

    result = {"dataset": str(args.dataset), "case_count": len(cases), "results": reports}
    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 1 if failed else 0
