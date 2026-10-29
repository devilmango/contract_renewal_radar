from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from pydantic import BaseModel, Field

from .schemas import ContractData, ContractObligation, Evidence


class ExtractionError(RuntimeError):
    pass


class Extractor(Protocol):
    name: str

    def extract(self, text: str) -> ContractData: ...


class ContractExtractionOutput(BaseModel):
    """Provider-facing extraction schema; ownership is assigned during human review."""

    contract: str | None
    start_date: date | None
    expiration_date: date | None
    renewal_notice_days: int | None
    notice_day_type: str
    auto_renew: bool | None
    termination_notice: str | None
    annual_contract_value: float | None
    currency: str | None
    evidence: list[Evidence]
    obligations: list[ContractObligation] = Field(default_factory=list)


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
        date_hits: list[tuple[date, str, str, int, int]] = []
        for match in re.finditer(date_pattern, text):
            parsed = _parse_date(match.group(0))
            if parsed:
                context_start = max(0, match.start() - 100)
                context = text[context_start: min(len(text), match.end() + 100)]
                date_hits.append((parsed, match.group(0), context, match.start() - context_start, context_start))

        start_terms = re.compile(r"effective date|commencement|start date|beginning on|takes effect", re.IGNORECASE)
        end_terms = re.compile(r"expiration|expire|expiry|end date|term ends|term through", re.IGNORECASE)
        for parsed, _raw, context, _date_offset, context_start in date_hits:
            # Date labels conventionally precede the value. Looking both ways in a
            # symmetric window can assign an effective date to a later expiration
            # label (for example, "effective date is X and expiration date is Y").
            label_start = max(0, context_start + len(context) - 200)
            prior: list[tuple[int, str]] = []
            for pattern, field in ((start_terms, "start_date"), (end_terms, "expiration_date")):
                prior.extend((label_start + hit.end(), field) for hit in pattern.finditer(text[label_start:]))
            date_position = context_start + _date_offset
            prior = [(position, field) for position, field in prior if position <= date_position and date_position - position <= 80]
            field = max(prior, key=lambda item: item[0])[1] if prior else None
            if field and field not in result:
                result[field] = parsed
                evidence.append(Evidence(field=field, quote=context.strip()[:1000], confidence=0.72))

        notice = re.search(
            r"(?:(?:at least|no later than|not later than)\s+)?(\d{1,4})\s*(?:\(\s*[\w-]+\s*\))?\s*"
            r"(?:calendar\s+|business\s+)?days?\s+(?:prior|before|in advance of)\s+"
            r"(?:to\s+)?(?:the\s+)?(?:expiration|expiry|end of (?:the\s+)?(?:then-current\s+)?term|renewal|end date)",
            normalized,
            re.IGNORECASE,
        ) or re.search(
            r"(?:renewal|non-?renewal) notice(?: period)?\s*(?:is|of|:)?\s*(\d{1,4})\s*days?",
            normalized,
            re.IGNORECASE,
        )
        if notice:
            result["renewal_notice_days"] = int(notice.group(1))
            evidence.append(Evidence(field="renewal_notice_days", quote=notice.group(0), confidence=0.78))
            result["notice_day_type"] = "business" if re.search(r"business\s+days?", notice.group(0), re.IGNORECASE) else "calendar"

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
                r"auto(?:matically)?\s+renew(?:s|ed)?|renew(?:s|ed)?\s+automatically|automatically\s+extend",
                normalized,
                re.IGNORECASE,
            )
            if positive_auto_renew:
                result["auto_renew"] = True
                match = re.search(
                    r".{0,70}(?:auto(?:matically)?\s+renew(?:s|ed)?|renew(?:s|ed)?\s+automatically|automatically\s+extend).{0,100}",
                    normalized,
                    re.IGNORECASE,
                )
                evidence.append(Evidence(field="auto_renew", quote=match.group(0) if match else "Automatic renewal clause", confidence=0.8))

        termination = re.search(
            r"termination notice(?: period)?\s*(?:is|of|:)?\s*([^.;\n]{1,80})",
            normalized,
            re.IGNORECASE,
        )
        if termination:
            result["termination_notice"] = termination.group(1).strip()
            evidence.append(Evidence(field="termination_notice", quote=termination.group(0), confidence=0.7))

        amount = re.search(
            r"(?:annual contract value|annual fee|annual subscription fee|annual spend)\s*(?:is|of|:|=)?\s*"
            r"(?:(USD|EUR|GBP|CAD|AUD|INR)\s*)?\$?\s*([\d,]+(?:\.\d{1,2})?)\s*(USD|EUR|GBP|CAD|AUD|INR)?",
            normalized,
            re.IGNORECASE,
        )
        if amount:
            result["annual_contract_value"] = amount.group(2).replace(",", "")
            currency = (amount.group(1) or amount.group(3) or ("USD" if "$" in amount.group(0) else "")).upper()
            if currency:
                result["currency"] = currency
            evidence.append(Evidence(field="annual_contract_value", quote=amount.group(0), confidence=0.65))

        obligation_patterns = (
            ("price_increase", re.compile(r"price increase|fee increase|rate increase|price adjustment|annual price escalation", re.I), "Price change"),
            ("insurance_certificate", re.compile(r"certificate of insurance|insurance certificate|proof of insurance", re.I), "Insurance certificate"),
            ("audit_right", re.compile(r"audit right|right to audit|audit the books|audit records", re.I), "Audit right"),
            ("termination_window", re.compile(r"termination notice|non-?renewal notice|notice to terminate", re.I), "Termination notice"),
        )
        obligations: list[ContractObligation] = []
        seen_obligations: set[tuple[str, date]] = set()
        for parsed, raw_date, _context, _date_offset, _context_start in date_hits:
            date_position = _context_start + _date_offset
            prior_boundary = max(text.rfind(mark, 0, date_position) for mark in (".", "!", "?", "\n"))
            following = [position for mark in (".", "!", "?", "\n") if (position := text.find(mark, date_position + len(raw_date))) >= 0]
            window_start = prior_boundary + 1
            window_end = min(following) + 1 if following else len(text)
            excerpt = text[window_start:window_end].strip()
            for category, pattern, label in obligation_patterns:
                if not pattern.search(excerpt) or (category, parsed) in seen_obligations:
                    continue
                evidence_quote = excerpt[:1000]
                recurrence = _recurrence_interval_months(excerpt)
                obligations.append(ContractObligation(
                    category=category, title=label, description=evidence_quote,
                    due_date=parsed,
                    recurrence_interval_months=recurrence,
                    evidence=Evidence(field=f"obligation:{category}", quote=evidence_quote, confidence=0.62),
                ))
                seen_obligations.add((category, parsed))
        if obligations:
            result["obligations"] = obligations

        if not result:
            raise ExtractionError("No contract terms could be identified in the PDF text.")
        result["evidence"] = evidence
        return ContractData.model_validate(result)


