# Runbook: live evaluation in GitHub Actions

Issue #135; spec [live-evaluation-and-deploy-gate.md](../specs/live-evaluation-and-deploy-gate.md); [ADR-0031](../adr/0031-live-evaluation-workflow-and-fingerprint-deploy-gate.md). The workflow is `.github/workflows/llm-eval.yml`. It is paid (cents per run) and runs only after the owner approves it.

## One-time setup (owner)

1. Settings > Environments > New environment `llm-eval`:
   - Required reviewers: the owner. Leave "Prevent self-review" **off**: the owner both starts and approves runs (F-091).
   - Deployment branches: `main` only.
   - Environment secrets: `OPENAI_API_KEY` (the existing key), `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` (project `aorte-mvp`, US). Paste each value from its file outside the repo; never into a command that prints it.
2. Settings > Actions > General > Workflow permissions: turn on "Allow GitHub Actions to create and approve pull requests" (F-091). Keep the default token read-only.

## Run it

- It starts by itself after a merge to `main` that changes a covered file (the list is in `scripts/eval_fingerprint.py`). Or start it by hand: `gh workflow run llm-eval.yml --ref main`.
- The `evaluate` job waits for approval. Approve to pay; reject, or leave it pending, to skip. Only the newest run per fingerprint matters; reject older pending runs.
- Stages print as `stage=...` lines: preflight (no provider call), model load and policy index, per set `smoke` then `evaluation` with `case-i-of-n`, then `langfuse-readback`.
- Print the current fingerprint locally: `PYTHONPATH=src uv run --no-sync python -m scripts.eval_fingerprint`.

## Report PR

The `report-pr` job opens a tracking issue and a PR `eval: live evaluation report <fp12>` with the report in `evals/release/results/eval-<time>-<fp12>/`. A PR made by the workflow token starts its CI only after approval: open the PR, approve the waiting `CI` run, read `README.md` (safety G1..G3, quality G4/G5, spend, Langfuse count), then squash-merge. The PR closes its tracking issue.

## Deploy gate (#157)

`deploy-aws.yml` runs the job `eval-gate` before the deploy job (`scripts/eval_deploy_gate.py`). It computes the commit's fingerprint and looks for `evals/release/results/eval-*/report.json` with the same fingerprint and the same fingerprint inputs (the newest one decides).

- No matching report: blocked. Run this workflow on `main`, approve it, and merge its report PR first.
- G1..G3 failed in the report: blocked; an override does not help.
- Only G4/G5 failed: blocked unless the deploy is started with `-f quality_override_reason="<why>"`; the reason (one line, at most 300 characters) is written to the job summary.
- Run it on a branch (`gh workflow run deploy-aws.yml --ref <branch>`) to see the gate result without deploying: the deploy job runs only from `main`.

## When it fails

- Preflight failure: the message names the missing item (secret, setting, ceiling, unclassified file). Nothing was paid.
- Failure during the run (provider timeout after one retry, smoke check, budget): the job fails at the named stage and publishes no report. The last merged report stays valid for its fingerprint. The throwaway database is gone with the runner, so a new run starts clean.
- Langfuse read-back `incomplete`: the report is still published; check Langfuse for `environment = eval` in the run's time window (F-092 notes up to 15 minutes delay for some exporters).

## Limits

- Fixture carts only; no Odoo, execution, or delivery is tested here (those are in `make ci` and the AWS smoke).
- The ceilings are reservations; actual spend is the estimate from provider usage, not a bill (`#130` reconciles billing).
