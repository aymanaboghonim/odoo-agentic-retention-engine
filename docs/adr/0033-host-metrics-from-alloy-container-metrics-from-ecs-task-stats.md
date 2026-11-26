# ADR-0033: Host metrics from Alloy, container metrics from ECS task stats, restart policy

- Status: accepted (owner, 2026-11-26)
- Date: 2026-11-26
- Issue: #165
- Supersedes in part: [ADR-0029](0029-observability-topology-and-telemetry-data-rule.md) decision 3, only the sentence on Alloy ("A Grafana Alloy container in the core task collects host, container (all containers, Odoo included), and PostgreSQL metrics ..."). Engine APM and the rest of ADR-0029 stay. PostgreSQL metrics through Alloy stay planned (#166).
- Changes: [ADR-0024](0024-mvp-ecs-task-layout-and-cloudfront-origin.md) decision 4 (core task layout): the long-running containers get an ECS restart policy.

## Context

ADR-0029 planned one Alloy container for host, container, and PostgreSQL metrics. The usual way to get per-container metrics (cAdvisor or the Docker integration) needs the Docker socket, which gives full control of the host. The owner refused the socket on 2026-11-26.

Checked on 2026-11-26 (`F-098`, `F-099`):

- ECS gives every container of the task Docker stats for all containers of the task at `${ECS_CONTAINER_METADATA_URI_V4}/task/stats`, with no socket and no IAM permission. `/task` maps Docker IDs to task-definition names.
- In bridge mode Alloy sees only its own network namespace (tested: `eth0`, `lo`), and ECS sets the network mode per task, not per container. Host network counters would need a second task with host networking.
- The filesystem collector does not need the host root mounted: the container's overlay root reports the root volume it lives on (tested: same size and free bytes as the host `/`).
- `RestartCount` exists only when a container restart policy is enabled. Today an essential container that exits stops the whole task; the service then starts a new one.
- Alloy sends usage reports to Grafana unless started with `--disable-reporting`.

## Decision

1. **Alloy collects host metrics only.** One `alloy` container in the core task, image `grafana/alloy` pinned by digest and mirrored to ECR like the other third-party images (#106). It mounts the host `/proc` and `/sys` read-only, nothing else: no Docker socket, no host root, no host network, not privileged. Collectors: `cpu`, `meminfo`, `loadavg`, `filesystem`. A `keep` rule sends only an allowlist of metric names, the filesystem only for mount point `/`. No logs. It sends over OTLP to the same Grafana endpoint with the existing write token from SSM, runs with `--disable-reporting`, keeps its HTTP server on `127.0.0.1`, and has a hard memory limit set from a measurement.
2. **The worker exports container metrics from ECS task stats.** It reads `/task` and `/task/stats` when metrics are exported and sends per-container CPU time, memory in use, network bytes (received and sent), and restarts, with the container name as a label from a closed set (the core task's containers). Outside ECS the variable is absent and nothing is sent. A failed read is skipped, never retried in a loop, and never stops the worker.
3. **No host network metric.** Network traffic is measured per container (decision 2). A second task with host networking was rejected (below).
4. **Restart policy on the long-running containers.** `restartPolicy` is enabled with `restartAttemptPeriod` 300 seconds on `odoo-db`, `odoo`, `engine-db`, `worker`, `approval-api`, `mailpit`, `operator`, and `alloy`. A container that exits after running at least 300 seconds restarts in place, and `RestartCount` counts it. A container that exits sooner is not restarted, so a crash loop still stops the task and the service replaces it, as today. The one-shot containers (`initialize`, `model-sync`) get no restart policy: their `SUCCESS` condition gates the others.
5. **Alerting.** The "worker missing" alert also fires when the worker is absent while Alloy reports the host (#163 peer presence), so a stack whose API has also failed is no longer read as stopped.

## Alternatives

- **Docker socket for Alloy (cAdvisor or Docker integration):** standard and complete, but whoever controls the socket controls the host. Rejected by the owner.
- **Alloy in its own task with host network and host PID:** real host network counters, but one more task and service, more memory, and more privilege on the host. Rejected; per-container network covers what we need.
- **Container Insights (CloudWatch):** billed per metric, and the cluster has it disabled for cost (ADR-0020). Rejected.
- **Task restarts only, no restart policy:** no behavior change, but a single crash replaces the whole task (all databases and Odoo restart). Rejected by the owner in favor of restarts in place.

## Consequences

- Good: host, container, and restart signals without the Docker socket or new IAM permissions; about 60 more active series on the free plan (measured in #133).
- The worker gains one more job; if the worker is down, container metrics stop too, but the "worker missing" alert covers that.
- No host-wide network total; the per-container sums are close because all traffic goes through the task's containers.
- Restart behavior changes: a crash after 300 seconds restarts one container instead of the whole task. Not stated in the docs and checked live in #133: whether a failed health check restarts a container, and how `dependsOn` acts on a restart.
- One more image to mirror and keep up to date.
