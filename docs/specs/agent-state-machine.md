# Spec: Agent state machine and human approval

- Issue: #24
- ADRs: ADR-0002 (modular engine with API and worker), ADR-0004 (scheduled batch work), ADR-0013 (POC channels).
- Status: draft

## Purpose

Define a persistent, auditable lifecycle for agent runs that process abandoned carts and campaign requests. Keep policy decisions deterministic and require a human decision before any action that is not eligible for automatic execution.

## Scope and non-goals

Scope: run states, allowed transitions, human approval and rejection, revalidation before execution, and observable terminal outcomes.

Non-goals: selecting model prompts, defining retry limits or approval expiry, specifying channel payloads, or implementing the engine. Discount and margin policy is specified in [safety-gate.md](safety-gate.md).

## Invariants

The named tests are implementation acceptance tests; they do not exist yet and must be added with the implementing code.

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| AS-1 | A run moves only through the transitions defined below; terminal states cannot transition back to active states. | `test_run_rejects_invalid_state_transitions` |
| AS-2 | Each run has a durable state and audit record sufficient to identify its trigger, transition history, policy result, and final outcome. | `test_run_state_and_audit_survive_restart` |
| AS-3 | A customer-facing message or coupon is not issued before the run reaches `ready_to_execute`; a run awaiting approval cannot cause an external side effect. | `test_pending_approval_has_no_external_side_effects` |
| AS-4 | An approval applies only to the exact proposal version presented to the approver; any change requires fresh policy evaluation and, where required, approval. | `test_changed_proposal_requires_fresh_policy_and_approval` |
| AS-5 | Immediately before execution, all safety-gate checks are re-evaluated; human approval cannot override a hard rejection or a failed margin or usage cap. | `test_execution_rechecks_safety_gate` |
| AS-6 | Repeated handling of a run or decision cannot create duplicate coupons or dispatches for that run. | `test_repeated_trigger_does_not_duplicate_side_effects` |
| AS-7 | A failed or interrupted run is not reported as completed; its failure is recorded and exposed to the caller or operator. | `test_failed_run_has_explicit_failure_outcome` |

## Behavior

### States and transitions

| State | Meaning | Allowed next state(s) |
| :-- | :-- | :-- |
| `received` | Trigger accepted and run identity recorded. | `validating_input`, `cancelled`, `failed` |
| `validating_input` | Input is checked and sanitized before use. | `loading_context`, `rejected`, `failed` |
| `loading_context` | Required cart, customer, product, and feature context is fetched. | `generating_proposal`, `failed` |
| `generating_proposal` | Agent proposes an offer and/or message content. | `evaluating_policy`, `failed` |
| `evaluating_policy` | Deterministic safety gate evaluates the proposal. | `awaiting_approval`, `ready_to_execute`, `rejected`, `failed` |
| `awaiting_approval` | A specific proposal is held for an authorized human decision. | `evaluating_policy` after approval; `rejected` after rejection; `cancelled`; `failed` |
| `ready_to_execute` | Proposal passed policy, including required approval and final revalidation. | `executing`, `rejected`, `failed`, `cancelled` |
| `executing` | Coupon creation and/or channel dispatch is in progress. | `completed`, `failed` |
| `completed` | All requested side effects completed successfully. | Terminal |
| `rejected` | Input or proposal cannot proceed under policy, or an approver rejected it. | Terminal |
| `cancelled` | Run was cancelled before execution. | Terminal |
| `failed` | An operational or configuration error prevented safe completion. | Terminal |

### Decision behavior

- **Given** a valid trigger, **when** accepted, **then** the system creates one durable run and begins input validation.
- **Given** required context or configuration cannot be retrieved or validated, **when** the run reaches that step, **then** it fails explicitly; it must not invent missing values or continue as if the check passed.
- **Given** the safety gate returns auto-eligible, **when** final validation succeeds, **then** the run proceeds to `ready_to_execute`.
- **Given** the safety gate requires human approval, **when** the proposal is presented, **then** the run remains in `awaiting_approval` without creating a coupon or dispatching a message.
- **Given** an authorized approver approves the exact pending proposal, **when** the decision is recorded, **then** the proposal returns through policy evaluation; if unchanged and still safe, the recorded approval satisfies the human-approval requirement and the run proceeds to `ready_to_execute`.
- **Given** an approver rejects the proposal, **when** the decision is recorded, **then** the run becomes `rejected` and no side effect is issued.
- **Given** the proposal changes after approval, **when** execution is attempted, **then** the old approval is not reused; the changed proposal returns to policy evaluation.
- **Given** an untrusted message or tool output requests bypassing approval or policy, **when** the run is evaluated, **then** only the defined transition and deterministic policy rules govern its next state.
- **Given** a run has completed or reached another terminal state, **when** a duplicate trigger or stale approval arrives, **then** it cannot restart the run or repeat a side effect.

## Risks

- A crash between an external side effect and recording its result can make a retry duplicate an action. Execution needs idempotency keys or an equivalent deduplication mechanism.
- An approval attached to mutable proposal data can authorize an unintended action. Store an immutable proposal version or fingerprint with the decision.
- Approval identity and permission checks are necessary to prevent unauthorized decisions; they must be defined before enabling real operators.

## Open questions

- Who may approve, and how is approver identity and authorization verified?
- How long does an approval remain valid, and what happens when it expires?
- What retry policy applies to transient context-fetch, coupon-creation, and channel-dispatch failures?
- How is exactly-once or effectively-once behavior provided across an external side effect and local state persistence?
- Can a run be cancelled after execution has started, and how is partial completion presented?
- Which trigger types are in the first POC slice, and what stable key deduplicates each trigger?
