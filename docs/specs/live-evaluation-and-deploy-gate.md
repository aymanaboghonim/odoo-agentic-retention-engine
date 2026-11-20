# Spec: Live evaluation workflow and fingerprint deploy gate

- Issue: #125 (spec); implemented in #135
- ADRs: [ADR-0031](../adr/0031-live-evaluation-workflow-and-fingerprint-deploy-gate.md), [ADR-0029](../adr/0029-observability-topology-and-telemetry-data-rule.md)
- Parent: [release gates and kill switch](release-gates-and-kill-switch.md) (EV-1, G1..G5), [run governance](project-wide-run-governance.md)
- Status: draft

## Purpose

Run the live release-gate evaluation in GitHub Actions, without the owner's machine, and make EV-1 mechanical: a deploy whose model behavior changed needs a passing evaluation first.

## Scope and non-goals

Scope: `llm-eval.yml`; the fingerprint; the report PR; the gate step in `deploy-aws.yml`; Langfuse traces from evaluation runs and decision events.

Non-goals: changing the gate thresholds (G1..G5 stay as agreed); scheduled unattended runs; feedback-triggered runs (post-MVP); production traffic evaluation.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| EVW-1 | The live evaluation is never part of `make ci` or a required status check, and needs the `llm-eval` environment. | `test_llm_eval_workflow_is_separate_and_approval_gated` |
| EVW-2 | The fingerprint is deterministic and covers the prompt, the model settings, and every gate and proposal source file. | `test_eval_fingerprint_is_stable_and_covers_behavior_files` |
| EVW-3 | A deploy is blocked when G1..G3 fail, or when no merged report matches a changed fingerprint. | `test_deploy_gate_blocks_on_safety_failure_or_missing_report` |
| EVW-4 | A G4 or G5 failure only warns; deploying past it needs a non-empty override reason, written to the job summary. | `test_quality_override_requires_reason` |
| EVW-5 | Preflight runs before any paid call, and spend stays under the hard ceiling. | `test_eval_workflow_preflight_and_ceiling` |
| EVW-6 | The report and job summary contain no secret, and the evaluation uses only the fixed synthetic datasets. | `test_eval_report_has_no_secrets_and_synthetic_only` |
| EVW-7 | Operator decisions are exported as events in the run trace with the decision code only (ADR-0032). | `test_operator_decision_event_has_code_only` |

## Behavior

- **Given** a merged change to the proposal prompt, **when** `llm-eval.yml` starts on `main`, **then** it waits for the owner's approval; if approved, it runs and opens a PR with the report and its fingerprint.
- **Given** a deploy of that commit before the report PR is merged, **when** the gate step runs, **then** the deploy stops and names the missing fingerprint (EVW-3).
- **Given** a deploy that changes only infrastructure scripts, **when** the gate step runs, **then** the fingerprint matches the last passing report and the deploy continues.
- **Failure:** the provider times out mid-run: the run fails at the named stage, publishes no report, and the last passing report stays (run governance).
- **Abuse:** a report edited by hand to say "pass" with a wrong fingerprint does not match the deployed commit, so the gate blocks.

## Risks

- A behavior file missing from the fingerprint list lets a change through untested. EVW-2 lists the files explicitly and fails when a new file appears in the covered folders.
- Pending approvals may pile up after many merges; only the newest run per fingerprint matters, and older ones can be rejected.

## Open questions

- None.
