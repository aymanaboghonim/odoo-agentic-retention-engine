# Spec: Financial safety gate

- Issue: #23
- ADRs: ADR-0002 (policy module boundary), ADR-0003 (discount-cap evaluation; accepted), ADR-0016 (S2 outcome).
- Status: draft

## Purpose

Prevent unsafe discounts and margin breaches when the agent proposes offers or campaigns. A deterministic policy gate, not the LLM, decides whether a proposal can proceed.

## Scope and non-goals

Scope: discount eligibility, category margin checks, bulk-campaign approval, coupon frequency, and the daily discount budget before coupon creation or message dispatch.

Non-goals: selecting the category margins or daily budget values, evaluating campaign performance, or implementing payment, coupon, or delivery integrations.

## Invariants

The named tests are implementation acceptance tests; they do not exist yet and must be added with the implementing code.

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

## Behavior

- **Given** a proposal exceeds 15%, **when** the policy gate evaluates it, **then** it rejects the proposal without issuing a coupon or dispatching a message.
- **Given** a non-bulk proposal is at or below 10%, passes the configured category minimum-margin rule, and passes all caps, **when** evaluated, **then** it may be auto-approved.
- **Given** a proposal is above 10% and at or below 15%, or is a bulk campaign, **when** evaluated, **then** it waits for human approval and has no external side effect until approval. Hard-cap, margin, and usage constraints are checked again before execution and cannot be waived by approval.
- **Given** a proposal would breach the category margin, per-cart coupon limit, or daily budget, **when** evaluated, **then** no coupon or message proceeds while the breach remains. The response must be explicit; it must not silently fall back to a lower discount or treat a failed check as success.
- **Given** a prompt or tool output asks the agent to ignore a cap, **when** the proposal reaches the gate, **then** the deterministic checks still decide the outcome.

## Risks

- A missing, stale, or incorrectly scoped configuration could permit an unsafe offer. Configuration must be validated and the gate must fail closed.
- Concurrent approvals could exceed the daily budget unless budget reservation and enforcement are atomic.
- Ambiguous discount or margin calculations could produce different outcomes at the thresholds. Their bases, currency rounding, and margin definition must be settled before implementation.

## Implementation status

Not implemented. S2 (#46) covers only a bounded discount text matcher in the evaluator and a failed-score block on the smoke-only internal activity write; it is not this gate. The production gate, coupon and message-dispatch enforcement, and the tests named above are tracked in #56 (POC-2).

## Open questions

- What amount is the percentage discount calculated against (for example, eligible line total or cart subtotal)?
- When the daily budget is exhausted, should the candidate be rejected or held for a later budget window? It must not be allowed to exceed the cap.
- What exact margin measure and calculation determine whether a post-discount price meets a category minimum?
- What timezone defines a day for the per-customer/cart coupon limit and daily budget?
- What currency rounding rule applies at the 10% and 15% boundaries?
- The default 10% and 15% thresholds are accepted starting points; validate them in S1 and against the golden set before treating them as final.
