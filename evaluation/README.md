# Extraction evaluation set

The checked-in fixtures are fictional and use generic party placeholders such as `[CUSTOMER]`, `[PROVIDER]`, and `[VENDOR]`. They cover fixed and auto-renewing terms, calendar and business-day notices, amendments, ambiguous extensions, explicit absent notice terms, and two OCR-style text cases. The `source_type` field lets reports track `synthetic`, `ocr`, and `approved_deidentified` subsets separately.

Each JSON file contains a stable `id`, a `contract_text` excerpt, and reviewed `expected` values. Include fields with a `null` expected value when the source explicitly does not contain that term; this makes false-positive extraction measurable. `notice_day_type` is scored when the fixture defines a renewal notice convention.

To add internal organization examples, obtain approval from the contract/data owner, de-identify all parties and personal identifiers, and keep the dataset in an access-controlled internal path. Do not put confidential contracts or unapproved agreement text in this public repository. An approved de-identified fixture must record its approval metadata, for example:

```json
{
  "id": "internal-redacted-001",
  "source_type": "approved_deidentified",
  "governance": {
    "approved": true,
    "deidentified": true,
    "approval_reference": "LEGAL-1234"
  },
  "contract_text": "[CUSTOMER] Services Agreement ...",
  "expected": {
    "start_date": "2025-01-01",
    "expiration_date": "2026-12-31",
    "renewal_notice_days": 60,
    "auto_renew": true
  }
}
```

The loader rejects approved-deidentified fixtures without all three governance fields and performs a basic email/government-ID scan. This is a guardrail, not a substitute for a human privacy review. Preserve the approval record in the organization's controlled dataset rather than publishing sensitive source material.

Run the deterministic baseline from the repository root:

```bash
renewal-radar evaluate --provider rules
```

Run a provider by name after configuring its API key and installing its optional dependency extra:

```bash
renewal-radar evaluate --provider openai
renewal-radar evaluate --provider anthropic
renewal-radar evaluate --provider google
renewal-radar evaluate --provider mistral
renewal-radar evaluate --provider cohere
renewal-radar evaluate --provider xai
```

Compare every configured provider with the rules baseline:

```bash
renewal-radar evaluate --provider all --output evaluation/reports/all-providers.json
```

Set minimum exact-case and field accuracy thresholds to fail the command when a provider falls below your release bar:

```bash
renewal-radar evaluate --provider rules --min-exact-accuracy 0.70 --min-field-accuracy 0.70 --min-evidence-support 0.90
```

GitHub Actions runs this deterministic gate on pull requests and pushes to `main` or `master`. LLM evaluations remain opt-in because they call external APIs and may incur charges; `LLM_ALLOWED_PROVIDERS` is enforced for evaluation runs too.

## Metrics

- **Field accuracy:** exact matches per expected field. A missing or invalid extraction does not count as correct.
- **Exact case match:** a case counts as correct only when every expected field matches.
- **Evidence grounding:** fraction of returned evidence quotes that occur verbatim in the source after case-folding and whitespace normalization.
- **Field evidence support:** fraction of scored non-null extracted values whose field-specific evidence quote also expresses that value. For dates the evaluator parses the date from the quote; for notice periods it checks the day count and renewal/expiration context; for other fields it checks the clause signal. This remains a deterministic proxy and should be reviewed when adding new clause forms.
- **Source-type breakdown:** exact-case and per-field accuracy for each `source_type`, so OCR regressions can be isolated from selectable-text fixtures.
- **Latency:** total and average extraction time for the fixture set. This local measurement does not estimate provider billing.
- **Token usage and estimated cost:** provider-reported input/output tokens are included when available. Set `EVAL_INPUT_USD_PER_MILLION_TOKENS` and `EVAL_OUTPUT_USD_PER_MILLION_TOKENS` to estimate cost for the selected model; keep these rates current because they are not fetched from providers.
- **Errors:** per-case extraction failures. The CLI exits non-zero when a provider fails on one or more cases.

Reports include provider and model, elapsed and average case time, field counts, accuracies, per-case expected and actual values, evidence grounding and support decisions, source-type breakdowns, and errors. Evaluation sends fixture text to external providers and may incur API charges.

This compact regression set is not a legal benchmark. A strong score does not establish production reliability. Add reviewed, organization-approved, de-identified examples before using scores for provider selection.
