# Spec: Approval API and CLI

- Issue: #66
- Parent: [agent state machine](agent-state-machine.md)
- ADRs: [ADR-0002](../adr/0002-modular-monolith.md),
  [ADR-0012](../adr/0012-worker-owned-agent-execution.md),
  [ADR-0018](../adr/0018-financial-safety-gate-decisions.md)
- Status: agreed (owner, 2026-11-07)

## Purpose

Let a POC operator review and record a decision on the exact proposal awaiting
human approval, without letting the API or CLI execute customer-facing actions.

## Scope and non-goals

Provide a local approval API and CLI, durable pending requests and decisions,
authenticated operator identity, audit, and a handoff back to policy
evaluation. This is synthetic-data-only POC functionality. Real identity,
production authorization, scheduling, coupon creation, dispatch, and the final
execution-time recheck belong to later work. The execution integration is #67.

## Decisions and reasons

- Use one named operator with a static environment token. A single operator
  keeps the POC simple while retaining an audit actor. It is not proof of real
  identity or a production authorization system. Tokens must not appear in
  command-line arguments, logs, audit records, or API responses.
- Use a local-only API and a CLI that calls that API. The API records decisions;
  it does not run the workflow, issue gate authorization, or call external
  side-effect ports. This preserves the worker-owned boundary in ADR-0012.
- Do not impose time-based expiry in the POC. There is no agreed lifetime or
  expiry workflow; a guessed duration could block operators or imply freshness
  that does not exist. A decision binds to the exact snapshot and gate
  fingerprint, and execution must revalidate current safety rules and context.
- Bind the approval to the complete displayed snapshot and its version as well
  as the existing `OfferProposal` fingerprint. The gate fingerprint covers the
  offer, but not every stored provenance or display field. Binding both avoids
  authorizing a different presentation while preserving the gate contract.
- After approval, return the run to `evaluating_policy`, not directly to
  `ready_to_execute`; a human cannot override the gate. Rejection is terminal.
  #66 tests the gate revalidation contract; #67 owns wiring and testing the
  actual immediate-before-execution checks and side effects.
- Keep #65's null-or-zero discount behavior. Synthetic approval-band and bulk
  fixtures exercise the approval path; the current proposal generator does not
  naturally generate an approval-required offer.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| AA-1 | Missing or invalid operator configuration fails closed; only an authenticated operator can list or decide requests, and credentials are not exposed. | `test_approval_authentication`, `test_approval_config_fails_closed`, `test_approval_token_not_exposed` |
| AA-2 | Only an eligible pending proposal can be decided, and the exact displayed snapshot and gate fingerprint must match. Changed, missing, stale or terminal proposals fail closed. | `test_changed_proposal_requires_fresh_policy_and_approval`, `test_stale_approval_rejected` |
| AA-3 | A decision, its actor, audit event and valid state transition are atomic and durable; competing decisions cannot both win. | `test_approval_is_atomic_and_durable`, `test_concurrent_approval_decisions` |
| AS-3 | Pending, approved or rejected decisions in this component never create a coupon or dispatch a message. | `test_pending_approval_has_no_external_side_effects` |
| AS-4 | Approval applies only to the exact proposal shown; a changed proposal cannot reuse it. | `test_changed_proposal_requires_fresh_policy_and_approval` |
| AS-5 | Human approval cannot waive a hard rejection, margin or usage cap; the gate contract is rechecked. The #67 execution path must enforce this immediately before side effects. | `test_execution_rechecks_safety_gate` |
| AA-4 | The CLI uses the authenticated API and reports conflicts or failures without reporting success. | `test_approval_cli` |

## Behavior

- Given a gate result requiring human approval, create a durable pending
  snapshot and transition the run to `awaiting_approval` atomically.
- Given valid operator credentials, list the pending proposals and their
  decision tokens. Approval requires the exact token and current fingerprint;
  record the decision and return to policy evaluation. Rejection records the
  decision and transitions to `rejected`.
- Given an invalid token, changed proposal, concurrent decision, missing run or
  terminal run, reject explicitly and do not write a successful decision.
- Given an approval, the gate must still reject hard-cap, margin or usage
  violations. The API does not claim that execution has occurred.

## Risks and open questions

Static tokens, loopback access and no time expiry are POC limitations, not
production controls. Before real operators or deployment, decide real identity,
authorization, token rotation, transport security and expiration. A current
cart or budget may change after approval; #67 must re-read and revalidate
before execution. Idempotent external actions and crash recovery also belong
to #67. The full AS-5 execution-path acceptance test remains open until then.
