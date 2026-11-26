# Runbook: Grafana dashboard as code

Issues #139 (dashboard) and #163 (alerts); root `deploy/terraform/grafana`; spec [observability-telemetry.md](../specs/observability-telemetry.md); facts `F-094`..`F-096`.

## What is managed

- Folder `aorte` ("aorte engine") and dashboard `aorte-overview` ("aorte engine overview"). The JSON is `deploy/terraform/grafana/dashboards/aorte-overview.json`; edits in the Grafana UI are overwritten by the next apply.
- Panels: runs by state, held effects, worker last scan age, runtime state, LLM tokens, estimated LLM USD, effect outcomes, dropped telemetry, and three trace tables (LLM calls, gate decisions, export checks). Each trace row links to the same trace in Langfuse.
- Data sources are dashboard variables (`prom`, `tempo`), so no stack name is in the repo; `env` selects `aws-mvp`, `local`, or `eval`.

## Secrets and private values (outside the repo)

| Value | Where | Used as |
| :-- | :-- | :-- |
| Service account token (`aorte-terraform`) | `~/.config/aorte/grafana-sa-token` | `GRAFANA_AUTH` |
| Stack URL | `~/.config/aorte/grafana-stack-url` | `GRAFANA_URL` |
| Langfuse project ID | `~/.config/aorte/langfuse-project-id` | `langfuse_project_id` in the gitignored `terraform.tfvars` |
| Alert email | `~/.config/aorte/alert-email` | `alert_email` in the gitignored `terraform.tfvars` |

The token is a Grafana service account token, not the OTLP write token (`F-094`). It never goes to the AWS host.

## First setup (owner, once)

1. Administration > Users and access > Service accounts > Add service account `aorte-terraform`, role Editor; add a token; save it to the file above (mode 600). Save the stack URL the same way.
2. Copy `backend.hcl.example` to `backend.hcl` (same state bucket as the `mvp` root) and `terraform.tfvars.example` to `terraform.tfvars` with the project ID.
3. The first apply creates the folder, but an Editor service account gets no access to a folder it creates (`F-094`), so the apply stops with 403 `folders:read`. Open the folder > Manage permissions, add the service account with Admin, run `terraform -chdir=deploy/terraform/grafana untaint grafana_folder.aorte` (state only), and plan again. Done on 2026-11-26.

## Change the dashboard

```bash
export AWS_PROFILE=aws-mvp   # the state is in S3
export GRAFANA_URL="$(cat ~/.config/aorte/grafana-stack-url)" GRAFANA_AUTH="$(cat ~/.config/aorte/grafana-sa-token)"
scripts/tf_safe.sh deploy/terraform/grafana plan        # owner reviews .tfsafe/plan.txt
scripts/tf_safe.sh deploy/terraform/grafana apply --confirm <plan-id>
```

The plan text holds the Langfuse project ID inside the dashboard JSON; it stays in the gitignored `.tfsafe/` and is deleted after apply. Redact it before sharing a plan.

## Check the Langfuse link

```bash
PYTHONPATH=src TELEMETRY_ENABLED=YES \
  GRAFANA_OTLP_ENDPOINT=https://otlp-gateway-prod-us-east-2.grafana.net/otlp TELEMETRY_DAILY_SPAN_CAP=100 \
  LANGFUSE_OTLP_ENDPOINT=https://us.cloud.langfuse.com/api/public/otel TELEMETRY_DAILY_LANGFUSE_UNIT_CAP=100 \
  uv run --no-sync python scripts/telemetry_check.py --environment local \
  --token-file ~/.config/aorte/grafana-otlp-token --langfuse-key-file ~/.config/aorte/langfuse-keys
```

Then open the dashboard (last hour), find the check in "Export checks", and click "Open this trace in Langfuse". Checked on 2026-11-26 with check `f4b1507a-98ae-4bad-8fe6-14dfc53dad2f`.

## Alerts

Five rules in `alerts.tf` email the owner (contact point `aorte-owner-email`). They stay quiet while the stack is stopped (OBS-19): no data is OK, and "worker missing" needs the approval API to be reporting.

Fire tests (in the #133 session, stack running, deployed from `main`):

| Rule | How to trigger | Then |
| :-- | :-- | :-- |
| Worker reports no data while the API runs | on the host over SSM: `docker pause <worker container>` | `docker unpause`; it resolves |
| Worker scan is stale | on the host: `docker pause <odoo-db container>` for 4 minutes (scans fail, the worker keeps reporting) | `docker unpause` |
| Runtime state is not active | admin API: set `fallback_fixed_rule` with a reason | set `active` again |
| Held effects above 0, telemetry dropped at the daily cap | `alert_test_mode = true` in `terraform.tfvars`, plan and apply | set it back to `false`, plan and apply |

After the stack is stopped, wait at least 10 minutes and confirm that no rule fires.

## Teardown

Grafana objects are not AWS resources, so the AWS cleanup does not remove them (INF-8 lists them as such). Remove them with `scripts/tf_safe.sh deploy/terraform/grafana destroy-plan`, then `apply --confirm <plan-id>`; include this in the MVP teardown (#120).

## Limits

- Tempo search drops leading zeros from trace IDs, so links use the span attribute `aorte.trace_id` that the exporter adds (`F-095`). Traces exported before #139 have no such attribute and show no link.
- Metric names for LLM cost, effects, and drops are matched by regex until the first real data shows their unit suffix (`F-095`).
