# Extraction evaluation set

These six fixtures are fictional and use generic party placeholders such as `[CUSTOMER]`, `[PROVIDER]`, and `[VENDOR]`. They preserve the date and renewal language needed for field-level measurement without including customer, supplier, or personal contract data.

Each JSON file contains:

- `id`: stable case identifier.
- `contract_text`: redacted agreement excerpt provided to the selected extractor.
- `expected`: reviewed target values for start date, expiration date, renewal notice days, auto-renewal status, and termination notice.

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

Or include the rules baseline and every provider key configured in the environment:

```bash
renewal-radar evaluate --provider all --output evaluation/reports/all-providers.json
```

## Metrics

- **Field accuracy:** exact matches per expected field. A missing or invalid extraction does not count as correct.
- **Exact case match:** a case counts as correct only when every expected field matches.
- **Evidence grounding:** fraction of returned evidence quotes that occur verbatim in the source after case-folding and whitespace normalization. This checks quote traceability, not whether the quote logically proves the associated field.
- **Errors:** per-case extraction failures. The CLI exits non-zero when a provider fails on one or more cases.

The report includes the provider name, model, case count, field counts, accuracies, per-case expected and actual values, evidence grounding, and errors. Evaluation sends fixture text to external providers and may incur API charges.

This is a compact regression set, not a legal benchmark. A strong score does not establish production reliability. Add reviewed, organization-approved, de-identified examples that cover date formats, contract amendments, notice counting rules, ambiguous renewal clauses, and exception cases before using scores for provider selection.
