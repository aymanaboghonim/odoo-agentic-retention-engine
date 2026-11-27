# Spec: PII-safe observability telemetry

- Issue: #125 (spec); implemented in #126, #127, #128, #129, #130, #134, #165, #166
- ADRs: [ADR-0029](../adr/0029-observability-topology-and-telemetry-data-rule.md), [ADR-0033](../adr/0033-host-metrics-from-alloy-container-metrics-from-ecs-task-stats.md), [ADR-0020](../adr/0020-aws-cloudfront-s3-edge-and-backups.md), [ADR-0024](../adr/0024-mvp-ecs-task-layout-and-cloudfront-origin.md), [ADR-0012](../adr/0012-worker-owned-agent-execution.md)
- Parent: [privacy spec](privacy-consent-retention.md) (`TM-1`, `TM-2`), [data connections](../architecture/data-connections.md) (`DC-3`)
- Status: draft

## Purpose

Give the owner traces, metrics, logs, APM, infrastructure metrics, LLM cost, and provider-billing drift from the AWS host and from evaluation runs in GitHub Actions (`Q20`), with no dependency on the owner's machine and without any customer identifier or text leaving the engine.

## Scope and non-goals

Scope: OpenTelemetry setup in the engine (versions in `F-080`); stage spans; metrics; OTLP logs; engine APM (FastAPI, SQLAlchemy/psycopg, and outbound HTTP auto-instrumentation with scrubbing); a Grafana Alloy container for host, container, and PostgreSQL metrics; the attribute allowlist and value scanner; client-side export caps; export to Grafana Cloud (`F-079`) and Langfuse Cloud (`F-078`); the CloudFront relay to Langfuse for the IPv6-only host; operator decisions as events in the run trace (ADR-0032); the daily billing reconciliation job. The evaluation workflow is in [its own spec](live-evaluation-and-deploy-gate.md).

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
| operator decision code (`aorte.decision`), as an event in the run trace (ADR-0032) | operator identity, reason text |
| error class name (not the message) | exception messages and stack traces |

Metric labels are a closed set (stage, state, outcome, channel, model, environment). `run_id` and `proposal_id` are span attributes, never metric labels.

APM spans keep the SQL statement shape without parameters, and outbound HTTP spans keep only the host and route template. Alloy sends only metrics on its allowlist (host CPU, memory, load, root disk; PostgreSQL up, connections, and database size, OBS-22), not logs, and runs with a hard memory limit. Per-container CPU, memory, network, and restarts come from ECS task stats, read by the worker (ADR-0033); there is no host network metric.

## Export caps

No payment method is ever added to either account (ADR-0029). The engine counts exported spans and Langfuse units per UTC day; at `TELEMETRY_DAILY_SPAN_CAP` or `TELEMETRY_DAILY_LANGFUSE_UNIT_CAP` it stops exporting until the next day and counts the dropped items in a metric. The caps are set below 1/30 of the monthly free allowance (`F-010`, `F-011`). What the vendors do above their limits is unknown (no official statement), so the caps must hold on their own.

The only exception: with `TELEMETRY_EVAL_TEXT=YES` and a run on a fixed synthetic evaluation dataset, the tokenized prompt and output go to Langfuse. ERP mode refuses this setting at startup.

## Settings

All fail closed. `TELEMETRY_ENABLED` must be `YES` or `NO`. With `YES`, the Grafana settings and the Langfuse settings are each all-or-none groups, at least one group is required, and a partial group stops startup. `TELEMETRY_EVAL_TEXT` may be omitted, which means the safe `NO`.

