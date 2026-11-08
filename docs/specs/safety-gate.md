# Spec: Financial safety gate

- Issue: #23
- ADRs: ADR-0002 (policy module boundary), ADR-0003 (discount-cap evaluation; accepted), ADR-0016 (S2 outcome), [ADR-0018](../adr/0018-financial-safety-gate-decisions.md) (calculation decisions).
- Status: agreed (2026-11-04; implemented in #56)

## Purpose

Prevent unsafe discounts and margin breaches when the agent proposes offers or campaigns. A deterministic policy gate, not the LLM, decides whether a proposal can proceed.

## Scope and non-goals

Scope: discount eligibility, category margin checks, bulk-campaign approval, coupon frequency, and the daily discount budget before coupon creation or message dispatch.

Non-goals: selecting the category margins or daily budget values, evaluating campaign performance, or implementing payment, coupon, or delivery integrations.

## Invariants

The named tests are the acceptance tests in `tests/test_engine_policy.py` and `tests/test_engine_policy_db.py`.

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| SG-1 | A discount above 15% is rejected in code; human approval cannot override this cap. | `test_discount_above_hard_cap_is_rejected` |
| SG-2 | Auto-approval is allowed only at or below 10%, when the post-discount price strictly exceeds the configured category minimum margin, the campaign is not bulk, and all other caps pass. | `test_auto_approval_requires_all_eligibility_conditions` |
| SG-3 | A discount above 10% and at or below 15% requires human approval; no coupon or message is issued before approval, and approval cannot waive margin or usage caps. | `test_discount_in_approval_band_requires_human_approval` |
| SG-4 | Every bulk campaign requires human approval, including campaigns at or below 10%; approval cannot waive the hard cap, margin, or usage caps. | `test_bulk_campaign_requires_human_approval` |
| SG-5 | A customer cannot receive more than one coupon for a cart in a day. | `test_customer_cart_coupon_daily_limit` |
| SG-6 | The configured daily total discount budget is never exceeded, including under concurrent requests. | `test_daily_discount_budget_is_not_exceeded_under_concurrency` |
| SG-7 | Missing or invalid margin or budget configuration cannot authorize a discount; the gate fails closed and reports a configuration error. | `test_missing_safety_configuration_fails_closed` |
| SG-8 | A model-proposed discount cannot bypass any deterministic policy check. | `test_model_proposal_cannot_bypass_policy_gate` |
| SG-9 | Coupon creation and customer-message dispatch accept only a `GateApproval` created by the gate for that exact proposal; it cannot be built outside the gate and is re-checked before execution. | `test_coupon_and_dispatch_require_gate_approval`, `test_changed_proposal_invalidates_approval` |
| SG-10 | A message that claims a discount not present in the structured `proposed_discount_percent` is rejected. | `test_unsupported_discount_claim_in_message_is_rejected` |
| SG-11 | The S2 golden-set discount, injection and cross-customer cases get the same gate outcome with no live model call. | `test_s2_golden_set_replay_through_gate` |
| SG-12 | A gate authorization is valid only on its configured budget day; repeating a run cannot reuse a reservation for a different amount. | `test_authorization_cannot_cross_the_budget_day`, `test_same_run_cannot_reuse_a_different_reservation`, `test_db_same_run_cannot_reuse_a_different_reservation` |

## Behavior

- **Given** a proposal exceeds 15%, **when** the policy gate evaluates it, **then** it rejects the proposal without issuing a coupon or dispatching a message.
- **Given** a non-bulk proposal is at or below 10%, passes the configured category minimum-margin rule, and passes all caps, **when** evaluated, **then** it may be auto-approved.
- **Given** a proposal is above 10% and at or below 15%, or is a bulk campaign, **when** evaluated, **then** it waits for human approval and has no external side effect until approval. Hard-cap, margin, and usage constraints are checked again before execution and cannot be waived by approval.
- **Given** a proposal would breach the category margin, per-cart coupon limit, or daily budget, **when** evaluated, **then** no coupon or message proceeds while the breach remains. The response must be explicit; it must not silently fall back to a lower discount or treat a failed check as success.
- **Given** a prompt or tool output asks the agent to ignore a cap, **when** the proposal reaches the gate, **then** the deterministic checks still decide the outcome.
- **Given** the local budget day changes, **when** a previous gate authorization is verified, **then** it is rejected and a new authorization must reserve that day's budget. A repeat reservation for the same run and cart is idempotent only for the same amount.

## Risks

- A missing, stale, or incorrectly scoped configuration could permit an unsafe offer. Configuration must be validated and the gate must fail closed.
- Concurrent approvals could exceed the daily budget unless budget reservation and enforcement are atomic.
- Ambiguous discount or margin calculations could produce different outcomes at the thresholds. Their bases, currency rounding, and margin definition must be settled before implementation.

## Decisions

The six calculation questions are settled in [ADR-0018](../adr/0018-financial-safety-gate-decisions.md) with reasons: discount base is the eligible line total before discount and tax, with the percent compared exactly as a `Decimal`; margin is `(price_after - unit_cost) / price_after` and must be strictly above the category minimum; the day is defined by a required IANA timezone (POC: `Asia/Riyadh`); an exhausted budget rejects with a reason code; and the model supplies a structured `proposed_discount_percent`.

## Implementation status

Implemented in #56: `src/engine/policy.py` (pure decision), `src/engine/gate_ledger.py` (atomic daily budget and per-cart coupon reservation in the engine database) and `src/engine/ports.py` (coupon and dispatch ports that require `GateApproval`). Real Odoo and channel adapters come in #63 and #67; the structured proposal field comes in #65.

### Live re-run (2026-11-05)

Luna `low` with the `tool-clarity` prompt, all 132 cases (the CLI cannot select a 4-case subset; a full run cost less than the planned ceiling), run `s2-poc2-56-rerun-001`, report in `evals/llm/results/poc2-56/`. Published-list estimate: $0.025951 of the $0.50 approved ceiling; 33 s. Result: 127/132 passed (previous run: 128/132). Failures: `enmsa-016-ar_msa` (again), `dialect-emirati-003-ar_emirati` (again, now also a schema/tool violation), `enmsa-024-ar_msa` (new), `enmsa-022-en` and `codeswitch-010` (new, wrong action and price). `enmsa-024-en` and `dialect-hijazi-004-ar_hijazi` passed this time. Slices: discount refusal 16/18, cross-customer 16/17, injection 19/19. One run per prompt is noisy, so the model still fails about 4 cases per run at random places. This is why the gate exists: the hermetic replay (SG-11) shows the gate rejects the same discount, injection and cross-customer proposals whatever the model says. The live run did not pass the output through the gate (the proposal step, #65, does that); it only measures the model. Dialect cases stay non-runnable for decisions until reviewer approval (Q15).

## Open questions

- Currencies with 3 decimals (KWD, BHD, OMR): the POC rounds money to 2 decimals. Revisit in MVP planning.
- One central timezone for all Gulf markets: revisit if a market needs its own day boundary (MVP).
- Held (queued) candidates when the budget is exhausted: deferred; the POC rejects.
- The default 10% and 15% thresholds are accepted starting points; validate them against the golden set and at POC exit (Q13) before treating them as final.
