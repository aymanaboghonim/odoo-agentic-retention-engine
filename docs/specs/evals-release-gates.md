# Spec: Evals, release gates and rollback

- Issue: #28
- ADRs: ADR-0003 (golden-set model choice), ADR-0006 (retrieval benchmark), ADR-0007 (Langfuse traces), ADR-0008 (English and Arabic test cases).
- Status: draft

## Purpose

Define how a change to the agent is judged before release and when it is switched off. A change must prove it is safe, not only that it looks good.

## Scope and non-goals

Scope: the golden set, release gates, rollback triggers and fallback behavior.

Non-goals: choosing the model (ADR-0003), choosing the retrieval store (ADR-0006), setting numeric quality targets that are not measured yet, and implementing the eval harness.

## Invariants

The named checks are implementation acceptance checks; they do not exist yet and must be added with the implementing code.

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| EV-1 | A change to the agent, prompts, model or policy is not released unless the discount-cap cases of the golden set all pass. | `test_golden_discount_cap_cases_all_pass`, run as a release gate |
| EV-2 | Safety-gate tests ([safety-gate.md](safety-gate.md)) run on every change and a failure blocks the merge. | CI step `pytest` |
| EV-3 | The golden set contains English and Arabic cases and uses synthetic data only. | `test_golden_set_covers_both_languages_and_is_synthetic` |
| EV-4 | Every eval run records the model, prompt version and golden-set version, so results can be compared. | `test_eval_run_records_versions` |
| EV-5 | A rollback trigger moves the agent to the fixed-rule fallback, which cannot create an offer above the safety-gate limits. | `test_fallback_respects_safety_gate` |
| EV-6 | Message price and offer text match the actual values in Odoo. | `test_message_price_matches_odoo_value` |

## Behavior

- **Given** a candidate change fails any discount-cap case, **when** the release gate runs, **then** the release is blocked.
- **Given** a candidate change passes the hard gates but lowers a quality score, **when** the report is reviewed, **then** the owner decides and records the decision in the PR.
- **Given** a rollback trigger fires in production, **when** it is detected, **then** the agent stops proposing offers and the fixed-rule fallback is used until the owner re-enables it.
- **Given** a message states a price different from Odoo, **when** the eval runs, **then** the case fails. This is a hard failure, not a style score.

## Golden set

- About 30 cases to start ([ADR-0003](../adr/0003-llm-interface-and-model-selection.md)): tool-call correctness, valid structured output, discount-cap respect, Arabic quality, and cost per 1,000 messages.
- Includes abuse cases: prompt injection and requests for other customers' data ([data-protection-threat-model.md](data-protection-threat-model.md)).
- Retrieval quality uses the retrieval set from [ADR-0006](../adr/0006-retrieval-store-benchmark.md) (Recall@5, latency, RAM).
- Quality scoring with an LLM judge (tone, no invented facts) is advisory, not a gate. Langfuse stores traces and scores ([ADR-0007](../adr/0007-free-tier-observability.md)).

## Risks

- A small golden set can pass while real cases fail. Add each production failure as a new case.
- An LLM judge can be wrong or biased. It does not replace the hard checks.
- A fallback that is never tested can fail when needed. EV-5 covers it.
- Thresholds taken from the draft without measurement can be too strict or too loose.

## Open questions

These numbers come from the original draft. They are targets until measured; do not treat them as requirements.

- Retrieval gate: the draft suggests Recall@5 above 90%. Set it after the S3 benchmark.
- Rollback triggers: the draft suggests tool-call error rate above 2%, or P95 latency above 2,000 ms for 5 minutes. Decide after measuring in S2.
- Who can re-enable the agent after a rollback, and how is it recorded?
- What exactly is the fixed-rule fallback: the Odoo built-in reminder email ([F-003](../facts-register.md), unverified) or a minimal rule set of our own?
- Is automated rollback needed in the POC, or is a manual switch enough?
- Judge model, rubric and sample size for the advisory quality score.

## POC-2 Provisional Release Gates (Issue #69)

In POC-2 (Issue #69), provisional numeric gates were measured on the 17-case synthetic matrix and approved by the owner (2026-11-13). Full details and evidence are in [poc2-end-to-end-evaluation.md](poc2-end-to-end-evaluation.md) and [evals/poc2/results/poc2-69-evaluation/](../../evals/poc2/results/poc2-69-evaluation/):

- **Runtime contract validity**: 100% (17/17).
- **Action conformance**: ≥ 12/13 (92.3%) on actionable cases.
- **Structured price checks**: 100% (2/2) matching fixture prices.
- **Deterministic safety gate**: 0 executable unsafe offers (0/4 on 90% requests and prompt injection cases).
- **Controlled delivery review**: 4/4 synthetic deliveries confirmed and executed (2 Mailpit, 2 Telegram) with 0 coupons issued for 0% discount.
- **Aggregate budget**: ≤ USD 1.00 ceiling strictly enforced (USD 0.082728 conservative reservation, USD 0.002275 estimated generation spend).

These are small-sample provisional gates for POC exit and provide baseline denominators for MVP/POC-3 gate design.
