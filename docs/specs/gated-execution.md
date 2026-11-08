# Spec: Gated execution and synthetic channel review

- Issue: #67
- Parents: [agent state machine](agent-state-machine.md),
  [financial gate](safety-gate.md), [approval API](approval-api.md)
- ADRs: [ADR-0012](../adr/0012-worker-owned-agent-execution.md),
  [ADR-0018](../adr/0018-financial-safety-gate-decisions.md)
- Status: implemented (owner-reviewed synthetic channel examples, 2026-11-08)

## Purpose and scope

Execute a synthetic, approved abandoned-cart proposal through a worker-owned
path, with current-context checks, durable effect tracking and explicit recovery.
Create a coupon through the supported Odoo API when the offer has a positive
discount, then dispatch to one dedicated Telegram test chat or local Mailpit.
The owner reviews English and MSA Arabic examples from both fixed and live-model
proposals on each channel. Scheduling, production identities and full quality
evaluation remain in #68 and #69.

## Decisions and reasons

- Permit a model to propose a structured nonzero percentage; the deterministic
  gate, budget and human approval decide whether it may be used. This replaces
  #65's null-or-zero restriction and needs a new runtime prompt/schema version.
  S2 scores do not validate the new version; #69 must evaluate it.
- Require a durable worker claim and journal each coupon and message effect
  before attempting it. A known-not-sent error may be retried under configured
  bounds; an unknown outcome is held for operator reconciliation, not blindly
  resent. A local key alone cannot deduplicate a request already accepted by an
  external service after its response is lost.
- Reconstruct the approved proposal and check the whole approval snapshot;
  reload the cart and check ownership, line details, category, price, cost,
  currency and eligibility immediately before authorization and effects.
  Reject changed or unavailable context rather than relying on stale approval.
  Apply gate rules and atomic budget reservation; `Gate.verify` alone does not
  recheck ledger usage.
- Current cart data does not establish contact consent. For this synthetic POC,
  require a trusted, explicitly confirmed dedicated test destination per run;
  do not infer real customer consent from an email address or retrieved policy.
  No real or shared Odoo instance, real customer, or uncontrolled destination
  is permitted. Production consent and contact-frequency enforcement are not
  claimed by this synthetic test.
- Never create a coupon for zero/null discount. For a positive discount,
  dispatch only after the coupon's existence and terms are confirmed. The
  approved text must not be changed after approval without binding the final
  rendered text and coupon representation to a fresh approval.
- The owner chose to include the coupon code in the final customer message
  before review. Trusted code derives the code from a stable run identity and
  renders it before the proposal is saved and approved; it is not supplied by
  the model. Deriving it from the message fingerprint would be circular. A
  displayed code is a proposed coupon, not proof that Odoo has created it.
- The owner approved up to USD 0.50 total estimated model generation cost for
  this issue's live review, across all runs. Reserve the aggregate cost before
  every request; retain uncertain usage. Count-only billing remains unknown
  (F-038) and is excluded. No provider calls occur in CI.
- For the isolated synthetic review only, the owner approved a USD 450 daily
  discount budget and a 0.20 minimum margin for `Furniture / Office`
  (2026-11-08). This covers four $750 synthetic carts with $500 unit cost at
  the 15% hard cap; it is not a production default. The separate USD 0.50
  model-generation ceiling still applies.
- Bind the selected `telegram` or `mailpit` channel into the proposal and
  approval snapshot. The operator cannot switch channels at execution time;
  each dispatch requires a run-specific terminal confirmation and separate
  confirmation of the dedicated synthetic destination.
- Bind the selected English or MSA language into the proposal and approval
  snapshot. Mailpit subjects use the same language as the approved message.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| EX-1 | No worker calls an effect before validating the exact approval, current cart, destination and gate. | `test_missing_human_approval_has_no_side_effect`, `test_changed_cart_is_rejected_before_claim_or_effect` |
