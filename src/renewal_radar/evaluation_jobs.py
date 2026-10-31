from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .evaluation import evaluate_extractor, load_cases
from .extractor import configured_llm_providers, get_extractor
from .notifications import delivery_channel, send_notification
from .policies import effective_escalation_policy


def provider_configuration_signature() -> str:
    providers = configured_llm_providers()
    config = {"providers": providers, "models": {provider: getattr(get_extractor(provider), "model", None) for provider in providers}}
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def run_scheduled_provider_evaluation(store, tenant_id: str, signature: str) -> dict:
    candidates = [Path.cwd() / "evaluation" / "cases", Path(__file__).resolve().parents[2] / "evaluation" / "cases"]
    dataset_path = next((path for path in candidates if path.is_dir()), None)
    if dataset_path is None:
        raise RuntimeError("The redacted evaluation fixture set is not installed")
    cases = load_cases(dataset_path)
    payload = [{"id": case["id"], "text": case["contract_text"], "active_contract_text": case.get("active_contract_text"),
                "expected": case["expected"], "expected_changed_fields": case.get("expected_changed_fields", []),
                "expected_clause_categories": case.get("expected_clause_categories", [])} for case in cases]
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    providers = ["rules", *configured_llm_providers()]
    runs = []
    for provider in dict.fromkeys(providers):
        try:
            extractor = get_extractor(provider)
            report = evaluate_extractor(extractor, cases)
        except Exception as exc:
            try:
                extractor = get_extractor(provider)
            except Exception:
                continue
            report = {"provider": provider, "model": getattr(extractor, "model", None), "cases_scored": len(cases),
                      "errors": [{"error": str(exc)}], "exact_case_match": {"accuracy": 0.0}, "field_evidence_support": {"accuracy": 0.0}}
        run = store.record_provider_evaluation(tenant_id, provider, report, fingerprint, "scheduled-evaluation")
        runs.append(run)
        for alert in run["alerts"]:
            _notify_drift(store, tenant_id, run, alert)
    if not runs:
        raise RuntimeError("No configured provider could be evaluated")
    store.complete_provider_evaluation_schedule(tenant_id, signature or provider_configuration_signature())
    return {"dataset_fingerprint": fingerprint, "runs": runs}


def _notify_drift(store, tenant_id: str, run: dict, alert: dict) -> None:
    schedule = store.get_provider_evaluation_schedule(tenant_id) or {}
    policy = effective_escalation_policy(store, tenant_id)
    recipients = schedule.get("alert_emails") or policy.get("escalation_recipients") or []
    if not recipients:
        recipients = [email.strip() for email in os.getenv("PROVIDER_EVALUATION_ALERT_EMAILS", "").split(",") if email.strip()]
    if not recipients:
        recipients = [os.getenv("ESCALATION_EMAIL", "").strip() or None]
    channels = policy.get("escalation_channels") or ["email"]
    task = {"id": f"evaluation:{run['id']}:{alert['metric']}", "tenant_id": tenant_id,
            "title": f"Provider drift detected: {run['provider']} {alert['metric']}", "owner_email": None,
            "task_kind": "renewal", "due_date": "", "expiration_date": ""}
    for requested_channel in channels:
        channel_recipients = recipients if requested_channel == "email" else [None]
        for recipient in channel_recipients:
            started = time.perf_counter()
            channel, status, error = "log", "sent", None
            task["owner_email"] = recipient
            try:
                channel = delivery_channel(task, True, recipient, store=store, requested_channel=requested_channel)
                options = {"escalation": True, "recipient_email": recipient,
                           "escalation_label": f"provider evaluation drop {alert['drop']:.0%}", "store": store}
                if requested_channel != "email":
                    options["channel"] = requested_channel
                send_notification(task, **options)
            except Exception as exc:
                status, error = "failed", f"{type(exc).__name__}: {exc}"
            store.record_evaluation_alert_delivery(tenant_id, run["id"], run["provider"], alert["metric"], recipient,
                                                   channel, status, round((time.perf_counter() - started) * 1000), error)
