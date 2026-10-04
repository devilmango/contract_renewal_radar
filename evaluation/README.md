# Extraction evaluation set

These twelve fixtures are fictional and use generic party placeholders such as `[CUSTOMER]`, `[PROVIDER]`, and `[VENDOR]`. They cover fixed and auto-renewing terms, calendar and business day notices, amendments, non-renewal, and ambiguous extensions without including customer, supplier, or personal contract data.

Each JSON file contains a stable `id`, a `contract_text` excerpt, and reviewed `expected` values for applicable fields. `notice_day_type` is scored when the fixture explicitly defines calendar or business days.

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
renewal-radar evaluate --provider rules --min-exact-accuracy 0.70 --min-field-accuracy 0.70
```

GitHub Actions runs this deterministic gate on pull requests and pushes to `main` or `master`. LLM evaluations remain opt-in because they call external APIs and may incur charges.

## Metrics

- **Field accuracy:** exact matches per expected field. A missing or invalid extraction does not count as correct.
- **Exact case match:** a case counts as correct only when every expected field matches.
- **Evidence grounding:** fraction of returned evidence quotes that occur verbatim in the source after case-folding and whitespace normalization. This checks quote traceability, not whether the quote logically proves the associated field.
- **Latency:** total and average extraction time for the fixture set. This local measurement does not estimate provider billing.
- **Token usage and estimated cost:** provider-reported input/output tokens are included when available. Set `EVAL_INPUT_USD_PER_MILLION_TOKENS` and `EVAL_OUTPUT_USD_PER_MILLION_TOKENS` to estimate cost for the selected model; keep these rates current because they are not fetched from providers.
- **Errors:** per-case extraction failures. The CLI exits non-zero when a provider fails on one or more cases.

Reports include provider and model, elapsed and average case time, field counts, accuracies, per-case expected and actual values, evidence grounding, and errors. Evaluation sends fixture text to external providers and may incur API charges.

This compact regression set is not a legal benchmark. A strong score does not establish production reliability. Add reviewed, organization-approved, de-identified examples before using scores for provider selection.
