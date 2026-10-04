from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from .schemas import ContractData


SCORED_FIELDS = (
    "start_date",
    "expiration_date",
    "renewal_notice_days",
    "notice_day_type",
    "auto_renew",
    "termination_notice",
)


def load_cases(directory: Path) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(case, dict) or not case.get("id") or not case.get("contract_text") or not isinstance(case.get("expected"), dict):
            raise ValueError(f"Invalid evaluation fixture: {path}")
        unknown_fields = set(case["expected"]) - set(SCORED_FIELDS)
        if unknown_fields:
            raise ValueError(f"Unknown expected fields in {path}: {', '.join(sorted(unknown_fields))}")
        ContractData.model_validate(case["expected"])
        case["fixture"] = path.name
        cases.append(case)
    if not cases:
        raise ValueError(f"No JSON evaluation cases found in {directory}")
    return cases


def evaluate_extractor(extractor, cases: list[dict[str, Any]]) -> dict[str, Any]:
    field_counts = {
        field: {"correct": 0, "missing": 0, "incorrect": 0, "total": 0}
        for field in SCORED_FIELDS
    }
    case_count = exact_cases = 0
    evidence_total = evidence_grounded = 0
    elapsed_seconds = 0.0
    input_tokens = output_tokens = usage_cases = 0
    errors: list[dict[str, str]] = []
    case_results = []

    for case in cases:
        case_count += 1
        started = time.perf_counter()
        try:
            extracted = extractor.extract(case["contract_text"])
        except Exception as exc:
            elapsed_seconds += time.perf_counter() - started
            errors.append({"case": case["id"], "error": str(exc)})
            case_results.append({"case": case["id"], "exact_match": False, "error": str(exc)})
            for field in case["expected"]:
                field_counts[field]["total"] += 1
                field_counts[field]["missing"] += 1
            continue
        elapsed_seconds += time.perf_counter() - started
        usage = getattr(extractor, "last_usage", None)
        if usage:
            input_tokens += usage["input_tokens"]
            output_tokens += usage["output_tokens"]
            usage_cases += 1

        case_correct = True
        field_results = {}
        for field, expected in case["expected"].items():
            actual = getattr(extracted, field)
            counts = field_counts[field]
            counts["total"] += 1
            if actual is None:
                counts["missing"] += 1
                case_correct = False
                matched = False
            elif _normalize(actual) == _normalize(expected):
                counts["correct"] += 1
                matched = True
            else:
                counts["incorrect"] += 1
                case_correct = False
                matched = False
            field_results[field] = {
                "expected": _json_value(expected),
                "actual": _json_value(actual),
                "match": matched,
            }
        exact_cases += int(case_correct)

        source = _normalize_text(case["contract_text"])
        evidence_results = []
        for evidence in extracted.evidence:
            evidence_total += 1
            quote = _normalize_text(evidence.quote)
            grounded = bool(quote and quote in source)
            if grounded:
                evidence_grounded += 1
            evidence_results.append({"field": evidence.field, "quote": evidence.quote, "grounded": grounded})
        case_result = {
            "case": case["id"],
            "exact_match": case_correct,
            "fields": field_results,
            "evidence": evidence_results,
        }
        if usage:
            case_result["token_usage"] = usage
        case_results.append(case_result)

    for counts in field_counts.values():
        counts["accuracy"] = _ratio(counts["correct"], counts["total"])

    report = {
        "provider": extractor.name,
        "model": getattr(extractor, "model", None),
        "elapsed_seconds": round(elapsed_seconds, 4),
        "average_case_seconds": round(elapsed_seconds / case_count, 4) if case_count else None,
        "dataset_cases": len(cases),
        "cases_scored": case_count,
        "exact_case_match": {"correct": exact_cases, "total": case_count, "accuracy": _ratio(exact_cases, case_count)},
        "fields": field_counts,
        "evidence_grounding": {
            "grounded": evidence_grounded,
            "total_quotes": evidence_total,
            "accuracy": _ratio(evidence_grounded, evidence_total),
        },
        "case_results": case_results,
        "errors": errors,
    }
    if usage_cases:
        report["token_usage"] = {
            "cases_with_usage": usage_cases,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        input_rate = os.getenv("EVAL_INPUT_USD_PER_MILLION_TOKENS")
        output_rate = os.getenv("EVAL_OUTPUT_USD_PER_MILLION_TOKENS")
        if input_rate is not None and output_rate is not None:
            try:
                input_rate_value = float(input_rate)
                output_rate_value = float(output_rate)
                if not all(math.isfinite(rate) and rate >= 0 for rate in (input_rate_value, output_rate_value)):
                    raise ValueError("Rates must be finite, non-negative numbers")
                report["estimated_cost_usd"] = round(
                    input_tokens * input_rate_value / 1_000_000
                    + output_tokens * output_rate_value / 1_000_000,
                    6,
                )
            except ValueError:
                report["estimated_cost_usd"] = None
    return report


def _normalize(value: Any) -> str:
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    return _normalize_text(str(value))


def _json_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None
