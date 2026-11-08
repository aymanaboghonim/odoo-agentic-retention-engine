# Spec: Agent proposal and cost control

- Issue: #65
- ADRs: [ADR-0003](../adr/0003-llm-interface-and-model-selection.md),
  [ADR-0016](../adr/0016-s2-llm-evaluation-and-model-selection.md),
  [ADR-0018](../adr/0018-financial-safety-gate-decisions.md)
- Status: agreed (owner, 2026-11-06)

Issue #67 revises the discount restriction below: structured nonzero model
proposals are permitted, subject to the deterministic gate and required human
approval. See [gated execution](gated-execution.md). The revised prompt/schema
is versioned separately and remains unevaluated until #69.

## Purpose

Produce a bounded, auditable, bilingual proposal from synthetic cart and policy
context. A proposal is not an authorization to contact a customer or issue a
coupon.

## Scope and non-goals

Input validation, scoped context loading, a provider-neutral LLM port and POC
provider adapter, output validation, gate handoff, per-run usage and estimated
cost, and durable run transitions. Worker scheduling, approval, coupon creation
and dispatch belong to #66–#68. The full live evaluation is #69.

## Decisions and reasons

- Use the POC model from ADR-0016 behind the LLM port. Issue #67 replaces the
  prior S2-derived runtime prompt with a standalone structured-discount prompt.
  A new, explicitly
  versioned runtime response schema adds `proposed_discount_percent`, as
  ADR-0018 requires. Issue #67 permits finite percentages from zero to 100 as
  proposals, while the gate enforces its lower hard cap and other rules.
  The changed prompt/schema is **not** the evaluated S2 contract. #69 may need
  to revise it and evaluate the revised contract.
- Customer input and cart/policy text are untrusted data, separated from system
  instructions. Load context only for the already-bound customer and cart,
  never from a model-supplied ID. No model-selected write tool exists here.
- Require a synthetic-only deployment flag, allowlist model-visible fields,
  mask known customer identifiers and reject detected contact identifiers.
  These controls reduce exposure but do not prove arbitrary text synthetic or
  free of all PII. No real customer data is permitted in the POC.
- Require an explicit per-run USD ceiling and per-request token limits. Reserve
  conservative worst-case estimated cost before each generation request; use
  recorded, verified rate provenance and provider usage to reconcile it.
  Atomic reservation prevents concurrent attempts spending the same budget.
  Ambiguous failures and missing usage do not release an unresolved reservation
  for automatic reuse. Estimates are not provider bills or a guarantee against
  unknown charges or provider price changes.
- Store proposal version/fingerprint, usage, rate provenance and estimated cost
  with run state and audit. Logs contain structured status and reason codes,
  not credentials, prompts, model output or customer data. Unsupported claims
  are checked by the existing bounded gate matcher, not assumed detectable
  in every wording.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| AP-1 | Context is bound to the run's customer and cart; model-controlled IDs cannot expand access. | `test_proposal_rejects_cross_customer_context` |
| AP-2 | Only an allowlisted, masked payload is sent in explicit synthetic-only mode; detected contact identifiers fail closed. | `test_proposal_outbound_privacy` |
| AP-3 | Untrusted input cannot change system instructions, tool permissions, or deterministic gate rules. | `test_proposal_untrusted_input_is_data` |
| AP-4 | Malformed outputs and invalid claims cannot become executable proposals; proposed discounts are controlled by the gate. | `test_proposal_rejects_invalid_output`, `test_generated_discounts_reach_deterministic_gate` |
| AP-5 | Every candidate proposal is passed through the deterministic gate; no coupon or message is issued by this step. | `test_proposal_gate_handoff_has_no_side_effects` |
| AP-6 | Reservations across concurrent calls cannot exceed the configured per-run estimate ceiling; usage and errors are recorded. | `test_proposal_budget_concurrent_reservations`, `test_proposal_records_usage_and_failures`, `test_budget_blocks_count_and_generation` |
| AP-7 | A missing usage record or ambiguous provider failure cannot cause an automatic unsafe budget release or successful run. | `test_proposal_unknown_usage_fails_closed`, `test_ambiguous_generation_failure_keeps_reservation` |
| AP-8 | Proposal, audit, cost and legal run transitions survive restart; failed work is not marked completed. | `test_proposal_valid_with_fake_provider`, `test_proposal_missing_usage_and_provider_failure` |
| AP-9 | CI tests use a fake provider and do not call live providers. | `make ci`, `test_proposal_valid_with_fake_provider` |

## Behavior

- Given a valid synthetic trigger, validate and load only the matching cart and
  relevant policy passages, then reserve budget before invoking the provider.
- Given valid usage and a valid structured answer, record the proposal and
  estimated cost, then hand it to the gate. Advance only through allowed run
  transitions; leave approval and execution to later steps.
- A positive-discount draft gets a trusted coupon code and discount footer
  before its fingerprint and approval snapshot are stored. The code is not
  proof that an Odoo coupon exists; execution must confirm it before dispatch.
- The runtime uses a no-tool, preloaded-context prompt.
  The trusted synthetic trigger binds its run reference to `customer_id:cart_id`.
  The output remains at `evaluating_policy` after a successful gate evaluation;
  no-action, refusal and gate rejection are terminal `rejected` runs.
- Given a provider error, over-budget estimate, missing context, missing usage
  or invalid answer, fail or reject explicitly and preserve available usage.
- Given input attempting to override the prompt, obtain another customer's data
  or promise a discount, treat it as data; do not make a write or skip the gate.

## Risks

Provider usage can be missing after an actual charge; a held reservation is
conservative and needs operator reconciliation rather than an automatic retry.
Token-count-only endpoint billing is unknown (F-038) and is not included in the
estimated generation budget. Prices can change, and the bound assumes the
standard non-regional API tier and the documented token ceilings in F-005.
Pattern-based masking and discount checks are bounded. The evaluated S2 prompt
did not include this runtime schema and adapters, so its score is not evidence
that this implementation has the same quality or safety.
