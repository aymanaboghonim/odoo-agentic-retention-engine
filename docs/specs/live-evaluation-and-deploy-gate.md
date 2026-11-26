# Spec: Live evaluation workflow and fingerprint deploy gate

- Issue: #125 (spec); implemented in #135 (workflow, EVW-1, EVW-2, EVW-5..EVW-7), #156 (first live run), #157 (deploy gate, EVW-3, EVW-4)
- ADRs: [ADR-0031](../adr/0031-live-evaluation-workflow-and-fingerprint-deploy-gate.md), [ADR-0029](../adr/0029-observability-topology-and-telemetry-data-rule.md)
- Parent: [release gates and kill switch](release-gates-and-kill-switch.md) (EV-1, G1..G5), [run governance](project-wide-run-governance.md)
- Status: draft; implemented in #135 (workflow) and #157 (deploy gate), 2026-11-26

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

## How it works (#135)

- **Fingerprint** (`scripts/eval_fingerprint.py`, `python -m scripts.eval_fingerprint` with `PYTHONPATH=src`): a SHA-256 over the model, the prompt version, the evaluation token limits (4,000 in, 8,192 out), and the SHA-256 of every covered file: the proposal path, the gate and fixed rule, retrieval, PII tokenization, the policy snapshot, the two datasets, and the scoring scripts. Every `src/engine/*.py` and `policies/*` file is either covered or excluded with a written reason; an unclassified file fails EVW-2. The `llm-eval.yml` push trigger lists exactly the covered files.
- **Run** (`scripts/release_gate_ci.py`): a throwaway PostgreSQL service (refused unless local, flagged `EVAL_THROWAWAY_DATABASE=YES`, and empty), schema migration, preflight (no provider call), the policy index built with the pinned E5 model, then per set (tuned 62 cases, held-out 44 cases as of 2026-11-26) a 1-case smoke checked by machine and the full set. Only the proposal step runs, on fixture carts: no Odoo, no execution, no channel, no customer. **Owner decision 2026-11-26:** for workflow runs the approval of the run replaces the owner's smoke digest review (the local #87 script keeps it).
- **Ceilings**: review budgets USD 1.00 (tuned) and USD 0.90 (held-out), together USD 1.90, under the owner's USD 2.00 per evaluation; per call USD 0.05. They are fixed in code, so they are part of the fingerprint. The #87 runs spent about USD 0.014 for both sets.
- **Telemetry**: `TELEMETRY_ENVIRONMENT=eval`, Langfuse US only (`us.cloud.langfuse.com`, directly, not the relay), `TELEMETRY_EVAL_TEXT=YES`, daily unit cap 2,000. After the run the report records how many `eval` generations Langfuse lists through `GET /api/public/v2/observations` (F-090, F-092). The read-back is recorded, not required: a Langfuse outage must not hide the safety result.
- **Report**: `evals/release/results/eval-<UTC time>-<fingerprint 12>/` with `report.json` and `README.md` (fingerprint, commit, safety and quality verdicts, spend, Langfuse count) and per set the #87-format smoke and evaluation reports. It is published only after the whole run succeeds, then uploaded as an artifact. The `report-pr` job (no secrets) opens a tracking issue, a branch `<issue>-eval-report-<fp12>`, a commit with `Refs #<issue>`, and a PR with `Closes #<issue>`. GitHub starts CI on a PR made with `GITHUB_TOKEN` only after an approval (F-091), so the owner approves the CI run, reads the report, and merges.
- **Setup** (owner, #156; steps in the [runbook](../runbooks/live-evaluation.md)): the `llm-eval` environment with the owner as required reviewer and "Prevent self-review" off, deployments from `main` only; secrets `OPENAI_API_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`; the repository setting "Allow GitHub Actions to create and approve pull requests" on.

## Deploy gate (#157)

- `deploy-aws.yml` has a job `eval-gate` (no secrets, no environment) that the deploy job `needs`; the deploy job also runs only from `main`. The gate runs `scripts/eval_deploy_gate.py` on the checked-out commit.
- A report counts only if it is a `live-evaluation` report whose `fingerprint` equals the commit's, whose `fingerprint_inputs` equal the commit's own inputs, and whose inputs hash to the fingerprint (the abuse case above). The newest matching report decides. Safety and quality are recomputed from every set's gates; a summary that disagrees with its sets fails.
- The override reason is the workflow input `quality_override_reason`. It reaches the script through an environment variable (never inside the shell script), is cut to one line of at most 300 characters, and is written to the job summary.
- First report (2026-11-26, `#156`): held-out G4 warns, so deploys of fingerprint `3b939f5a4e9d` need an override reason until a newer report passes.

## Risks

- A behavior file missing from the fingerprint list lets a change through untested. EVW-2 lists the files explicitly and fails when a new file appears in the covered folders.
- Pending approvals may pile up after many merges; only the newest run per fingerprint matters, and older ones can be rejected.

## Open questions

- None.
