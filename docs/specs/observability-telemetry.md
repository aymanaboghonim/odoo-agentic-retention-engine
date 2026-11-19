# Spec: PII-safe observability telemetry

- Issue: #125 (spec); implemented in #126, #127, #128, #129, #130, #134
- ADRs: [ADR-0029](../adr/0029-observability-topology-and-telemetry-data-rule.md), [ADR-0020](../adr/0020-aws-cloudfront-s3-edge-and-backups.md), [ADR-0024](../adr/0024-mvp-ecs-task-layout-and-cloudfront-origin.md), [ADR-0012](../adr/0012-worker-owned-agent-execution.md)
- Parent: [privacy spec](privacy-consent-retention.md) (`TM-1`, `TM-2`), [data connections](../architecture/data-connections.md) (`DC-3`)
- Status: draft

## Purpose

Give the owner traces, metrics, logs, APM, infrastructure metrics, LLM cost, and provider-billing drift from the AWS host and from evaluation runs in GitHub Actions (`Q20`), with no dependency on the owner's machine and without any customer identifier or text leaving the engine.

## Scope and non-goals

Scope: OpenTelemetry setup in the engine (versions in `F-080`); stage spans; metrics; OTLP logs; engine APM (FastAPI, SQLAlchemy/psycopg, and outbound HTTP auto-instrumentation with scrubbing); a Grafana Alloy container for host, container, and PostgreSQL metrics; the attribute allowlist and value scanner; client-side export caps; export to Grafana Cloud (`F-079`) and Langfuse Cloud (`F-078`); the CloudFront relay to Langfuse for the IPv6-only host; operator decisions as Langfuse scores; the daily billing reconciliation job. The evaluation workflow is in [its own spec](live-evaluation-and-deploy-gate.md).

Non-goals: archiving raw telemetry (ADR-0029); instrumenting Odoo, PostgreSQL, or Mailpit from inside (they are monitored from outside: container and PostgreSQL metrics through Alloy, and engine spans for each call to them); real customer data (`Q12`).

## Data rule

Deny by default. Only keys in the allowlist in `src/engine/telemetry.py` are exported, on spans, metrics, logs, and resources.

| Allowed | Never exported |
| :-- | :-- |
| `run_id`, `proposal_id`, run state, stage, duration, outcome | `customer_id`, `cart_id`, partner name, email, phone |
| model, token counts, estimated USD (`gen_ai.*`, `langfuse.*` usage fields) | destination address, chat ID, destination hash |
| gate decision code, effect type and outcome, held count | prompt, model output, message, subject, cart or product text |
| channel name (`telegram`, `mailpit`), scan mode, environment | tokens, keys, `Authorization`, pepper, Odoo or SSM values |
| HTTP route template, method, status; target service name | full URLs, query strings, the Telegram bot path, SQL parameters |
| operator decision code as a Langfuse score | operator identity, reason text |
| error class name (not the message) | exception messages and stack traces |

Metric labels are a closed set (stage, state, outcome, channel, model, environment). `run_id` and `proposal_id` are span attributes, never metric labels.

APM spans keep the SQL statement shape without parameters, and outbound HTTP spans keep only the host and route template. Alloy sends only metrics on its allowlist (host CPU, memory, disk, network; per-container CPU, memory, restarts; PostgreSQL up, connections, database size), not logs, and runs with a hard memory limit.

## Export caps

No payment method is ever added to either account (ADR-0029). The engine counts exported spans and Langfuse units per UTC day; at `TELEMETRY_DAILY_SPAN_CAP` or `TELEMETRY_DAILY_LANGFUSE_UNIT_CAP` it stops exporting until the next day and counts the dropped items in a metric. The caps are set below 1/30 of the monthly free allowance (`F-010`, `F-011`). What the vendors do above their limits is unknown (no official statement), so the caps must hold on their own.

The only exception: with `TELEMETRY_EVAL_TEXT=YES` and a run on a fixed synthetic evaluation dataset, the tokenized prompt and output go to Langfuse. ERP mode refuses this setting at startup.

## Settings

All fail closed. `TELEMETRY_ENABLED` must be `YES` or `NO`. With `YES`, every setting for each enabled exporter is required, and a partial set stops startup.