@dataclass
class ProviderExtractor:
    provider: str
    model: str
    api_key: str
    name: str = ""
    last_usage: dict[str, int] | None = None

    def __post_init__(self) -> None:
        self.name = self.provider

    def extract(self, text: str) -> ContractData:
        try:
            self.last_usage = None
            payload = self._request(text)
            return ContractData.model_validate(payload)
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError(f"{self.provider} extraction failed: {exc}") from exc

    def _request(self, text: str) -> dict:
        prompt = _user_prompt(text)

        if self.provider in {"openai", "xai"}:
            try:
                from openai import OpenAI
            except ImportError as exc:
                extra = "llm-xai" if self.provider == "xai" else "llm"
                raise _install_error(extra) from exc
            client_args = {"api_key": self.api_key}
            if self.provider == "xai":
                client_args["base_url"] = "https://api.x.ai/v1"
            response = OpenAI(**client_args).chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "contract_terms", "strict": True, "schema": _contract_schema()},
                },
                messages=[{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            )
            self.last_usage = _usage_counts(getattr(response, "usage", None), "prompt_tokens", "completion_tokens")
            return _parse_json(response.choices[0].message.content or "")

        if self.provider == "anthropic":
            try:
                import anthropic
            except ImportError as exc:
                raise _install_error("llm-anthropic") from exc
            schema = _contract_schema()
            transform_schema = getattr(anthropic, "transform_schema", None)
            if transform_schema:
                schema = transform_schema(schema)
            response = anthropic.Anthropic(api_key=self.api_key).messages.create(
                model=self.model,
                max_tokens=2048,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            self.last_usage = _usage_counts(getattr(response, "usage", None), "input_tokens", "output_tokens")
            content = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
            return _parse_json(content)

        if self.provider == "google":
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:
                raise _install_error("llm-google") from exc
            response = genai.Client(api_key=self.api_key).models.generate_content(
                model=self.model,
                contents=f"{_SYSTEM_PROMPT}\n\n{prompt}",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ContractExtractionOutput,
                    temperature=0,
                ),
            )
            self.last_usage = _usage_counts(getattr(response, "usage_metadata", None), "prompt_token_count", "candidates_token_count")
            return _parse_json(response.text or "")

        if self.provider == "mistral":
            try:
                from mistralai.client import Mistral
            except ImportError as exc:
                raise _install_error("llm-mistral") from exc
            response = Mistral(api_key=self.api_key).chat.parse(
                model=self.model,
                messages=[{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                response_format=ContractExtractionOutput,
                max_tokens=2048,
                temperature=0,
            )
            self.last_usage = _usage_counts(getattr(response, "usage", None), "prompt_tokens", "completion_tokens")
            return _parse_json(response.choices[0].message.content or "")

        if self.provider == "cohere":
            try:
                import cohere
            except ImportError as exc:
                raise _install_error("llm-cohere") from exc
            response = cohere.ClientV2(api_key=self.api_key).chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"Generate a JSON object matching the required contract extraction schema.\n\n{prompt}"},
                ],
                response_format={"type": "json_object", "schema": _contract_schema(nullable_as_any_of=True)},
            )
            units = getattr(response, "usage", None)
            billed = getattr(units, "billed_units", None) if units else None
            self.last_usage = _usage_counts(billed, "input_tokens", "output_tokens")
            return _parse_json(response.message.content[0].text)

        raise ExtractionError(f"Unsupported LLM provider: {self.provider}")


