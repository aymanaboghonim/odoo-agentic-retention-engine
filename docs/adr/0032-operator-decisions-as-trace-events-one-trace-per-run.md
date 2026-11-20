# ADR-0032: Operator decisions as trace events, one trace per run

- Status: accepted (owner, 2026-11-20)
- Date: 2026-11-20
- Issue: #128
- Supersedes in part: [ADR-0029](0029-observability-topology-and-telemetry-data-rule.md) decision 5 ("operator decision codes (as Langfuse scores)") and [ADR-0031](0031-live-evaluation-workflow-and-fingerprint-deploy-gate.md) decision 5 ("exported as Langfuse scores"). The rest of both ADRs stays.

## Context

ADR-0029 and ADR-0031 planned to send operator approve and reject decisions to Langfuse as scores. Checked on 2026-11-20 (`F-086`): Langfuse creates scores only through its SDK or REST API, with a required `traceId`; its OTLP ingestion maps traces and observations, not scores. Sending scores would add a second client, a second network path from the approval API (which on AWS must also pass the IPv6 relay, #129), and the Langfuse secret key in the `approval-api` container.

A decision also happens in a different process and much later than the proposal, so with random trace IDs it would land in a separate trace and lose its link to the proposal.

## Decision

1. **One trace per run.** A run's root spans (proposal, approval decision, execution) use the run UUID as the trace ID (`telemetry.span(run_id=...)`, a custom ID generator). All spans of one run share one trace in Grafana and Langfuse, whichever process emits them. Spans that are not about a run (the scan) keep random trace IDs.
2. **The operator decision is an event observation.** The approval API emits `aorte.approval` with `langfuse.observation.type=event`, the run ID, and the decision code (`approve` or `reject`) only. No operator identity and no reason text. It travels on the same OTLP path and relay as every other span.
3. **Scores come later.** Real Langfuse scores computed from these events belong to the post-MVP quality and feedback loop (STATUS), where a batch job can use the REST API without touching the request path.

## Alternatives

- **Scores through the REST API from the approval path:** Langfuse-native, but it needs a second client, the relay must also forward `/api/public/scores`, and the approval container would hold the Langfuse secret key. Rejected for now.
- **Store a trace context per run in the database and join it later:** works, but adds a column and a migration for what a deterministic trace ID gives for free. Rejected.
- **Keep random trace IDs:** the decision and the execution would not link to the proposal. Rejected.

## Consequences

- Good: one trace shows the whole run; no new credential or network path.
- Langfuse shows the decision as an event, not as a score, so score-based analytics wait for the quality loop.
- A run trace can have several root spans (one per process); Grafana and Langfuse group them by trace ID.