| Setting | Meaning |
| :-- | :-- |
| `TELEMETRY_ENABLED` | `YES` or `NO` |
| `TELEMETRY_ENVIRONMENT` | `local`, `eval`, or `aws-mvp`; a resource attribute |
| `GRAFANA_OTLP_ENDPOINT`, `GRAFANA_OTLP_TOKEN` | stack OTLP host (must have AAAA on AWS) and token (secret) |
| `LANGFUSE_OTLP_ENDPOINT`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | direct host, or the relay URL on AWS; keys are secrets |
| `TELEMETRY_RELAY_KEY` | secret header value for the relay (AWS only) |
| `TELEMETRY_EVAL_TEXT` | `YES` or `NO`; `YES` is refused in ERP mode |
| `TELEMETRY_DAILY_SPAN_CAP`, `TELEMETRY_DAILY_LANGFUSE_UNIT_CAP` | integers `>= 1`; required when telemetry is on |

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| OBS-1 | Only allowlisted keys leave in span, metric, log, and resource attributes; others are dropped before export. | `test_exporter_drops_non_allowlisted_attributes` |
| OBS-2 | A full hermetic run (English and MSA synthetic fixtures, ERP and synthetic modes) exports no value that matches an email, a phone, a fixture name, or `{{PII_`. | `test_full_run_telemetry_contains_no_pii` |
| OBS-3 | `customer_id`, `cart_id`, partner fields, and destinations are never exported; the channel is an enum only. | `test_customer_identifiers_are_never_exported` |
| OBS-4 | Text attributes are exported only with `TELEMETRY_EVAL_TEXT=YES` on a synthetic evaluation dataset; ERP mode with that setting fails startup. | `test_eval_text_refused_in_erp_mode` |
| OBS-5 | Exporter failure or slowness never changes a run outcome or delays a side effect (bounded queue, drop when full, no retry storm). | `test_exporter_failure_does_not_affect_run` |
| OBS-6 | Telemetry is off unless every setting for an exporter is present; a partial set fails startup. | `test_partial_telemetry_config_fails_closed` |
| OBS-7 | Tokens, keys, `Authorization`, and the pepper never appear in telemetry or logs. | `test_secrets_never_in_telemetry` |
| OBS-8 | Metric labels come from a closed set with no per-run IDs. | `test_metric_label_cardinality_is_bounded` |
| OBS-9 | The exported estimated USD for a call equals its settled `proposal_calls` estimate. | `test_cost_metric_matches_budget_ledger` |
| OBS-10 | The gate and the LLM adapter do not import telemetry. | `scripts/check_import_boundaries.py` rule BND-5 |
| OBS-11 | Every configured export host has an AAAA record before the AWS rollout. | smoke SM-6, `test_smoke_checks_export_host_ipv6` |
| RL-1 | The relay serves only `/lf-otel/*`, only to the Langfuse origin, with caching off, and returns 403 without the secret header. | `test_relay_behavior_policy` (python-hcl2), live `aws cloudfront test-function` |
| BR-1 | The billing job prints no amounts or keys; a missing provider day is `unknown`, never 0. | `test_billing_reconcile_output_and_missing_bucket` |
| OBS-12 | APM spans never carry a full URL, query string, the Telegram bot path, or SQL parameters. | `test_apm_spans_are_scrubbed` |
| OBS-13 | At a daily cap, export stops for the rest of the UTC day and the dropped items are counted; runs are not affected. | `test_daily_export_cap_stops_export_and_counts_drops` |
| OBS-14 | Alloy sends only allowlisted metrics, no logs, and its container has a hard memory limit. | `test_alloy_config_allowlist_and_memory_limit` |
| OBS-15 | An operator decision score carries the decision code only. | `test_operator_decision_score_has_code_only` |
| OBS-16 | The worker reports seconds since its last finished scan page and the runtime state (unreadable counts as `halted`); the API answers `/healthz` only to loopback clients, outside the edge prefix, with `ok` or `unavailable` and nothing else. | `test_worker_heartbeat_tracks_completed_scan_pages`, `test_health_gauges_report_liveness_and_runtime_state`, `test_unreadable_runtime_state_reports_halted`, `test_health_endpoint_is_loopback_only_and_reveals_nothing` |

## Behavior

