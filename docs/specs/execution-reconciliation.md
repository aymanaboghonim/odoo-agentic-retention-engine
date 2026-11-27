# Spec: Automatic execution reconciliation for held effects

- Issue: #125 (spec); implemented in #131 (evidence and API) and #132 (automatic and admin resolution)
- ADRs: [ADR-0030](../adr/0030-execution-reconciliation-without-resend.md), [ADR-0012](../adr/0012-worker-owned-agent-execution.md), [ADR-0027](../adr/0027-operator-container-in-core-task.md)
- Parent: [gated execution](gated-execution.md) (`EX-4`), [operator RBAC](operator-rbac-and-expiring-approvals.md)
- Status: implemented in #131 and #132; live checks in #133

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

Settings (all required, no default, fail closed): `RECONCILE_INTERVAL_MINUTES`, `RECONCILE_STALE_STARTED_MINUTES`, `RECONCILE_DEADLINE_HOURS` (integers `>= 1`). #131 adds `RECONCILE_STALE_STARTED_MINUTES` (1 to 10080; worker, approval API, operator; 30 on AWS) and `RECONCILE_MAILPIT_API_URL` (`reconcile_cli` only; `http://` and a private host); #132 adds the other two and `RECONCILE_STAGE` (`YES` or `NO`, required; AWS `YES` with 10 minutes and 24 hours, Compose `NO`).

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| REC-1 | The evidence lookup is read-only: no Odoo write, no dispatch, no engine DB change. | `test_reconcile_check_is_read_only`, boundary rule REC-1 in `scripts/check_import_boundaries.py` (`test_reconcile_check_cannot_reach_dispatch_or_execution`) |
| REC-2 | The email Message-ID is deterministic from (run, claim, attempt) and stored before the send. | `test_message_id_is_recorded_before_dispatch`, `test_telegram_and_coupon_effects_store_no_message_id` |
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

## Implementation notes (#131)

- Owner decisions 2026-11-27: one issue; `GET /held-runs` reads the engine database only (no live lookup in the API, which has no Odoo key or Mailpit path; ADR-0012). Live evidence comes from `engine.reconcile_cli check`; in #132 the worker stage stores the latest evidence for the API.
- Migration 0014 adds `execution_effects.message_id`. `start_effect(email=True)` stores `effect_message_id(run, claim, attempt)` = `<aorte-<uuid5>@aorte.invalid>` (opaque, `.invalid` is reserved) in the same transaction that marks the attempt `started`; the dispatcher then sends with that ID. Telegram and coupon effects store none.
- `engine.reconcile` (no side effects): settings, the evidence codes, `HeldEffect`, and the API view. `RunRepository.held_effects` and `held_effect_count(stale_before=...)` use the held definition above; the worker gauge `aorte.execution.held` now counts stale `started` effects too.
- `engine.reconcile_cli check` (operator container, `EXEC_MODULES`): coupon by `loyalty.card` `search_read` on the run's code (archived included, exact and unique, else `unknown`); Mailpit by `GET /api/v1/search?query=message-id:"<id>"`, compared exactly, because Mailpit matches substrings (`F-101`); Telegram `attestation_required`. Output: JSON lines with codes only.
- `engine.erp_cli list`: run ID, state, trigger type, created time (found missing in #140).
- `scripts/aws_mvp_rollout.sh` refuses a task definition without `RECONCILE_STALE_STARTED_MINUTES` (apply Terraform first, #133). Runbook: [reconciliation.md](../runbooks/reconciliation.md).

## Implementation notes (#132)

- Owner decisions 2026-11-27: one issue; the worker runs the stage and gets the Odoo API key (SSM), the Odoo settings, and links to `odoo` and `mailpit`; 10 minutes and 24 hours on AWS; the live checks move to #133 (deploys run only from `main`).
- Migration 0015: `execution_effects.evidence`, `evidence_at`, `coupon_deactivated_at`; table `reconcile_decisions` (one open decision per effect, outcome `applied`, `rejected_mismatch`, or `not_held`).
- `engine.reconcile_stage.ReconcileStage` (worker maintenance, every interval, never raises): evidence for each held effect, then an open admin decision if it matches the evidence (`accept_decision`), else definitive evidence only (`evidence_resolution`); a run whose unresolved effect is older than the deadline fails as `reconciled_unverified`. `RunRepository.resolve_held` does it in one transaction with the run row locked (REC-8); `run_outcome` gives the result: `completed`, `reconciled_message_not_sent` (also when the coupon exists but the message never started), `reconciled_coupon_not_created`, or `reconciled_unverified`.
- REC-6 is a little stricter than written: an attestation does not override definitive evidence to the contrary (for example `not_sent` while Mailpit has the message).
- REC-10: `orphan_coupons` lists runs failed as message-not-sent with a confirmed coupon not yet deactivated; `CouponDeactivator` archives the program and card through JSON-2 (`archive_coupon`); a failed call is retried at the next stage.
- REC-7: `POST /held-runs/{run}/resolve` (admin, `ResolveInput`, reason checked like the runtime reason) only records; a refused call is audited. CLI fallback: `approval_cli held` and `approval_cli resolve`, through the API with a token, instead of `reconcile_cli resolve` from the issue, so the actor is a verified admin.
- REC-12: run events with the actor and the evidence; counter `aorte.reconcile.resolutions` (label `aorte.resolution`); Grafana alert "Held run closed unverified".
- The import rule REC-1 also covers `reconcile_evidence` and `reconcile_stage` (no `channels`, `execution`, or LLM provider: REC-5).
- Grafana plan `e7aff50bb321` (the sixth rule) applied 2026-11-27, no drift. The `mvp` plan `7bdbe1ef0292` (worker access and settings, with the #165 and #166 changes) was reviewed on 2026-11-27 and is re-planned and applied in #133.

## Open questions

- The held pre-loyalty run on `poc68-isolated` (STATUS) may be reconciled only with the owner's approval, because that stack must not be changed without it.
