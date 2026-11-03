# ADR-0016: S2 LLM evaluation and model selection

- Status: proposed (full run completed with deterministic failures; no model selected pending owner review)
- Date: 2026-11-03

## Context

The retention POC needs an LLM for bilingual abandoned-cart tasks, structured output, and safe tool use. The agreed test contract is in [the S2 spec](../specs/s2-llm-golden-set-evaluation.md); volatile provider facts and their checked limits are in [F-004 to F-006 and F-037](../facts-register.md). A prior Batch run is provisional: Gemini responses did not conform to the expected output keys, while Luna returned some deterministic failures. Offline inspection found that the old prompt did not state the exact output keys and the Batch responses did not enforce the schema; these are possible contributors, not a proven single cause.

## Decision

- Keep the provider-neutral `LLMClient` boundary in [ADR-0003](0003-llm-interface-and-model-selection.md).
- Compare `gemini-3.8-flash` at `low`, `gemini-3.5-flash-lite` at `minimal` ([F-027](../facts-register.md)), and `gpt-6-luna` at `none` and `low`; exclude Gemini 4 Argon from S2.
- Use the owner-approved 132-input bilingual golden set with a versioned, provider-specific prompt. Keep exact output keys, tool-use policy, and deterministic money/safety constraints explicit in code and tests.
- Run a prompt-versioned synchronous smoke, stop for owner review, validate one hard case per candidate, then run all cases synchronously with a maximum of 16 concurrent cases. Full-set tool results are frozen synthetic fixtures. A cart-tool case makes no more than two synchronous provider requests.
- Use bounded retries only for HTTP 429 and 5xx errors. Treat remaining provider errors as an incomplete run, return nonzero, and publish no partial candidate set. The cost guard reserves worst-case spend for in-flight cases and enforces the minimum of the approved ceiling, candidate cap, and remaining shared candidate-group cap.
- The smoke may create one fixed internal activity through the Odoo API only after a deterministic gate approves it. This is a test-only harness, not a production action path. CI mocks both LLM and Odoo clients and makes no live calls.
- Enforce published-list cost caps of $5 for Gemini 3.8 Flash, $2.428160 for Gemini 3.5 Flash-Lite, and $2 combined for both GPT-6 Luna configurations. The owner-approved per-request ceilings are 4,000 input and 8,192 output tokens. Reports calculate published-list estimates from provider usage, not account credits or billing.
- The full comparison `s2-full-001` completed all 132 cases for each candidate. The CLI returned nonzero because deterministic case failures were found, so the runbook explicitly says these results are not a model-selection decision. Pass counts were Gemini 3.8 Flash 91/132, Flash-Lite 70/132, Luna `none` 106/132, and Luna `low` 111/132. All candidates passed the bounded discount/injection text checks 37/37, but each failed other deterministic checks, including cross-customer and money-boundary cases. No model is selected; the next remediation and numeric comparative thresholds remain for owner review.
- Candidate reports: [Gemini 3.8 Flash](../../evals/llm/results/full/s2-full-001/gemini-3.8-flash-low.md), [Gemini Flash-Lite](../../evals/llm/results/full/s2-full-001/gemini-3.5-flash-lite-minimal.md), [Luna none](../../evals/llm/results/full/s2-full-001/gpt-6-luna-none.md), and [Luna low](../../evals/llm/results/full/s2-full-001/gpt-6-luna-low.md). The combined published-list usage estimate is $0.384558, not account billing; count-only endpoint billing/quota is unknown and excluded.

## Alternatives

- Keep the Batch path: rejected for S2 because the provisional run failed structured-output behavior on Gemini and incurred long queue delay. Its results are not decision-grade.
- Use Gemini Interactions API structured output and tool calling in the same request: dropped after the Phase A diagnostic returned no final text on every tested call; it was unnecessary for the supported two-request flow.
- Select the cheapest candidate from published prices: rejected because cost does not establish safety, Arabic quality, or tool correctness.
- Choose from owner preference or a generic benchmark: rejected because neither measures the retention-engine task contract.

## Consequences

- No model is selected. The full run is complete but cannot support selection because deterministic failures remain; do not treat relative pass counts as a winner ranking.
- The Batch RCA does not isolate a single cause; schema enforcement in Batch and incomplete prompt instructions are both possible contributors.
- The updated-prompt smoke passed, and the owner approved its exact JSON hash before the full run. Subsequent report-format corrections cleared the local approval hash; any future live rerun requires a fresh smoke review and approval. Active provider tiers/quotas were reconfirmed by the owner on 2026-11-03 but remain unverified account-specific claims. Numeric comparative thresholds and the remediation path remain open in [STATUS](../STATUS.md).
- S2 must use synthetic data only. It never writes directly to the Odoo database; smoke state changes use the supported Odoo API.
- The eventual accepted decision must record reviewed score slices, provider configuration, usage, latency, cost estimate, limitations, and the selected configuration—or explicitly leave selection unresolved.
