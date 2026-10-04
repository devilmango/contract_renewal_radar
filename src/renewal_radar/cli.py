from __future__ import annotations

import argparse
import json

from .reminders import run_reminders
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(prog="renewal-radar", description="Contract Renewal Radar CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Run the API service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    subparsers.add_parser("run-reminders", help="Send due renewal reminders and escalations once")
    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn

        uvicorn.run("renewal_radar.api:app", host=args.host, port=args.port)
    else:
        print(json.dumps(run_reminders(Store()), indent=2))
