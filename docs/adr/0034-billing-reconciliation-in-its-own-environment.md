# ADR-0034: The daily billing reconciliation runs in its own GitHub environment

- Status: accepted (owner, 2026-11-27)
- Date: 2026-11-27
- Issue: #130
- Supersedes in part: [ADR-0029](0029-observability-topology-and-telemetry-data-rule.md) decision 8, only "The OpenAI admin key is a secret of the protected `aws-mvp` environment". The rest of decision 8 (daily in GitHub Actions, provider USD pushed to Grafana beside the engine's estimate, a daily aggregate JSON report under `reports/` with a lifecycle expiry, pass or fail only in the log) stays.

## Context

ADR-0029 put the OpenAI admin key in the `aws-mvp` environment. That environment has the owner as a required reviewer, and protection rules apply to every job that references the environment (F-103), scheduled jobs included. A daily job there would wait for an approval every day; a day without an approval would have no data.

The job never deploys, never touches the stack, and needs only: the OpenAI admin key (read the organization Costs API; it cannot be limited to read-only, F-036), a Grafana read token, the Grafana OTLP write token, and write access to `reports/` in the backups bucket.

## Decision

1. **Environment `billing`.** Deployment branches: `main` only. No required reviewer. It holds only the job's secrets: `OPENAI_ADMIN_KEY` (moved from `aws-mvp`), `GRAFANA_STACK_URL`, `GRAFANA_READ_TOKEN` (a Viewer service account, `aorte-billing-read`), `GRAFANA_OTLP_TOKEN`, `AWS_BACKUPS_BUCKET`, and `AWS_BILLING_ROLE_ARN`.
2. **Own AWS role.** `aorte-mvp-gha-billing` (bootstrap root) trusts only the OIDC subject of the `billing` environment and may only `s3:PutObject` on `reports/*` of the project's backups bucket. The deploy role keeps trusting only `aws-mvp`, so neither job can assume the other's role.
3. **Drift.** The job computes the drift itself for one finished UTC day: OpenAI organization cost minus (the engine's estimate in Grafana plus the actual USD of the evaluation reports merged to `main`). Grafana accepts samples at most 2 h old (F-102), so the job pushes the day's values as current gauges. A day without a provider bucket, or with a failed lookup, is `unknown`, never 0 (BR-1). The alert threshold is USD 0.10 a day.

## Alternatives

- **Keep `aws-mvp` and approve daily.** No new environment, but one approval click a day and lost days. Rejected by the owner.
- **No environment, repository secrets.** Any workflow on any branch could read the admin key. Rejected.
- **Export evaluation runs to Grafana instead of reading their reports.** Cleaner long term, but `scripts/release_gate_ci.py` is in the behavior fingerprint: the #133 deploy would need a new paid evaluation run first. Not chosen (owner, 2026-11-27).

## Consequences

- A merged change on `main` can run with the admin key without a review. `main` is protected and only the owner merges, which is the same trust as the `llm-eval` push trigger.
- The admin key is broad; the script calls only `GET /v1/organization/costs` and prints only `billing day=... status=...`.
- In a public repository a scheduled workflow is disabled after 60 days without repository activity (F-103); STATUS records it if that happens.
