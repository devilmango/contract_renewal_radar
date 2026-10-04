from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .schemas import ContractData, Evidence


class ExtractionError(RuntimeError):
    pass


class Extractor(Protocol):
    name: str

    def extract(self, text: str) -> ContractData: ...


@dataclass
class RulesExtractor:
    """Small, transparent fallback parser. It proposes terms; humans still confirm them."""

    name: str = "rules"

    def extract(self, text: str) -> ContractData:
        normalized = " ".join(text.split())
        evidence: list[Evidence] = []
        result: dict = {}

        title_match = re.search(
            r"(?:agreement|contract)\s*(?:title|name)?\s*[:\-]\s*([^.;\n]{3,120})",
            text,
            re.IGNORECASE,
        )
        if title_match:
            result["contract"] = title_match.group(1).strip()
            evidence.append(Evidence(field="contract", quote=title_match.group(0)[:1000], confidence=0.7))

        date_pattern = r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})\b"
        date_hits: list[tuple[date, str, str]] = []
        for match in re.finditer(date_pattern, text):
            parsed = _parse_date(match.group(0))
            if parsed:
                context = text[max(0, match.start() - 100): min(len(text), match.end() + 100)]
                date_hits.append((parsed, match.group(0), context))

        for parsed, raw, context in date_hits:
            lower = context.lower()
            field = None
            if any(term in lower for term in ("effective date", "commencement", "start date", "beginning on")):
                field = "start_date"
            elif any(term in lower for term in ("expiration", "expires", "expiry", "end date", "initial term ends")):
                field = "expiration_date"
            if field and field not in result:
                result[field] = parsed
                evidence.append(Evidence(field=field, quote=context.strip()[:1000], confidence=0.72))

        notice = re.search(
            r"(?:at least\s+)?(\d{1,4})\s*(?:\(\s*\d+\s*\))?\s*"
            r"(?:calendar\s+|business\s+)?days?\s+(?:prior|before|in advance of)\s+"
            r"(?:the\s+)?(?:expiration|expiry|end of (?:the )?term|renewal)",
            normalized,
            re.IGNORECASE,
        ) or re.search(
            r"(?:renewal|non-?renewal|termination) notice(?: period)?\s*(?:of|:)?\s*(\d{1,4})\s*days?",
            normalized,
            re.IGNORECASE,
        )
        if notice:
            result["renewal_notice_days"] = int(notice.group(1))
            evidence.append(Evidence(field="renewal_notice_days", quote=notice.group(0), confidence=0.78))

        negative_auto_renew = re.search(
            r"(?:will not|does not|shall not)\s+(?:automatically\s+)?renew|no automatic renewal",
            normalized,
            re.IGNORECASE,
        )
        if negative_auto_renew:
            result["auto_renew"] = False
            evidence.append(Evidence(field="auto_renew", quote=negative_auto_renew.group(0), confidence=0.8))
        else:
            positive_auto_renew = re.search(
                r"auto(?:matically)?\s+renew|automatically\s+extend",
                normalized,
                re.IGNORECASE,
            )
            if positive_auto_renew:
                result["auto_renew"] = True
                match = re.search(r".{0,70}(?:auto(?:matically)?\s+renew|automatically\s+extend).{0,100}", normalized, re.IGNORECASE)
                evidence.append(Evidence(field="auto_renew", quote=match.group(0) if match else "Automatic renewal clause", confidence=0.8))

        termination = re.search(r"termination notice(?: period)?\s*(?:of|:)?\s*([^.;\n]{1,80})", normalized, re.IGNORECASE)
        if termination:
            result["termination_notice"] = termination.group(1).strip()
            evidence.append(Evidence(field="termination_notice", quote=termination.group(0), confidence=0.7))

        if not result:
            raise ExtractionError("No contract terms could be identified in the PDF text.")
        result["evidence"] = evidence
        return ContractData.model_validate(result)


@dataclass
class OpenAIExtractor:
    model: str
    name: str = "openai"

    def extract(self, text: str) -> ContractData:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ExtractionError("Install the optional LLM extra: pip install '.[llm]'") from exc

        schema = {
            "type": "object",
            "properties": {
                "contract": {"type": ["string", "null"]},
                "start_date": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD"},
                "expiration_date": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD"},
                "renewal_notice_days": {"type": ["integer", "null"]},
                "auto_renew": {"type": ["boolean", "null"]},
                "termination_notice": {"type": ["string", "null"]},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "quote": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                        "required": ["field", "quote", "confidence"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["contract", "start_date", "expiration_date", "renewal_notice_days", "auto_renew", "termination_notice", "evidence"],
            "additionalProperties": False,
        }
        try:
            response = OpenAI().chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={"type": "json_schema", "json_schema": {"name": "contract_terms", "strict": True, "schema": schema}},
                messages=[
                    {"role": "system", "content": "Extract contract terms exactly. Return null when a value is not explicit. Do not calculate dates. Attach a short verbatim evidence quote for each extracted field."},
                    {"role": "user", "content": text[:100000]},
                ],
            )
            payload = json.loads(response.choices[0].message.content or "{}")
            return ContractData.model_validate(payload)
        except Exception as exc:
            raise ExtractionError(f"LLM extraction failed: {exc}") from exc


def get_extractor() -> Extractor:
    if os.getenv("OPENAI_API_KEY"):
        return OpenAIExtractor(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    return RulesExtractor()


def _parse_date(value: str) -> date | None:
    from datetime import datetime

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(value.replace(",", ""), fmt.replace(",", "")).date()
        except ValueError:
            continue
    return None
