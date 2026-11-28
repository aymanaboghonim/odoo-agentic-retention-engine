# Runbook: daily provider billing reconciliation

Spec: [observability](../specs/observability-telemetry.md) (BR-1, implementation notes #130). Decision: [ADR-0034](../adr/0034-billing-reconciliation-in-its-own-environment.md).

The workflow `.github/workflows/billing-reconcile.yml` runs daily at 06:23 UTC (and by dispatch from `main`). It prints only `billing day=<day> status=<pass|drift|unknown> push=<ok|failed>`; amounts go to Grafana and to the private report.

## One-time setup

1. GitHub environment `billing`: deployment branches `main` only, no required reviewer.
2. Secrets of `billing` (never printed; set from files outside the repo):
   - `OPENAI_ADMIN_KEY`: an organization Admin key (only an organization Owner creates it, `F-036`; it cannot be limited to read-only). Not kept in `aws-mvp`.
   - `GRAFANA_STACK_URL`, `GRAFANA_OTLP_TOKEN` (the existing write token, base64 of `<instance-id>:<token>`).
   - `GRAFANA_READ_TOKEN`: a token of the Viewer service account `aorte-billing-read`, made by the owner in Grafana (Administration, Service accounts). The Terraform service account is an Editor and cannot make service accounts.
   - `AWS_BACKUPS_BUCKET`, and `AWS_BILLING_ROLE_ARN` (bootstrap output `billing_role_arn`).

   Example, values never shown: `gh secret set --env billing GRAFANA_STACK_URL < ~/.config/aorte/grafana-stack-url`.
3. Terraform: the bootstrap root holds the role `aorte-mvp-gha-billing` (`s3:PutObject` on `reports/*` only); the `mvp` root holds the `expire-reports` lifecycle rule (90 days); the Grafana root holds the panel and the two rules. All through `scripts/tf_safe.sh`.

## Read a result

- Grafana dashboard, row "Provider billing": provider, engine estimate, drift, and known (1) or unknown (0) for the latest finished day.
- Report: `aws s3 cp s3://<backups bucket>/reports/billing/<day>.json -` (owner's SSO session). Per day of the 7-day window: provider USD per project, engine USD, evaluation USD, drift, status.

## Alerts

- **Provider billing drift above USD 0.10**: the provider charged more or less than the engine estimated plus the merged evaluation reports. Usual causes: a local run with telemetry off, an evaluation run whose report PR was not merged, a price change not in the ledger rates. Check the report's per-project amounts.
- **Provider billing day unknown**: no provider bucket for the day or a failed lookup. Never read as 0 (BR-1). If it repeats, run the workflow by dispatch later in the day (provider lateness is measured in #175).

A failed run (config or push error) is reported by GitHub by email. In a public repository a scheduled workflow is disabled after 60 days without repository activity (`F-103`); re-enable it under Actions.
