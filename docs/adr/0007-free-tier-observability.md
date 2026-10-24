# ADR-0007: Free-tier observability, same stack for POC and MVP

- Status: accepted
- Date: 2026-10-24

## Context

Self-hosted Langfuse v3 needs several services (for example ClickHouse and Redis). That is heavy for 8 GB WSL2 and for a 4 vCPU / 8 GB VPS shared with Odoo. Cost per month (OPEX) and unit economics are part of the portfolio story.

## Decision

- Use OpenTelemetry in the engine. Send traces, metrics and logs to Grafana Cloud free tier (the LGTM stack, hosted).
- Use Langfuse Cloud free tier for LLM traces and evaluations.
- Use Cloudflare Pages for the UI and R2 for artifacts and backups.
- This is the target for the MVP too. Document limits and data region (F-010, F-011, F-013) and the monthly cost.
- Only synthetic data goes to these services.

## Consequences

- Very low RAM and cost.
- Data leaves the box. Real GCC customer data would need a self-hosted or in-region option. Document this as the production path.
