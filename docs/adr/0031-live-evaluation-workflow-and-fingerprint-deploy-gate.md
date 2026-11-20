# ADR-0031: Live evaluation workflow and fingerprint deploy gate

- Status: accepted; decision 5 (decisions as Langfuse scores) superseded in part by [ADR-0032](0032-operator-decisions-as-trace-events-one-trace-per-run.md)
- Date: 2026-11-19
- Issue: #125

## Context

The release gates are defined in [release-gates-and-kill-switch.md](../specs/release-gates-and-kill-switch.md): G1 to G3 are zero-tolerance safety gates, and G4 and G5 are quality gates. EV-1 says no release unless the live evaluation meets G1 to G3, but that is checked by hand: the live runs were started from the owner's machine (`scripts/release_gate_eval.py`), and `deploy-aws.yml` does not check any result. "Mechanical gates over promises" ([process.md](../process.md)) asks for a check.

Behavioral tests stay hermetic (process principle 4): `make ci` and the required `Local-parity checks` never call a paid LLM. A live evaluation is not a behavioral test; it is a paid run under the preflight, progress, and report rules. GitHub-hosted runners are free on public repositories (`F-083`) and can reach Langfuse (`F-078`).

## Decision

1. **Separate workflow `llm-eval.yml`.** Triggers: `workflow_dispatch`, and push to `main` when prompt, model, gate, or proposal files change. The job runs in a protected `llm-eval` environment with the owner as required reviewer: approving pays for the run; rejecting it, or leaving it pending, skips it and nothing waits. It is never a required status check, and `make ci` never calls it.
2. **Run rules.** Preflight first, a hard USD ceiling enforced by the proposal budget, the fixed synthetic datasets (tuned set and held-out set), `TELEMETRY_EVAL_TEXT=YES` traces to Langfuse ([ADR-0029](0029-observability-topology-and-telemetry-data-rule.md)), and a Markdown + JSON report as the job summary and artifact.
3. **Fingerprint.** The report records a fingerprint: a SHA-256 over the prompt version, the model and its settings, and the gate and proposal source files. A passing report reaches `main` through a PR the workflow opens; the owner merges it.
4. **Deploy gate.** `deploy-aws.yml` computes the fingerprint of the commit it deploys, then looks for a merged report with the same fingerprint:
   - G1 to G3 failing, or no report for a changed fingerprint: the deploy is blocked.
   - G4 or G5 failing: a warning; the owner may deploy with a recorded override reason (a workflow input written to the job summary).
   - Unchanged fingerprint: the last passing report applies, so unrelated deploys are never blocked.
5. **Feedback.** Operator approve and reject decisions are exported as Langfuse scores on the proposal trace (decision code only). Feedback-triggered evaluation and growing the eval sets from feedback are a later item (STATUS, post-MVP).

## Alternatives

- **Live eval inside `make ci` or as a required check:** breaks the hermetic rule and makes every PR pay. Rejected.
- **Report only, never block:** leaves EV-1 as a promise. Rejected.
- **Block on any gate failure:** G4 and G5 have small samples (N 16 to 41, wide intervals); one miss would stop deploys. Rejected; G4/G5 warn with an override.
- **Run on pull requests:** shows results before merge, but a pending approval shows as noise on every touching PR. Not chosen.
- **Unattended scheduled runs:** need an environment without a reviewer, so paid runs without approval. Not chosen.

## Consequences

- EV-1 becomes a mechanical check; a change to the prompt, model, gate, or proposal code needs a passing evaluation before it is deployed.
- Each evaluation costs a few cents (the release-gate runs were USD 0.0060 to 0.0082 estimated, spec above) and an approval click.
- The fingerprint file list must cover every file that can change model behavior; a missed file would let a change through. A test checks the list.