_SYSTEM_PROMPT = (
    "Extract contract terms exactly as written. Return null when a value is not explicit. "
    "For notice_day_type, return business only when the contract explicitly says business days; otherwise return calendar. "
    "Do not calculate dates or infer missing terms. For each extracted field, provide a short "
    "verbatim evidence quote from the document and a confidence from 0 to 1. Extract annual_contract_value "
    "and currency only when an annual amount and its currency are explicit; do not convert billing periods. "
    "Also extract schedulable obligations for price increases, insurance certificates, audit rights, and termination windows. "
    "For each obligation, include a verbatim evidence quote and either its explicit due_date or an explicit day offset from the contract start_date or expiration_date. "
    "If the clause explicitly repeats on a schedule, include recurrence_interval_months (for example annual=12, quarterly=3); otherwise use null. "
    "Do not invent owners, dates, offsets, or obligations; omit an obligation when no supported schedule can be identified."
)


def _recurrence_interval_months(text: str) -> int | None:
    normalized = text.casefold()
    if re.search(r"\b(annually|annual|each year|every year|yearly)\b", normalized):
        return 12
    if re.search(r"\b(quarterly|each quarter|every quarter)\b", normalized):
        return 3
    if re.search(r"\b(semi-?annually|biannually|twice a year|twice yearly|every six months?)\b", normalized):
        return 6
    if re.search(r"\b(monthly|each month|every month)\b", normalized):
        return 1
    match = re.search(r"\bevery\s+(\d{1,3}|two|three|four|five|six|eight|ten|twelve)\s+months?\b", normalized)
    if not match:
        return None
    amount = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8, "ten": 10, "twelve": 12}.get(match.group(1), match.group(1))
    amount = int(amount)
    return amount if 1 <= amount <= 120 else None


def _user_prompt(text: str) -> str:
    return f"Extract the contract terms from the following document text.\n\n<contract>\n{text[:100000]}\n</contract>"


def _contract_schema(*, nullable_as_any_of: bool = False) -> dict:
    def nullable(kind: str, **kwargs) -> dict:
        if nullable_as_any_of:
            return {"anyOf": [{"type": kind, **kwargs}, {"type": "null"}]}
        return {"type": [kind, "null"], **kwargs}

    return {
        "type": "object",
        "properties": {
            "contract": nullable("string"),
            "start_date": nullable("string", description="ISO date YYYY-MM-DD"),
            "expiration_date": nullable("string", description="ISO date YYYY-MM-DD"),
            "renewal_notice_days": nullable("integer"),
            "notice_day_type": {"type": "string", "enum": ["calendar", "business"]},
            "auto_renew": nullable("boolean"),
            "termination_notice": nullable("string"),
            "annual_contract_value": nullable("number", description="Annual amount, without currency conversion"),
            "currency": nullable("string", description="Explicit three-letter currency code"),
            "obligations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": ["price_increase", "insurance_certificate", "audit_right", "termination_window", "other"]},
                        "title": {"type": "string"},
                        "description": nullable("string"),
                        "due_date": nullable("string", description="ISO date YYYY-MM-DD when explicitly stated"),
                        "due_offset_days": nullable("integer"),
                        "due_offset_from": {"type": ["string", "null"], "enum": ["start_date", "expiration_date", None]},
                        "due_offset_direction": {"type": ["string", "null"], "enum": ["before", "after", None]},
                        "recurrence_interval_months": nullable("integer"),
                        "evidence": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string"}, "quote": {"type": "string"},
                                "page": nullable("integer"), "confidence": {"type": "number"},
                            },
                            "required": ["field", "quote", "page", "confidence"], "additionalProperties": False,
                        },
                    },
                    "required": ["category", "title", "description", "due_date", "due_offset_days", "due_offset_from", "due_offset_direction", "recurrence_interval_months", "evidence"],
                    "additionalProperties": False,
                },
            },
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
        "required": ["contract", "start_date", "expiration_date", "renewal_notice_days", "notice_day_type", "auto_renew", "termination_notice", "annual_contract_value", "currency", "evidence", "obligations"],
        "additionalProperties": False,
    }


