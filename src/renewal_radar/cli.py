from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .auth import SUPPORTED_ROLES, create_token_config
from .evaluation import evaluate_extractor, load_cases
from .extractor import configured_llm_providers, get_extractor
from .reminders import run_reminders
from .store import Store


PROVIDERS = ("auto", "rules", "openai", "anthropic", "google", "mistral", "cohere", "xai", "all")


def main() -> int:
    parser = argparse.ArgumentParser(prog="renewal-radar", description="Contract Renewal Radar")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the API service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)

    subparsers.add_parser("run-reminders", help="Send due renewal reminders and escalations once")

    create_token = subparsers.add_parser("create-token", help="Create a bearer token and hashed user configuration entry")
    create_token.add_argument("--actor", required=True, help="Identity recorded in the audit trail")
    create_token.add_argument("--role", required=True, choices=sorted(SUPPORTED_ROLES), action="append", help="Role to grant; may be repeated")
    create_token.add_argument("--email", help="Email used to scope owner tasks and receive notifications")

    evaluate = subparsers.add_parser("evaluate", help="Measure extraction accuracy on the included redacted contract set")
    evaluate.add_argument("--provider", choices=PROVIDERS, default="auto", help="Provider to evaluate, or 'all' configured providers plus rules baseline")
    evaluate.add_argument("--dataset", type=Path, default=Path("evaluation/cases"), help="Directory containing JSON evaluation cases")
    evaluate.add_argument("--output", type=Path, help="Optional path for the JSON report")

    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn

        uvicorn.run("renewal_radar.api:app", host=args.host, port=args.port)
        return 0

    if args.command == "run-reminders":
        print(json.dumps(run_reminders(Store()), indent=2))
        return 0

    if args.command == "create-token":
        token, user_entry = create_token_config(args.actor, args.role, args.email)
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
