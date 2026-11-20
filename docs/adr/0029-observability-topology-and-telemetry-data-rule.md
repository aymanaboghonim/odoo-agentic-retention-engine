# ADR-0029: Observability topology and telemetry data rule

- Status: accepted; decision 5 (operator decision codes as Langfuse scores) superseded in part by [ADR-0032](0032-operator-decisions-as-trace-events-one-trace-per-run.md)
- Date: 2026-11-19
- Issue: #125
- Supersedes in part: [ADR-0020](0020-aws-cloudfront-s3-edge-and-backups.md) decision 1 (how telemetry reaches Grafana Cloud and Langfuse Cloud, and what it may carry). The vendors stay; the rest of ADR-0020 stays.
- Extends: [ADR-0024](0024-mvp-ecs-task-layout-and-cloudfront-origin.md) decision 4 and [ADR-0027](0027-operator-container-in-core-task.md): one more container (`alloy`) in the core task.

## Context

ADR-0020 keeps Grafana Cloud (free) and Langfuse Cloud (Hobby) for telemetry, with the synthetic and PII-tokenized rule (`TM-1`, `TM-2`). Re-checked on 2026-11-19:

- The free tiers are unchanged (`F-010`, `F-011`). No official page says what happens above their limits: unknown.
- The MVP host has IPv6-only egress (ADR-0024). Grafana's OTLP gateways have AAAA records, except `prod-eu-west-0` (`F-079`). Langfuse Cloud has **no AAAA record in any region** (`F-078`).
- GitHub-hosted runners are free on public repositories (`F-083`), so evaluation runs do not need the owner's machine.
- Self-hosted Langfuse needs about 21.5 GiB; the host has about 2.7 GiB available (`F-081`).
- No GCC region exists in either service (`F-010`, `F-011`).
- The Langfuse Python SDK adds four runtime dependencies; the plain OTel SDK exports to both services (`F-080`, `F-078`).
- Provider USD is only in the OpenAI organization Costs API, which needs an admin key and gives daily buckets (`F-036`).

## Decision

1. **SDK.** The engine uses the OpenTelemetry Python SDK and the OTLP/HTTP exporter (`F-080`). No Langfuse SDK; LLM spans carry `gen_ai.*` and `langfuse.*` attributes (`F-078`).
2. **Grafana Cloud** in `us-east-2`, reached directly over IPv6 from the AWS host. Traces, metrics, and logs. The stack's real OTLP host must have AAAA before rollout (smoke SM-6).
3. **Engine APM and infrastructure metrics.** OTel auto-instrumentation for FastAPI, SQLAlchemy/psycopg, and outbound HTTP, with URL and statement scrubbing (the Telegram bot token is in its URL path). A Grafana Alloy container in the core task collects host, container (all containers, Odoo included), and PostgreSQL metrics, with a curated metric allowlist and a hard container memory limit, measured before rollout. Odoo, PostgreSQL, and Mailpit are monitored from outside; they are not instrumented inside.
4. **Langfuse Cloud US** (`us.cloud.langfuse.com`). Evaluation runs export from GitHub Actions ([ADR-0031](0031-live-evaluation-workflow-and-fingerprint-deploy-gate.md)); local runs may export too, but nothing depends on the owner's machine. The AWS host exports through a CloudFront behavior `/lf-otel/*` on the existing distribution: a Langfuse origin, caching off, and a viewer-request CloudFront Function that requires a secret header. If the live check in #129 shows CloudFront cannot forward the request (for example the `Authorization` header), the relay is not built and the AWS host sends LLM spans to Grafana only (**split mode**). That outcome is recorded in STATUS and the facts register, not by editing this ADR.
5. **Data rule: metadata only, text by reference.** Telemetry carries only keys from an allowlist in code: `run_id`, `proposal_id`, run state, stage, durations, model, token usage, estimated USD, gate decision codes, operator decision codes (as Langfuse scores), effect outcomes, channel name, error class. It never carries `customer_id`, `cart_id`, partner fields, destinations, or message, prompt, or cart text. The tokenized text stays in the engine DB, which has the retention sweep and erasure. Exception: tokenized prompt and output may go to Langfuse only from runs on the fixed synthetic evaluation datasets with `TELEMETRY_EVAL_TEXT=YES`; ERP mode refuses that setting at startup.
6. **Free tiers without billing risk.** Never add a payment method to the Grafana Cloud or Langfuse Cloud account, so nothing can be billed. The engine enforces client-side caps that do not rely on the unknown behavior above the limits: a daily export budget for spans and Langfuse units (export stops at a set share of the monthly allowance, and dropped items are counted), a closed metric label set, and a curated Alloy metric list. Usage is measured in #133 and checked monthly.
7. **Region.** US for both services, close to the `us-east-1` stack. No personal identifier leaves the engine, so the region holds no customer data. Real customer data would still need an in-region or self-hosted option (`Q12`).
8. **Provider billing reconciliation** runs daily in GitHub Actions. The OpenAI admin key is a secret of the protected `aws-mvp` environment, never on the host or the owner's machine. The job pushes the daily provider USD to Grafana beside the engine's estimate, writes a daily aggregate JSON report to `reports/` in the backups bucket (lifecycle expiry), and prints only pass or fail.

## Alternatives

- **Self-host Langfuse on the MVP host:** about 21.5 GiB minimum against 2.7 GiB free (`F-081`). A second host would break the USD 13/month target. Rejected.
- **A Langfuse offer through AWS Marketplace:** the MVP uses no Marketplace products (STATUS, MVP decisions). Rejected.
- **CloudWatch / X-Ray instead of Langfuse:** dual-stack endpoints and SigV4 with the task role (`F-082`), but its pricing for this volume is not verified and it gives no LLM-specific views. Not chosen; it remains the AWS-native fallback.
- **NAT gateway for IPv4 egress:** bills every hour, also while stopped (ADR-0024). Rejected.
- **Evaluation runs only from the owner's machine:** a manual dependency on home bandwidth and setup. Replaced by GitHub Actions.
- **Tokenized text in every trace:** most useful for debugging, but the name scrubber has known gaps on short or unusual names ([privacy spec](../specs/privacy-consent-retention.md#risks)). Rejected.
- **Rely on the vendors' overage behavior:** not stated in any official doc. Rejected in favor of no payment method plus caps in code.
- **No infrastructure metrics:** the host has 4 GiB and runs eight containers; memory or disk pressure would go unseen. Rejected.
- **EU regions:** no legal gain while no personal data leaves; slightly further from the stack. Not chosen.
- **Billing check on the host or the owner's machine:** an org admin key on a demo host, or a manual step. Rejected.
- **Archive raw telemetry to S3:** the engine DB is the durable record, an archive is a second privacy surface, and the post-MVP cleanup deletes the bucket (ADR-0026). Only the daily aggregate report is kept.

## Consequences

- Good: no new host, no new vendor, about USD 0 extra, no local-machine dependency. "No raw PII in telemetry" is a CI invariant, not a promise ([observability spec](../specs/observability-telemetry.md)).
- Alloy costs host memory and one more mirrored image; its limit is set from a measurement.
- The relay is a public path. The secret header and the `/lf-otel/*` path limit keep abuse to, at worst, using up the CloudFront Always Free allowance (`F-065`).
- When a cap is reached, telemetry for the rest of the day is dropped and counted; runs are not affected.
- Debugging needs two steps: the span gives `proposal_id`, then the operator reads the tokenized text in the engine DB.
