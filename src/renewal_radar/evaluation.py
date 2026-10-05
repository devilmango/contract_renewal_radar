from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime
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
    seen_ids: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(case, dict) or not case.get("id") or not case.get("contract_text") or not isinstance(case.get("expected"), dict):
            raise ValueError(f"Invalid evaluation fixture: {path}")
        unknown_fields = set(case["expected"]) - set(SCORED_FIELDS)
        if unknown_fields:
            raise ValueError(f"Unknown expected fields in {path}: {', '.join(sorted(unknown_fields))}")
        ContractData.model_validate(case["expected"])
        if case["id"] in seen_ids:
            raise ValueError(f"Duplicate evaluation case id: {case['id']}")
        seen_ids.add(case["id"])
        source_type = case.get("source_type", "synthetic")
        if source_type not in {"synthetic", "ocr", "approved_deidentified"}:
            raise ValueError(f"Unsupported source_type in {path}: {source_type}")
        if source_type == "approved_deidentified":
            governance = case.get("governance")
            if not isinstance(governance, dict) or governance.get("approved") is not True or governance.get("deidentified") is not True or not governance.get("approval_reference"):
                raise ValueError(f"Approved de-identified fixture {path} needs governance.approved, governance.deidentified, and governance.approval_reference")
            if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b\d{3}[- .]?\d{2}[- .]?\d{4}\b", case["contract_text"]):
                raise ValueError(f"Possible email address or government ID remains in evaluation fixture {path}")
        case["source_type"] = source_type
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
    support_total = support_count = 0
    elapsed_seconds = 0.0
    input_tokens = output_tokens = usage_cases = 0
    errors: list[dict[str, str]] = []
    case_results = []
    source_type_counts: dict[str, dict[str, int]] = {}

    for case in cases:
        case_count += 1
        source_type = case["source_type"]
        source_metrics = source_type_counts.setdefault(source_type, {"cases": 0, "exact_matches": 0, "fields": {}})
        source_metrics["cases"] += 1
        started = time.perf_counter()
        try:
            extracted = extractor.extract(case["contract_text"])
        except Exception as exc:
            elapsed_seconds += time.perf_counter() - started
            errors.append({"case": case["id"], "error": str(exc)})
            case_results.append({"case": case["id"], "source_type": source_type, "exact_match": False, "error": str(exc)})
            for field in case["expected"]:
                field_counts[field]["total"] += 1
                field_counts[field]["missing"] += 1
                source_field = source_metrics["fields"].setdefault(field, {"correct": 0, "total": 0})
                source_field["total"] += 1
            continue
        elapsed_seconds += time.perf_counter() - started
        usage = getattr(extractor, "last_usage", None)
        if usage:
            input_tokens += usage["input_tokens"]
            output_tokens += usage["output_tokens"]
            usage_cases += 1

        case_correct = True
        field_results = {}
        case_evidence_support = []
        evidence_by_field = {item.field: item for item in extracted.evidence}
        for field, expected in case["expected"].items():
            actual = getattr(extracted, field)
            counts = field_counts[field]
            counts["total"] += 1
            if actual is None and expected is None:
                counts["correct"] += 1
                matched = True
            elif actual is None:
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
            source_field = source_metrics["fields"].setdefault(field, {"correct": 0, "total": 0})
            source_field["total"] += 1
            source_field["correct"] += int(matched)
            if actual is not None:
                supporting_evidence = evidence_by_field.get(field)
                if field == "notice_day_type" and supporting_evidence is None:
                    supporting_evidence = evidence_by_field.get("renewal_notice_days")
                supported = _evidence_supports(field, actual, supporting_evidence.quote if supporting_evidence else None)
                case_evidence_support.append({"field": field, "supported": supported})
                support_total += 1
                support_count += int(supported)
        exact_cases += int(case_correct)
        source_metrics["exact_matches"] += int(case_correct)

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
            "source_type": source_type,
            "exact_match": case_correct,
            "fields": field_results,
            "evidence": evidence_results,
            "field_evidence_support": case_evidence_support,
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
        "field_evidence_support": {
            "supported": support_count,
            "total": support_total,
            "accuracy": _ratio(support_count, support_total),
        },
        "source_types": {
            source_type: {
                "cases": counts["cases"],
                "exact_matches": counts["exact_matches"],
                "exact_case_accuracy": _ratio(counts["exact_matches"], counts["cases"]),
                "fields": {
                    name: {**values, "accuracy": _ratio(values["correct"], values["total"])}
                    for name, values in counts["fields"].items()
                },
            }
            for source_type, counts in source_type_counts.items()
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


def _evidence_supports(field: str, value: Any, quote: str | None) -> bool:
    if value is None or not quote:
        return False
    folded = _normalize_text(quote)
    if field in {"start_date", "expiration_date"}:
        target = value.isoformat() if hasattr(value, "isoformat") else str(value)
        for candidate in re.findall(r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})\b", quote):
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y"):
                try:
                    if datetime.strptime(candidate.replace(",", ""), fmt.replace(",", "")).date().isoformat() == target:
                        return True
                except ValueError:
                    pass
        return False
    if field == "renewal_notice_days":
        clause_context = bool(re.search(
            r"\b(?:renew(?:al)?|non[- ]renewal|before (?:the )?expiration|prior to (?:the )?expiration|in advance of|end date|term)\b",
            folded,
        ))
        numbers = re.findall(r"\b(\d{1,4})\s*(?:\([\w-]+\))?\s*(?:(?:calendar|business)\s+)?days?\b", folded)
        return clause_context and any(int(number) == value for number in numbers)
    if field == "notice_day_type":
        return "business day" in folded if value == "business" else "business day" not in folded
    if field == "auto_renew":
        negative = bool(re.search(r"(?:will not|does not|shall not|not automatically|no automatic)\s+(?:automatically\s+)?(?:renew|extend)|does not renew", folded))
        positive = bool(re.search(r"(?:automatically\s+renew|auto(?:matically)?\s+renew|renew\s+automatically|automatically\s+extend)", folded))
        return not negative if value is True else negative if value is False else False
    if field == "termination_notice":
        return _normalize_text(str(value)) in folded
    return _normalize_text(str(value)) in folded
