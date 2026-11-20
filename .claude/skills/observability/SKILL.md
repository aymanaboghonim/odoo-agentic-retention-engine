---
name: observability
description: 'Use when adding or changing a span, metric, log line, or telemetry setting, when wiring an exporter (Grafana Cloud, Langfuse, the CloudFront relay, Alloy), or when checking free-tier usage. Gives the deny-by-default attribute allowlist, naming and label rules, the export caps and no-payment-method rule, the hermetic exporter test pattern, and the live export check.'
---

# Observability

Rules come from [ADR-0029](../../../docs/adr/0029-observability-topology-and-telemetry-data-rule.md) and the [observability spec](../../../docs/specs/observability-telemetry.md). The code is [telemetry.py](../../../src/engine/telemetry.py); the tests are [test_telemetry.py](../../../tests/test_telemetry.py).

## The data rule

Deny by default. Nothing about a customer leaves the engine: no `customer_id`, `cart_id`, partner field, destination or its hash, and no prompt, message, subject, cart, or product text. Only `run_id` and `proposal_id` identify work. The tokenized text stays in the engine DB; a span points to it by `proposal_id`.

Every exported span, log record, and resource is rebuilt from the allowlist in `telemetry.py`. Each kept value must pass its validator (UUID, enum, count, or a strict pattern). Exception events, links, and status descriptions are always dropped; only `error.type` (the class name) is kept.

## Adding a span attribute, metric, or label

1. Ask: could the value ever hold customer data or free text? If yes, do not add it. Use a code, an enum, a count, or an ID that points into the engine DB.
2. Add the key to `SPAN_ATTRIBUTES` (or `METRIC_LABELS`, `RESOURCE_ATTRIBUTES`, `LOG_ATTRIBUTES`) with the tightest validator. Never a validator that accepts any string.
3. New span names go in `SPAN_NAMES`; anything else is exported as `aorte.other`.
4. New metrics go in `METRICS` with kind, unit, and description.
5. Add or extend a test in `tests/test_telemetry.py` that shows the new key is exported and that an invalid value is dropped.
6. If the change maps to a spec invariant, name it next to the test.

## Naming

- Spans: `aorte.<stage>` (`aorte.scan`, `aorte.proposal`, `aorte.llm.call`, `aorte.gate`, `aorte.execution`, `aorte.effect`).
- Engine attributes and metrics: `aorte.*`. LLM fields use the OTel `gen_ai.*` names (`gen_ai.request.model`, `gen_ai.usage.input_tokens`). Errors use `error.type`.
- Services: `aorte-worker`, `aorte-approval-api`, `aorte-operator`.

## Metric labels

A closed set with closed values (OBS-8). Never a run, proposal, customer, or cart ID as a label: it would create a series per run and exhaust the free series allowance (`F-011`). Put IDs on spans. Gauges that read the database (`aorte.runs`, `aorte.execution.held`) are registered by the worker only.

## Calling the helpers

```python
from engine import telemetry

with telemetry.span("aorte.gate", **{"aorte.run_id": str(run_id)}) as span:
    decision = gate.evaluate(proposal, now=now)
    span.set("aorte.gate.outcome", decision.outcome.value)
telemetry.count(
    "aorte.execution.effects", 1, {"aorte.effect": "coupon", "aorte.outcome": "confirmed"}
)
```

Helpers never raise and do nothing when telemetry is off (OBS-5). Do not call the OTel API directly from engine modules. The gate (`policy`) and the LLM adapter (`proposal_provider`) never import telemetry, and `telemetry` imports no engine module (BND-5).

Log lines: only loggers named `engine*` or `root`, INFO and above, are forwarded. A body that looks like an email, phone, PII token, or credential is replaced by `[redacted]`. Keep log lines to codes and IDs anyway.

## Settings and entrypoints

`TELEMETRY_ENABLED` (`YES` or `NO`) is required by every entrypoint that calls `telemetry.configure_from_env`: the worker, the approval API, and `erp_cli`. With `YES`, `TELEMETRY_ENVIRONMENT`, `GRAFANA_OTLP_ENDPOINT`, `GRAFANA_OTLP_TOKEN`, and `TELEMETRY_DAILY_SPAN_CAP` are required; a partial set stops startup (OBS-6). Call `logging.basicConfig` before `configure_from_env`, or the root logger keeps its WARNING level.

The AWS rollout copies the current task definition's environment. A new required setting must be applied with `scripts/tf_safe.sh` before the next image rollout; `scripts/aws_mvp_rollout.sh` refuses otherwise.

## Free tiers and caps

- Never add a payment method to the Grafana Cloud or Langfuse account. Then nothing can be billed, whatever the vendors do above their limits; that behavior is not in official docs (`F-010`, `F-011`).
- `TELEMETRY_DAILY_SPAN_CAP` counts spans plus log records per process per UTC day. Set it so the processes together stay below 1/30 of the monthly allowance. Drops are counted in `aorte.telemetry.dropped` (OBS-13).
- Check usage against the allowances every month and in the Tier 2 close report (`#133`).
- Measured SDK overhead: about 20 MiB peak per process (`F-084`).

## Langfuse and run traces

- Pass `run_id=` to `telemetry.span` for a run's root span (proposal, approval, execution): the run UUID becomes the trace ID, so one run is one trace across processes (OBS-17, ADR-0032).
- Only `LANGFUSE_SPANS` reach Langfuse, under `TELEMETRY_DAILY_LANGFUSE_UNIT_CAP` (each span is a Hobby unit). Never add the scan or other high-rate spans there.
- LLM spans: `langfuse.observation.type=generation`, `gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.usage.cost`. Text (`langfuse.observation.input`/`output`) only if `telemetry.eval_text_enabled()`; it is tokenized, and the exporter still scans it.
- Operator decisions are `aorte.approval` events with the code only. Langfuse scores need its REST API (F-086) and are post-MVP.

## Health signals

The worker reports `aorte.worker.last_scan_age` and `aorte.runtime.state`; the API answers `/healthz` (loopback only, no auth, no data) for the container health check (OBS-16). Alerts on "no data" must stay quiet while the stack is stopped by design (ADR-0026); see #127.

## Hermetic test pattern

Tests use `InMemorySpanExporter`, `InMemoryMetricReader`, and `InMemoryLogRecordExporter` passed to `telemetry.configure`, then `force_flush()`. Only one configuration per process: always call `shutdown()` in a `finally` (the `capture` fixture does this). Scan the serialized export for the fixture names, email, phone, `{{PII_`, and IDs (OBS-2, OBS-3). Never call a live endpoint from a test.

## Live export check

Follow the preflight rules in [process.md](../../../docs/process.md):

1. Preflight: the endpoint host has an AAAA record (the AWS host has IPv6-only egress, ADR-0024), the settings parse, and the token is present (never printed).
2. Send one synthetic run and confirm its trace, metrics, and logs in the vendor UI.
3. Record what was checked and what was not.
