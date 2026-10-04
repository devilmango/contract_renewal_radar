from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from pydantic import BaseModel

from .schemas import ContractData, Evidence


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
    auto_renew: bool | None
    termination_notice: str | None
    evidence: list[Evidence]


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

        for parsed, _raw, context in date_hits:
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
                match = re.search(
                    r".{0,70}(?:auto(?:matically)?\s+renew|automatically\s+extend).{0,100}",
                    normalized,
                    re.IGNORECASE,
                )
                evidence.append(Evidence(field="auto_renew", quote=match.group(0) if match else "Automatic renewal clause", confidence=0.8))

        termination = re.search(
            r"termination notice(?: period)?\s*(?:of|:)?\s*([^.;\n]{1,80})",
            normalized,
            re.IGNORECASE,
        )
        if termination:
            result["termination_notice"] = termination.group(1).strip()
            evidence.append(Evidence(field="termination_notice", quote=termination.group(0), confidence=0.7))

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

    def __post_init__(self) -> None:
        self.name = self.provider

    def extract(self, text: str) -> ContractData:
        try:
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
            return _parse_json(response.message.content[0].text)

        raise ExtractionError(f"Unsupported LLM provider: {self.provider}")


_SYSTEM_PROMPT = (
    "Extract contract terms exactly as written. Return null when a value is not explicit. "
    "Do not calculate dates or infer missing terms. For each extracted field, provide a short "
    "verbatim evidence quote from the document and a confidence from 0 to 1."
)


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
            "auto_renew": nullable("boolean"),
            "termination_notice": nullable("string"),
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


def _parse_json(content: str) -> dict:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ExtractionError("The selected provider returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ExtractionError("The selected provider did not return a JSON object.")
    return payload


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


def get_extractor() -> Extractor:
    requested = os.getenv("LLM_PROVIDER", "auto").strip().lower()
    if requested == "rules":
        return RulesExtractor()
    if requested == "auto":
        provider = next(
            (name for name, env_names in _PROVIDER_KEYS.items() if any(os.getenv(key) for key in env_names)),
            None,
        )
        if provider is None:
            return RulesExtractor()
    elif requested in _PROVIDER_KEYS:
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