| EX-2 | A model's structured discount cannot override a cap, margin, usage constraint or human approval. | `test_generated_discounts_reach_deterministic_gate` |
| EX-3 | One worker owns a run and repeated handling cannot duplicate a confirmed or uncertain effect. | `test_repeated_execution_does_not_repeat_completed_effects`, `test_concurrent_execution_claim` |
| EX-4 | A crash, timeout or unknown external outcome remains explicitly unresolved and is never automatically resent. | `test_ambiguous_dispatch_is_held_without_automatic_retry`, `test_unresolved_or_confirmed_effect_is_not_retried` |
| EX-5 | A discount message is backed by the exact approved offer and confirmed coupon; no coupon is created for zero/null discount. | `test_approved_discount_creates_matching_coupon_before_dispatch`, `test_zero_discount_sends_without_coupon_and_completes` |
| EX-6 | Only explicitly confirmed synthetic test destinations and the approved channel are used; no model-supplied recipient is trusted. | `test_unconfirmed_or_uncontrolled_destination_is_rejected`, `test_wrong_confirmed_channel_is_rejected_before_claim` |
| EX-7 | Partial failure is durable, visible and never reported completed. | `test_change_after_coupon_holds_before_dispatch`, `test_coupon_mismatch_never_dispatches` |
| EX-8 | The aggregate live-model estimate cannot exceed the owner ceiling, including concurrent or uncertain requests. | `test_live_review_aggregate_budget` |
| EX-9 | CI is hermetic; live bilingual Telegram and Mailpit review is separately authorized and owner-confirmed. | `make ci`, `test_review_cli_has_separate_operator_steps` |
| EX-10 | The approved message language is bound to the proposal; Mailpit's subject uses that language and is shown before dispatch confirmation. | `test_channel_dispatcher_uses_confirmed_target_and_localized_subject`, `test_review_preview_shows_localized_mailpit_subject_before_message`, proposal fingerprint |

## Behavior

- Given a matching, eligible run, claim it once, verify the current cart and
  approval, reserve budget, then execute only its configured channel.
- Given a rejected, changed, expired-in-fact, unauthorized or unsafe offer,
  fail closed without an external write. Human approval cannot override a gate.
- Given a confirmed coupon and valid message, send once; record provider
  response and completion. Given an unknown result or a worker crash during an
  external operation, stop and expose an unresolved state for reconciliation.
- Given an operator-confirmed test destination, the live review runs fixed and
  model-generated English/MSA examples on both channels. The operator verifies
  the received content; a provider response alone is not that review.
- Given an approved MSA Mailpit proposal, the subject and body are both MSA
  Arabic; English proposals use an English subject. The terminal preview shows
  the subject before asking for dispatch confirmation.

## Risks and open questions

Odoo coupon creation involves multiple API calls, so a partially created
program or lost response needs reconciliation; deterministic codes alone do
not prove exactly-once creation. Telegram and SMTP do not provide a shared
transaction with the engine DB. This spec promises no automatic duplicate on
uncertainty, not automatic recovery or guaranteed delivery. Consent evidence,
production authorization, policy timing and marketing opt-outs require their
own verified sources before production. Manual live review needs a confirmed
test chat, isolated services and the owner's actual inspection.

## Implementation progress

The worker revalidates the approval, proposal, channel, current cart and
financial gate before effects. It binds a single worker claim, records each
effect before an attempt, holds unknown outcomes, and retries only a known
not-sent error within the configured limit. End-to-end and journal checks are
listed in the invariant table.

The proposal budget can also associate calls from multiple runs with one
immutable review ceiling. It conservatively retains the full reservation even
when reported usage is lower, and keeps unknown usage reserved. The operator
CLI fixes the shared review ID and USD 0.50 limit for every live model call.
The interactive runbook binds each proposal to its chosen channel and language,
and prints the approved message, Mailpit subject and destination before
dispatch. The owner received and reviewed fixed and model-generated English
and MSA examples on both Mailpit and Telegram. The four model calls cost an
estimated USD 0.018384 at published list rates; this was not a retrieval-quality
or rate-limit evaluation, and the active OpenAI project tier remains unknown
(F-046).