- **Given** an ERP run with a fixture partner (English or Arabic name), **when** it goes through scan, proposal, gate, and execution with telemetry on, **then** the in-memory exporter holds spans for each stage with `run_id` and no identifier or text (OBS-1..3).
- **Given** the Grafana endpoint is down, **when** a run executes, **then** the run completes as without telemetry and the dropped-export count rises (OBS-5).
- **Failure:** `TELEMETRY_ENABLED=YES` with `GRAFANA_OTLP_TOKEN` missing stops startup with the setting named and its value not shown (OBS-6, OBS-7).
- **Abuse:** cart text containing `customer_id=…` or an email-shaped string reaches no attribute, because text keys are not allowlisted and the value scanner drops a matching value (OBS-1, OBS-2).
- **Abuse:** a request to the relay without the secret header returns 403 and never reaches Langfuse (RL-1).
- **Given** the daily billing job, **when** OpenAI returns no bucket for a day, **then** that day is `unknown` and the drift alert does not read it as 0 (BR-1).

## Risks

- Langfuse and Grafana behavior above the free limits is not stated in official docs (`F-010`, `F-011`). No payment method on either account, and the caps in code (OBS-8, OBS-13, OBS-14), so the risk does not depend on that behavior; usage is measured in #133.
- The relay may not forward the `Authorization` header; #129 checks it first, and the fallback is split mode (ADR-0029).
- The OTel Python logs API is still under `_logs`. Pin the version; stdout logs stay as the fallback.
- SDK and Alloy memory on the 4 GiB host is measured (#126, #134) before the AWS rollout; Alloy's limit comes from that measurement.
- Auto-instrumentation libraries may add attributes in a new version; the allowlist drops them (OBS-1), and versions are pinned.
- `approval-api` is an essential container, so three failed health checks make ECS replace the whole core task (the databases keep their data on the host volume). A database outage therefore restarts the task; `startPeriod` is 60 s. Watch restarts in #127 and #133.

## Implementation notes (#126)

Code: [telemetry.py](../../src/engine/telemetry.py) (allowlists, validators, safe exporters, caps, log handler, helpers), spans in [worker.py](../../src/engine/worker.py) (`aorte.scan`), [proposal.py](../../src/engine/proposal.py) (`aorte.proposal`, `aorte.llm.call`, `aorte.gate`), and [execution.py](../../src/engine/execution.py) (`aorte.execution`, `aorte.effect`); the gauges `aorte.runs` and `aorte.execution.held` in [full_stack_worker.py](../../src/engine/full_stack_worker.py). Tests: [test_telemetry.py](../../tests/test_telemetry.py) and BND-5 in [test_import_boundaries.py](../../tests/test_import_boundaries.py). Skill: `.claude/skills/observability/`.

- Entrypoints that configure telemetry: `engine.full_stack_worker`, `engine.approval_api`, and `engine.erp_cli`. Each requires `TELEMETRY_ENABLED`; Compose and Terraform set `NO` as the deployment default (the code has none). The rollout script refuses a task definition without it.
- `TELEMETRY_DAILY_SPAN_CAP` counts spans and log records together, per process. Three engine processes run on the AWS host, so the engine-wide daily total is at most three times the cap.
- Exports use bounded batch queues (2,048 items), 5 s batching, and a 10 s export timeout; metrics export every 60 s. A failed export is dropped and counted (`export_failed`).
- The resource is built from the allowlist only (`service.name`, `deployment.environment.name`, optional `service.version` from `ENGINE_VERSION`); environment-based resource detectors are not used.
- Measured overhead (`F-084`): about 20 MiB peak RSS per process.
- Health signals (OBS-16): gauges `aorte.worker.last_scan_age` (seconds; process start counts as the first beat, and a page skipped while halted beats too) and `aorte.runtime.state`; the approval API's `/healthz` checks the engine database with `SELECT 1`. The ECS task definition (Terraform, applied in #127) and Compose run it as the `approval-api` container health check. Grafana alerts on these signals, and how they stay quiet while the stack is stopped by design (ADR-0026), are #127.
- Not in #126: Langfuse export and OBS-4 (#128), the live Grafana export and SM-6 (#127), APM and Alloy (#134).

## Open questions

- Whether the OpenAI admin key can be limited to read-only usage and cost (checked in #130 when `F-036` is refreshed).
- Whether Grafana free alerting covers the drift alert (checked in #130).
