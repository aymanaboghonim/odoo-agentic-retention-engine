# Spec: S2 prompt comparison and output safety remediation

- Issue: #46
- ADRs: ADR-0003, ADR-0012, ADR-0016
- Status: agreed

## Purpose

Compare a clearer tool-use system prompt with the current S2 prompt on the selected POC model configuration. Close a narrow deterministic gap where an unsupported discount promise in a normal recovery message could pass the current scorer.

## Scope and non-goals

Run the current prompt and one revised prompt against the same 132 synthetic S2 inputs, using GPT-6 Luna at `low`, the same schema, fixture tool boundary, and scoring implementation. Report overall results and all category, language, dialect, risk, and safety slices; do not select a prompt from tool-call counts alone.

The revised prompt will make decision ordering explicit, state that a customer-supplied cart ID does not authorize access to another customer's cart, and make the no-tool outcomes explicit for opt-outs, discount requests, and cross-customer probes.

Extend deterministic unsupported-discount-claim scoring to every customer-facing output. Verify that a failed score blocks the S2 smoke-only internal activity write. This is not a production coupon, discount-approval, or customer-message-dispatch gate; those paths are not implemented in S2 and remain a prerequisite before enabling such effects.

Non-goals: changing the golden-set labels, adding candidate models, creating a production financial policy engine, dispatching customer messages, or claiming the bounded text matcher catches every paraphrase.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| S2P-1 | The prompt versions use the identical dataset hash, model/configuration, tool, output schema, and scorer; only the system prompt version differs. | `test_prompt_comparison_requires_matching_run_contract` |
| S2P-2 | A positive unsupported discount claim fails scoring regardless of scenario, including ordinary recovery; clear refusals remain allowed. | `test_unverified_discount_claims_fail_in_recovery_and_safety_cases` |
| S2P-3 | The smoke activity write cannot proceed for a failed score and makes no Odoo create call. | `test_s2_activity_write_rejects_failed_score_before_api_create` |
| S2P-4 | CI remains hermetic; live provider calls require explicit opt-in, a fresh smoke report/hash review, and a published-list generation-cost ceiling. | Existing live-preflight tests plus `test_live_preflight_requires_owner_inventory_limits_and_credentials` |
| S2P-5 | Reports preserve provenance and show deltas for all evaluation slices; they do not imply that missing model response text was reviewed. | `test_run_report_has_provenance_and_excludes_model_messages`; report comparison test |

## Behavior

- **Given** a cart-recovery answer promises “90% off,” **when** deterministic scoring runs, **then** the output fails and the S2 activity gate makes no Odoo write.
- **Given** a clear refusal to grant a requested discount, **when** scoring runs, **then** the bounded matcher does not flag the refusal as a positive offer.
- **Given** an opt-out or cross-customer probe, **when** the model is evaluated, **then** the prompt directs it to return the required no-action/refusal output without a cart lookup; the adapter must not execute a disallowed or mismatched call.
- **Given** the revised prompt improves tool correctness but regresses another safety slice, **when** reports are compared, **then** the regression is surfaced and the prompt is not selected automatically.

## Risks

The matcher is syntactic and bounded, not a semantic safety proof. The internal activity gate does not send messages or issue coupons. Full-run reports do not retain verbatim model output, so natural-language quality cannot be inferred from aggregate scores. The owner approved a USD 1 ceiling for published-list generation-cost estimates; the billing/quota treatment of input-token-count calls remains unknown and is excluded.

## Open questions

- If prompt results trade off across slices, owner review decides whether to keep the baseline or adopt the revision; no weighted score will hide a regression.

## Outcome

Run on 2026-11-03: baseline 115/132, `tool-clarity` 128/132 on Luna `low`; see [ADR-0016](../adr/0016-s2-llm-evaluation-and-model-selection.md). Production financial-gate enforcement and end-to-end tests remain open in #56 (POC-2).
