# Spec: Automatic execution reconciliation for held effects

- Issue: #125 (spec); implemented in #131 (evidence and API) and #132 (automatic and admin resolution)
- ADRs: [ADR-0030](../adr/0030-execution-reconciliation-without-resend.md), [ADR-0012](../adr/0012-worker-owned-agent-execution.md), [ADR-0027](../adr/0027-operator-container-in-core-task.md)
- Parent: [gated execution](gated-execution.md) (`EX-4`), [operator RBAC](operator-rbac-and-expiring-approvals.md)
- Status: draft

## Purpose

Runs held with an `unknown` (or crash-left `started`) coupon or message effect close by themselves when Odoo or Mailpit gives definitive evidence, and after a deadline otherwise, without ever sending twice. An admin can decide sooner through the API; the Tier 3 operator UI lists held runs so nobody needs a CLI.

## Scope and non-goals

Scope: a stored deterministic email Message-ID; the evidence lookups; a worker reconcile stage; `GET` held runs (`viewer`) and `POST` resolve (`admin`); a CLI fallback in the operator container; telemetry for the held count and resolutions; orphan-coupon deactivation; the runbook.

Non-goals: resending or re-queueing any effect; reading Telegram back (no Bot API call exists); Odoo-side partner cleanup; the UI itself (Tier 3, see STATUS).

## Definitions

- **Held effect:** an `execution_effects` row with status `unknown`, or `started` older than `RECONCILE_STALE_STARTED_MINUTES`, whose run is `executing`.
- **Evidence:** `present`, `absent`, `unknown`, or `attestation required`, from one lookup.

| Effect | Lookup | Key |
| :-- | :-- | :-- |
| coupon | Odoo JSON-2 `loyalty.card` `search_read` | `coupon_code_for_run(run_id)` |
| message, `mailpit` | Mailpit search `message-id:` (`F-082`) | stored Message-ID |
| message, `telegram` | none | admin attestation |

Settings (all required, no default, fail closed): `RECONCILE_INTERVAL_MINUTES`, `RECONCILE_STALE_STARTED_MINUTES`, `RECONCILE_DEADLINE_HOURS` (integers `>= 1`).

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| REC-1 | The evidence lookup is read-only: no Odoo write, no dispatch, no engine DB change. | `test_reconcile_check_is_read_only`, boundary rule in `scripts/check_import_boundaries.py` |
| REC-2 | The email Message-ID is deterministic from (run, claim, attempt) and stored before the send. | `test_message_id_is_recorded_before_dispatch` |
| REC-3 | A lookup error gives `unknown`, never `absent`. | `test_lookup_errors_are_unknown_not_absent` |
| REC-4 | The worker resolves automatically only on definitive evidence (`present` or `absent`); otherwise it waits until the deadline and then closes the run as `failed` (`reconciled_unverified`). | `test_auto_resolve_only_on_definitive_evidence_or_deadline` |
| REC-5 | No code path resends or re-queues a resolved effect. | `test_resolved_run_never_redispatches` |
| REC-6 | An admin resolution matches the evidence: `confirmed` needs `present` or an attestation; `not_sent` needs `absent` or an attestation. | `test_resolution_requires_matching_evidence` |
| REC-7 | `POST` resolve requires `admin` and a reason, only records the decision (the API changes no run state and calls no Odoo or channel), and is audited with the actor. | `test_resolve_api_requires_admin_and_only_records` |
| REC-8 | Resolution is idempotent and safe against a running worker: it locks the run and acts only on `executing` runs with held effects. | `test_concurrent_resolve_is_safe` |
| REC-9 | A resolved run is terminal (`completed`, or `failed` with a `reconciled_*` reason); the retention sweep and erasure then apply. | `test_resolved_run_is_terminal_and_sweepable` |
| REC-10 | The worker deactivates a confirmed coupon only when the message is proven `not_sent`; with `reconciled_unverified` the coupon stays active. | `test_orphan_coupon_deactivated_only_when_message_not_sent` |
| REC-11 | `GET` held runs (`viewer`) returns tokenized data only (no name, email, phone, or destination). | `test_held_runs_api_returns_no_pii` |
| REC-12 | Each resolution writes an audit event (actor `reconciler` or the admin) and updates the held-count and resolution metrics. | `test_resolution_is_audited_and_counted` |

## Behavior

- **Given** a coupon effect `unknown` and the card present in Odoo, **when** the reconcile stage runs, **then** the effect becomes `confirmed`; the run stays held until its message effect is resolved too.
- **Given** a Mailpit message effect `unknown` with no message for its Message-ID, **when** the stage runs, **then** the run becomes `failed` (`reconciled_message_not_sent`) and the worker deactivates the confirmed coupon (REC-10).
- **Given** a Telegram message effect `unknown`, **when** no admin attests before `RECONCILE_DEADLINE_HOURS`, **then** the run becomes `failed` (`reconciled_unverified`) and the coupon stays active.
- **Failure:** Odoo times out during a lookup: the evidence is `unknown`, nothing is resolved, and the next stage tries again (REC-3, REC-4).
- **Abuse:** an `approver` or `viewer` calls `POST` resolve: refused and audited (REC-7). Resolving the same effect twice is a no-op (REC-8).

## Risks

- A wrong attestation closes a run incorrectly. Only `admin` can attest, the audit shows it, and no resend follows, so the worst case is a missing offer, not a duplicate.
- Runs held before #131 have a random Message-ID that was not stored, so their Mailpit evidence is `unknown` until the deadline closes them.
- Erasure deletes held runs; the automatic stage makes this rarer, and the runbook says to reconcile first ([privacy spec](privacy-consent-retention.md)).

## Open questions

- The held pre-loyalty run on `poc68-isolated` (STATUS) may be reconciled only with the owner's approval, because that stack must not be changed without it.