| Setting | Meaning |
| :-- | :-- |
| `TELEMETRY_ENABLED` | `YES` or `NO` |
| `TELEMETRY_ENVIRONMENT` | `local`, `eval`, or `aws-mvp`; a resource attribute |
| `GRAFANA_OTLP_ENDPOINT`, `GRAFANA_OTLP_TOKEN` | stack OTLP host (must have AAAA on AWS) and token (secret) |
| `LANGFUSE_OTLP_ENDPOINT`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | base URL ending in `/api/public/otel` (the relay URL on AWS, #129); keys `pk-lf-...` and `sk-lf-...` are secrets |
| `TELEMETRY_DAILY_LANGFUSE_UNIT_CAP` | integer `>= 1`; spans sent to Langfuse per process per UTC day (each is one unit, `F-010`) |
| `TELEMETRY_RELAY_KEY` | secret header value for the relay (AWS only); sent as `x-aorte-relay-key` by the Langfuse exporter. Needs the Langfuse settings; on `aws-mvp`, Langfuse settings without it fail at startup (#150) |
| `TELEMETRY_EVAL_TEXT` | `YES` or `NO`; `YES` is refused in ERP mode |
| `TELEMETRY_DAILY_SPAN_CAP`, `TELEMETRY_DAILY_LANGFUSE_UNIT_CAP` | integers `>= 1`; required when telemetry is on |
| `TELEMETRY_APM` | `YES` or `NO` (omitted means `NO`); `YES` needs the Grafana settings (#134) |
| `TELEMETRY_DAILY_APM_SPAN_CAP` | integer `>= 1`; APM spans per process per UTC day, separate from the span cap; required with `TELEMETRY_APM=YES` |

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
| RL-1 | The relay serves only `/lf-otel/*`, only to the Langfuse origin, with caching off, and returns 403 without the secret header. | `tests/test_lf_relay.py` (`test_relay_behavior_policy` with python-hcl2, function logic under Node), live `aws cloudfront test-function` |
| BR-1 | The billing job prints no amounts or keys; a missing provider day is `unknown`, never 0. | `test_billing_reconcile_output_and_missing_bucket` |
| OBS-12 | APM spans never carry a full URL, query string, the Telegram bot path, or SQL parameters. | `test_apm_spans_are_scrubbed` |
| OBS-13 | At a daily cap, export stops for the rest of the UTC day and the dropped items are counted; runs are not affected. | `test_daily_export_cap_stops_export_and_counts_drops` |
| OBS-14 | Alloy sends only allowlisted metrics, no logs, and its container has a hard memory limit. | `test_alloy_config_allowlist_and_memory_limit` |
| OBS-15 | An operator decision is an `aorte.approval` event in the run's trace and carries the decision code only (ADR-0032). | `test_operator_decision_event_has_code_only` |
| OBS-17 | A run's root spans use the run UUID as trace ID, so one run is one trace across processes; other spans keep random IDs. | `test_run_spans_share_one_trace_named_by_the_run` |
| OBS-18 | Langfuse receives only the run spans (never the scan), under its own daily unit cap; tokenized text only there and only with `TELEMETRY_EVAL_TEXT=YES`, after a contact and secret scan. | `test_langfuse_gets_run_spans_only_never_the_scan`, `test_langfuse_unit_cap_is_separate_from_the_grafana_cap`, `test_text_reaches_only_langfuse_and_only_when_enabled`, `test_text_with_a_contact_is_dropped_even_when_enabled` |
| OBS-19 | Alert rules stay quiet while the stack is stopped: every rule treats no data as OK, every rule is scoped to `aws-mvp`, and "worker missing" fires only while the approval API reports `aorte.process.up` or Alloy reports the host (peer presence, #163, ADR-0033). | `test_alert_rules_are_quiet_while_stopped`, `test_every_process_reports_up_for_peer_presence` |
| OBS-20 | On ECS the worker exports per-container CPU time, memory in use, network bytes, and restarts from the task metadata endpoint, only for the core task's long-running containers (closed label set), with no APM span for the reads; a failed or malformed read reports nothing and never raises; outside ECS nothing is registered, and a non-local endpoint fails closed. | `test_container_gauges_closed_labels`, `test_container_stats_failures_report_nothing`, `test_container_stats_off_outside_ecs_and_fail_closed`, `test_metadata_reads_make_no_apm_spans` |
| OBS-21 | The long-running containers have an ECS restart policy (`restartAttemptPeriod` 300 s); the one-shot `initialize` and `model-sync` have none (ADR-0033). | `test_restart_policy_on_long_running_containers` |
| OBS-22 | PostgreSQL metrics are only `pg_up`, connections, and size of the `engine` and `odoo` databases, per server (`aorte_server`), with no query text: the init container creates `aorte_monitor` with LOGIN and CONNECT only (no `pg_monitor`, no other role, cannot read tables or other sessions' SQL), its passwords come from SSM, and the exporter has no `stat_statements` (owner, 2026-11-27). | `test_alloy_postgres_allowlist_and_no_query_text`, `test_monitor_role_reads_metrics_only`, `test_monitor_passwords_both_or_neither_and_distinct` |
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
- SDK and Alloy memory on the 4 GiB host is measured (#126, #134, #165) before the AWS rollout; Alloy's limit comes from that measurement (`F-098`).
- The restart policy changes failure behavior (ADR-0033): not stated in the docs and checked live in #133 are whether a failed health check restarts a container and how `dependsOn` acts on a restart (`F-099`).
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

## Implementation notes (#127)

- [telemetry_check.py](../../scripts/telemetry_check.py) is the live check (OBS-11): settings, DNS (AAAA required for `aws-mvp`), then one span, metric point (`aorte.telemetry.check`), and log line through the production exporters. Tests: [test_telemetry_check.py](../../tests/test_telemetry_check.py). Smoke SM-6 runs it in the operator container.
- `GRAFANA_OTLP_TOKEN` is the base64 of `<instance-id>:<token>`; a raw `glc_` token gets 401 from Grafana, so the settings refuse it, and they refuse the SSM placeholder `unset`. Found on 2026-11-20: the first local check got 401 with the raw token and passed with the base64 value.
- Terraform `telemetry_settings` sets `TELEMETRY_ENABLED=YES`, `TELEMETRY_ENVIRONMENT=aws-mvp`, the us-east-2 endpoint, and `TELEMETRY_DAILY_SPAN_CAP=20000` for the worker, approval-api, and operator; the token comes from SSM `grafana-otlp-token`.
- Dashboards and alert rules as code, and keeping alerts quiet while the stack is stopped, are #139.

## Implementation notes (#128)

- Langfuse gets its own OTLP span exporter ([telemetry.py](../../src/engine/telemetry.py) `langfuse_span_exporter`: Basic auth of `public:secret`, header `x-langfuse-ingestion-version: 4`, `F-078`), filtered to `LANGFUSE_SPANS` with its own daily cap. Grafana and Langfuse are independent: either or both.
- Mapping: the LLM span is `langfuse.observation.type=generation` with `gen_ai.request.model`, `gen_ai.usage.input_tokens`/`output_tokens`, and `gen_ai.usage.cost` (the settled ledger amount, OBS-9); run root spans carry `langfuse.trace.name=aorte.run`.
- Tokenized prompt and output (`langfuse.observation.input`/`output`) are set only when `telemetry.eval_text_enabled()`; the Langfuse exporter keeps them only after a contact and credential scan (PII tokens may stay); the Grafana exporter always drops them.
- Operator decisions: `aorte.approval` event from the approval API (ADR-0032). Real Langfuse scores need its REST API (`F-086`) and are post-MVP.
- `scripts/telemetry_check.py --langfuse-key-file <file>` sends one `aorte.check` event to Langfuse (the key file holds one line `<public>:<secret>`).
- Evaluation runs export from GitHub Actions in #135; the AWS host reaches Langfuse only through the relay (#129).
- Live check 2026-11-20 from the owner's machine to Langfuse US (project `aorte-mvp`): export accepted; the owner saw trace `506fef5973b543dfad4627ea196e6ee7` (the check ID without dashes) named `aorte.check`, one event observation, environment `local`, only allowlisted attributes, no input or output, no scores. The preflight again found no AAAA record for `us.cloud.langfuse.com` (`F-078`).

## Implementation notes (#139)

- Dashboard as code: Terraform root `deploy/terraform/grafana` (`grafana/grafana` 4.55.0, `F-094`), state in the S3 backend under its own key, applied only through `scripts/tf_safe.sh` (owner decision 2026-11-26). The provider takes `GRAFANA_URL` and `GRAFANA_AUTH` (a stack service account token, not the OTLP token) from the environment; the Langfuse project ID is a variable in the gitignored `terraform.tfvars`. Steps: [grafana runbook](../runbooks/grafana.md).
- Trace links: Tempo search drops leading zeros from trace IDs (`F-095`), so the exporter adds `aorte.trace_id` (32 hex, allowlisted; set by the exporter on run spans, a caller's value is overwritten) and the trace tables link `https://us.cloud.langfuse.com/project/<project-id>/traces/<aorte.trace_id>`. Test: `test_run_spans_carry_the_padded_trace_id_for_the_langfuse_link`; dashboard tests in `tests/test_grafana_dashboard.py`.
- Applied 2026-11-26 (plans `8d54fbcc25ba` folder, `b21c9866f179` dashboard; no drift). A local `telemetry_check.py` trace exported to both, and one click in the dashboard opened it in Langfuse.
- Alert rules and how they stay quiet while the stack is stopped (peer presence, owner decision 2026-11-26) are #163.

## Implementation notes (#134)

- APM with `TELEMETRY_APM=YES`: FastAPI (the approval API, `instrument_fastapi`; `/healthz` and the ASGI send/receive spans excluded), psycopg (all engine and Odoo-reader SQL; SQLAlchemy runs on it), and urllib (Odoo, OpenAI, Telegram, Mailpit calls). No SQLAlchemy instrumentation: its 0.66b2 release requires SQLAlchemy below 2.1 (`F-097`). Stable semantic conventions (`OTEL_SEMCONV_STABILITY_OPT_IN=http,database`).
- Scrubbing (OBS-12): APM span names are kept only as an HTTP method with an optional route template, or an SQL keyword with an optional database name; anything else becomes `aorte.other`. Allowlisted attributes: method, status code, route template, scheme, server address and port, `db.system.name`, `db.namespace`, and `db.query.text` (the statement shape; parameters are never captured, and a value the PII and secret scanner flags is dropped). Never the full URL, path, query, headers, or client address; the Telegram bot token is in the URL path, so it never leaves.
- APM spans count against `TELEMETRY_DAILY_APM_SPAN_CAP` (30,000 on AWS), never the engine span cap, and never reach Langfuse. Instrumentation metrics pass through OTel Views: only `http.server.request.duration` survives, with `http.route`, `http.request.method`, and `http.response.status_code` (OBS-8); every other `http.*` and `db.*` instrument is dropped.
- Measured locally (`F-097`): about 6 to 7 MiB more peak RSS per process. Grafana: an "API (RED) and database" row (requests, 5xx ratio, P95 per route, slow SQL). The live check from the host is in #133.

## Implementation notes (#163)

- Five Grafana-managed rules in `deploy/terraform/grafana/alerts.tf` (group `aorte-engine`, folder `aorte`, every 60 s): worker scan stale (age above 3 scan intervals, 180 s, for 2 min), worker missing (API up and no worker data, for 5 min), held effects above 0, runtime state not `active`, telemetry dropped at the daily cap; since #132 a sixth, held run closed unverified (`aorte_reconcile_resolutions` with `aorte_resolution="unverified"` in the last hour). Email contact point `aorte-owner-email`, attached on each rule; the stack-wide notification policy is not managed here.
- Quiet while stopped (owner decision 2026-11-26, peer presence): the stop scripts and the nightly scheduler stop the whole instance, so nothing reports; no data is OK. Every engine process reports `aorte.process.up` (`aorte_process_up_ratio` in Grafana), so a worker that stops reporting while the API runs is told apart from a stopped stack. Since #165 Alloy's host load is a second peer, so a failure of both engine processes while the instance runs also fires.
- Free plan only (owner, 2026-11-26; `F-096`): few rules, one email contact point, no trial-only products. The trial's knowledge graph created 617 rules of its own (`F-096`); nothing here depends on them.
- Applied 2026-11-26 (plan `84b34b96fa7c`, no drift). With the stack stopped, all five rules were `Normal (NoData)` from 16:48:50 to 16:53:50 UTC.
- `alert_test_mode` (default false) lowers the held-effects and drops thresholds for the #133 fire tests; it is applied and reverted through `tf_safe.sh`. Fire-test steps: [grafana runbook](../runbooks/grafana.md#alerts).

## Implementation notes (#165)

- ADR-0033. Alloy `v1.22.0` (`F-098`), mirrored to ECR as `alloy`; config in `deploy/alloy/config.alloy`, passed to the ECS container in `ALLOY_CONFIG` and written to a tmpfs at start; `--disable-reporting`; read-only root; host `/proc` and `/sys` read-only. Collectors `cpu`, `meminfo`, `loadavg`, `filesystem`; allowlist of 7 metric names, CPU modes `idle`, `user`, `system`, `iowait`, `steal`, filesystem `/` only; resource keys `service.name=aorte-alloy` and `deployment.environment.name`. About 16 series on the 2-vCPU host.
- The root disk is read through Alloy's own overlay root, which reports the volume it lives on (measured: same size and free bytes as the host `/`), so the host root is not mounted.
- `engine.container_stats` (worker only): `aorte.container.cpu.time` (s, counter), `aorte.container.memory.usage` (By, usage minus inactive page cache, like `docker stats`), `aorte.container.network.io` (By, counter, `aorte.direction` rx or tx), `aorte.container.restarts` (gauge, only when ECS reports `RestartCount`), label `aorte.container`. One read of `/task` and `/task/stats` per export (cached 15 s, 2 s timeout), outside APM. About 40 series.
- Restart policy `{enabled = true, restartAttemptPeriod = 300}` on the 8 long-running containers. Compose has the Alloy service under the `observability` profile; Compose has no restart policy change.
- Alloy memory (`F-098`): peak 61.0 MiB locally over 15 minutes with the full pipeline; hard limit 128 MiB (`alloy_memory_mib`). The `mvp` plan `9f8f794a1197` (Alloy container, ECR repository, restart policy, and the #134 APM settings; 3 to add, 1 replaced) was reviewed on 2026-11-26 and is re-planned and applied in #133 before the rollout.
- Grafana: a "Host and containers" row (host CPU busy, memory available, root disk free, load; container CPU, memory, network, restarts) and the second peer in "worker missing"; plan `1cd7db92b390` applied 2026-11-26, no drift. Live checks in #133.

## Implementation notes (#166)

- Owner decision 2026-11-27: the monitoring role gets no `pg_monitor`. A local test (`F-100`) showed a LOGIN and CONNECT role gets the same three metrics, cannot read tables, and sees other sessions' SQL text as `<insufficient privilege>`; the extra `pg_monitor` metrics (locks, replication, activity per state) are not needed for the MVP. A later need for them is one grant in `full_stack_init` plus a decision record.
- `full_stack_init.configure_monitor_role` creates or updates `aorte_monitor` on `odoo-db` (database `odoo`) and `engine-db` (database `engine`) on every start (the password follows SSM; connection limit 3; it fails if the role is a member of any role). Passwords: SSM `engine-monitor-password` and `odoo-monitor-password` (ephemeral, write-only, INF-5). Locally both are optional (both or neither); without them no role is made and Alloy reports `pg_up` 0.
- Alloy: one `prometheus.exporter.postgres` per server (collectors `database` and `stat_database`, no settings metrics), an allowlist of `pg_up`, `pg_database_size_bytes`, `pg_stat_database_numbackends` for the application databases only, label `aorte_server` (`engine-db` or `odoo-db`) from the credential-free instance label. 6 series. Alloy has links to both servers and no `dependsOn`, so host metrics never wait for the databases. Alloy memory with both exporters: peak 75.0 MiB over 15 minutes locally (`F-100`), so the 128 MiB limit stays (1.7 times the peak); it is measured on the host in #133.
- Grafana: a "PostgreSQL" row (up, connections, size per server); plan `46ebd1b5bc63` applied 2026-11-27, no drift. The `mvp` plan `a3578971018a` (2 new SSM parameters, existing secrets unchanged, plus the #165 changes) was reviewed on 2026-11-27 and is re-planned and applied in #133.

## Open questions

- Whether the OpenAI admin key can be limited to read-only usage and cost (checked in #130 when `F-036` is refreshed).
- Whether Grafana free alerting covers the drift alert (checked in #130).