def _parse_json(content: str) -> dict:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ExtractionError("The selected provider returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ExtractionError("The selected provider did not return a JSON object.")
    return payload


def _usage_counts(usage, input_name: str, output_name: str) -> dict[str, int] | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        input_tokens, output_tokens = usage.get(input_name), usage.get(output_name)
    else:
        input_tokens, output_tokens = getattr(usage, input_name, None), getattr(usage, output_name, None)
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _install_error(extra: str) -> ExtractionError:
    return ExtractionError(f"Install support for this provider with `pip install '.[{extra}]'`.")


_PROVIDER_KEYS = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "mistral": ("MISTRAL_API_KEY",),
    "cohere": ("COHERE_API_KEY",),
    "xai": ("XAI_API_KEY",),
}

_PROVIDER_MODELS = {
    "openai": ("OPENAI_MODEL", "gpt-4o-mini"),
    "anthropic": ("ANTHROPIC_MODEL", "claude-sonnet-5"),
    "google": ("GOOGLE_MODEL", "gemini-3.8-flash"),
    "mistral": ("MISTRAL_MODEL", "ministral-8b-latest"),
    "cohere": ("COHERE_MODEL", "command-a-plus-05-2026"),
    "xai": ("XAI_MODEL", "grok-4.7"),
}

_PROVIDER_MODEL_ALIASES = {"google": ("GEMINI_MODEL",)}


def configured_llm_providers() -> list[str]:
    allowed = _allowed_llm_providers()
    return [
        name
        for name, env_names in _PROVIDER_KEYS.items()
        if name in allowed and any(os.getenv(key) for key in env_names)
    ]


def _allowed_llm_providers() -> set[str]:
    raw = os.getenv("LLM_ALLOWED_PROVIDERS", "*").strip().casefold()
    if not raw or raw == "*":
        return set(_PROVIDER_KEYS) | {"rules"}
    allowed = {provider.strip() for provider in raw.split(",") if provider.strip()}
    unknown = allowed - set(_PROVIDER_KEYS) - {"rules"}
    if unknown:
        raise ExtractionError(f"Unsupported LLM_ALLOWED_PROVIDERS entries: {', '.join(sorted(unknown))}.")
    return allowed | {"rules"}


def get_extractor(provider_override: str | None = None) -> Extractor:
    requested = (provider_override or os.getenv("LLM_PROVIDER", "auto")).strip().lower()
    allowed = _allowed_llm_providers()
    if requested == "rules":
        return RulesExtractor()
    if requested == "auto":
        provider = next(
            (name for name, env_names in _PROVIDER_KEYS.items() if name in allowed and any(os.getenv(key) for key in env_names)),
            None,
        )
        if provider is None:
            return RulesExtractor()
    elif requested in _PROVIDER_KEYS:
        if requested not in allowed:
            raise ExtractionError(f"Provider '{requested}' is blocked by LLM_ALLOWED_PROVIDERS.")
        provider = requested
    else:
        raise ExtractionError(f"Unsupported LLM_PROVIDER '{requested}'. Choose one of: rules, {', '.join(_PROVIDER_KEYS)}.")

    api_key = next((os.getenv(key) for key in _PROVIDER_KEYS[provider] if os.getenv(key)), None)
    if not api_key:
        expected = " or ".join(_PROVIDER_KEYS[provider])
        raise ExtractionError(f"LLM_PROVIDER is '{provider}', but {expected} is not set.")
    model_env, default_model = _PROVIDER_MODELS[provider]
    aliases = _PROVIDER_MODEL_ALIASES.get(provider, ())
    model = (
        os.getenv("LLM_MODEL")
        or os.getenv(model_env)
        or next((os.getenv(name) for name in aliases if os.getenv(name)), None)
        or default_model
    )
    return ProviderExtractor(provider=provider, model=model, api_key=api_key)


def _parse_date(value: str) -> date | None:
    from datetime import datetime

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(value.replace(",", ""), fmt.replace(",", "")).date()
        except ValueError:
            continue
    return None
