# Spec: Synthetic full-stack worker and capacity measurement

- Issue: #68
- ADRs: [ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md),
  [ADR-0002](../adr/0002-modular-monolith.md),
  [ADR-0004](../adr/0004-batch-over-streaming.md),
  [ADR-0012](../adr/0012-worker-owned-agent-execution.md)
- Status: agreed (owner, 2026-11-08)

## Purpose and scope

Run the local synthetic POC stack and create durable agent runs from a bounded,
scheduled scan of abandoned carts. Measure the isolated stack at idle and
during a representative scan and one explicitly enabled model proposal. One
additional proposal is allowed only as the owner-approved recovery from the
initial measurement's sampler failure.

The stack contains isolated Odoo and engine databases, Odoo, the engine API and
worker, and Mailpit. Production deployment, real customer contact, throughput
claims, release gates, and the full end-to-end evaluation remain out of scope.

## Decisions

- Scheduled scanning is on for the isolated synthetic stack; it creates at most
  one durable run per cart. Proposal generation requires explicit operator
  enablement and a durable shared budget. Stack startup does not itself call
  the model or send messages.
- A scan must eventually traverse all eligible carts even when earlier carts
  were already processed. A per-scan bound limits work; progress must not
  silently skip later carts.
- The initial scan recognizes the synthetic review fixtures created by
  [the review workflow](../runbooks/gated-execution-review.md): an order
  reference `retention-review-` followed by 12 lowercase hex characters, and
  an email matching that reference at `example.test`. Other Odoo demo records
  are not treated as authorized fixtures. These markers do not replace the
  requirement to verify the Odoo instance is isolated.
- The owner approved at most USD 1 in total estimated model generation cost
  across both #68 attempts, including reservations for uncertain calls. The
  initial attempt is followed by at most one recovery proposal, and only when
  the initial sampler failure left no report, the initial run is rejected, its
  single provider call is settled, and no result is uncertain (owner approval,
  2026-11-09). Both attempts use the same durable aggregate budget; count-only
  billing is unknown (F-038). Active provider limits remain unknown (F-046).
- Customer-facing effects stay behind the existing gate, approval, and explicit
  dedicated synthetic destination confirmation. Scanning and proposal
  generation never create a coupon or send a message.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| FS-1 | Runtime Odoo database access is read-only; state changes use the supported API only against isolated synthetic Odoo. | `test_synthetic_reader_rejects_elevated_database_role`, `test_synthetic_role_check_rejects_engine_database_owner`, `test_no_sql_write_path_in_adapter` |
| FS-2 | Repeated and concurrent scans or restarts produce no more than one run per synthetic cart. | `test_repeated_scans_create_one_run`, `test_concurrent_scans_create_one_run` |
| FS-3 | A bounded scan can advance past previously seen carts without losing eligible later carts. | `test_scan_paginates_past_seen_carts` |
| FS-4 | Startup and scan alone never call a provider, create a coupon, or send a message; proposal generation requires explicit enablement and a bounded durable budget. | `test_worker_checks_role_before_scanning_without_provider`, `test_capacity_settings_require_explicit_shared_budget`, `test_failed_proposal_attempt_remains_claimed` |
| FS-5 | Approval access is authenticated and cannot execute customer-facing effects; the Compose service publishes no port and the web UIs bind only to loopback. | `test_approval_authentication`, `test_approval_token_not_exposed`, `test_pending_approval_has_no_external_side_effects`, `test_approval_api_is_unpublished_and_private`, `test_web_ui_ports_bind_only_to_loopback` |
| FS-6 | Measurement preflight fails before costly or state-changing work if required isolation, configuration, permissions, model cache, budget, or outputs are missing. | `test_capacity_preflight_fails_before_proposal_if_key_missing`, `test_capacity_settings_require_explicit_shared_budget`, `test_full_stack_init_rejects_unsafe_configuration` |
| FS-7 | Failed measurements do not overwrite successful results; progress and human-readable and machine-readable reports describe the same completed workload and its limits. | `test_capacity_reports_match_and_never_overwrite`, `test_snapshot_refreshes_container_list_when_a_run_container_exits` |
| FS-8 | CI remains hermetic; live calls and the actual capacity measurement require a separate operator run. | `make ci`, `test_worker_checks_role_before_scanning_without_provider` |
| FS-9 | The single recovery claim requires a rejected initial run with exactly one settled provider call, a different synthetic run, and the same aggregate budget; neither claim can be reused. | `test_recovery_claim_requires_one_settled_rejected_initial_attempt`, `test_capacity_proposal_claim_is_one_shot_under_concurrency` |

## Behavior

- Given isolated services and valid configuration, one documented command
  starts the stack; the worker creates runs for eligible synthetic carts.
- Given repeated or concurrent scans, the stable cart identity yields the
  existing run rather than another run or a repeated side effect.
- Given an operator-enabled, budgeted proposal run, the worker can create a
  proposal; absent that enablement it only scans and persists runs.
- Given a failed measurement report after a completed, settled and rejected
  initial proposal, the owner-approved recovery may use one different synthetic
  run. The same aggregate USD 1 budget applies; an uncertain provider result,
  a non-rejected initial run, or a repeated recovery claim fails closed.
- Given unavailable services, invalid configuration, a non-synthetic target,
  exhausted budget, or an uncertain external result, fail explicitly without
  inventing a success or retrying an uncertain effect automatically.
- Given malicious or unexpected cart/model content, no content can authorize
  an Odoo write, bypass the deterministic gate, or choose a destination.
- Given completed measurement samples, publish matching Markdown and raw data
  with host and container provenance, workload, sampling method, and limits.
  One workload is not a production capacity or throughput benchmark.

## Risks and open questions

The existing approval API listens and accepts requests only on loopback; a
container network must not weaken that boundary. Existing oldest-first scan
queries have a limit and need pagination to avoid starvation. Measurement
depends on local resources, model cache, provider access, and the explicitly
approved budget; preflight cannot prove future provider success. The recovery
is a one-time exception for the lost sampler output, not a general retry policy.
A scan-plus-proposal sample does not establish production sizing or pricing.
