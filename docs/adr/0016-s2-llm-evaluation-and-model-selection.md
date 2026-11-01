# ADR-0016: S2 LLM evaluation and model selection

- Status: proposed (model selection pending S2)
- Date: 2026-11-01

## Context

The retention POC needs an LLM for bilingual abandoned-cart tasks, structured output, and safe tool use. Documentation and price alone do not show which candidate handles these tasks. The agreed test contract is in [the S2 spec](../specs/s2-llm-golden-set-evaluation.md); current provider facts are in [F-004 to F-006](../facts-register.md).

## Decision

- Keep the provider-neutral `LLMClient` boundary in [ADR-0003](0003-llm-interface-and-model-selection.md).
- Compare `gemini-3.8-flash` at `low`, `gemini-3.5-flash-lite` at `minimal` ([F-027](../facts-register.md)), and `gpt-6-luna` at `none` and `low`; exclude Gemini 4 Argon from S2.
- Use the 132-input bilingual golden set. Run a small synchronous smoke first; full comparisons use Batch only after the owner reviews the seeded Odoo inventory and approves the smoke report.
- The smoke may create one fixed internal activity through the Odoo API only after a deterministic gate approves it. It is a test-only harness, not a production action path. Full-set tool results are frozen fixtures; CI mocks both LLM and Odoo clients.
- Enforce published-list cost caps of $5 for Gemini 3.8 Flash, $2 for Gemini 3.5 Flash-Lite, and $2 combined for both GPT-6 Luna configurations. Reports include actual token/cache usage and calculated published-list cost, not account credits or account billing.
- Do not select a model until the reviewed full comparison is complete. If it cannot be completed, record the selection as unresolved.

## Alternatives

- Select the cheapest candidate from published prices: rejected because cost does not establish safety, Arabic quality, or tool correctness.
- Choose a model from owner preference or a generic benchmark: rejected because neither measures the retention-engine task contract.
- Run the full comparison synchronously: rejected for the offline dataset because verified Batch APIs are available for all candidates and reduce published token rates; synchronous mode remains for the bounded smoke.

## Consequences

- No model is selected: no live provider evaluation or Odoo smoke has been run. Keep the selection unresolved until the reviewed reports exist; do not infer a winner from model documentation or fake-client tests.
- S2 must verify active project RPM/TPM and Batch queue limits before live calls. The Odoo 19 JSON-2 fields and write lifecycle remain unverified in [F-028](../facts-register.md); the read-only schema preflight is a guard, not integration evidence.
- The full-run input/output token limits remain unknown until the dataset and smoke measurements exist; set and approve them before the full Batch run without truncating prompts or outputs.
- A future accepted revision must record the measured results, limitations, and selected model/configuration—or explicitly retain an unresolved selection.
