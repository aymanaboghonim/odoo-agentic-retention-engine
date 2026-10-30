# ADR-0014: Odoo Community 19.0 baseline, no seed needed

- Status: accepted
- Date: 2026-10-29

## Context

Spike S1 ([#43](https://github.com/aymanaboghonim/odoo-agentic-retention-engine/issues/43)) had to choose the Odoo version, check the demo data and the built-in abandoned-cart reminder, and measure resources. Facts: [F-001](../facts-register.md), [F-002](../facts-register.md), [F-003](../facts-register.md), [F-014](../facts-register.md). Spec: [s1-odoo-baseline.md](../specs/s1-odoo-baseline.md).

## Decision

- Use Odoo Community 19.0 (`odoo:19.0`, pinned by digest) with PostgreSQL 15 and demo data, run by `deploy/docker-compose.yml` (profile `s1`, loopback only).
- Use the Odoo 19 JSON-2 API (bearer API key) for the worker and probes. The key must be global (no scope) and belong to an active user.
- Do not seed. Demo data already has two aged abandoned carts. The optional API-only fixture (`--seed-aged-cart`) stays available for Phase 2 if needed. Never seed by SQL.
- Treat the built-in reminder as an opt-in baseline: it is off by default, delay is 10 hours, the cron runs hourly.
- Capacity in S1 covers Odoo + PostgreSQL only (about 360 MiB idle). Full-stack capacity moves to Phase 2.

## Alternatives

- Odoo 18.0: has the reminder job, but is older. Odoo 20.0: its source explicitly disables the reminder job by default. 19.0 is the newest version that keeps the reminder job.
- PostgreSQL 17 or 18 instead of 15: not tested in S1. PostgreSQL 15 is the version in the official Odoo Docker Hub instructions (`postgres:15`, checked 2026-10-29, https://hub.docker.com/_/odoo), and Odoo 19.0 source sets a minimum of 13 (`MIN_PG_VERSION` in `odoo/release.py`). We chose it as the vendor-documented, conservative option. No performance or feature reason. A newer version may work but is unverified; test it before changing. This is the Odoo database only; the separate AI database (pgvector) has its own version choice in S3.
- Seeding carts by SQL: rejected, breaks the Odoo write boundary (DC-2).

## Consequences

- The Odoo 19 API differs from 17 and older docs; adapters must target JSON-2.
- Support/EOL dates for 19.0 are unverified. Recheck before MVP.
- Demo carts are old (768 h and 1464 h), so scenarios with recent carts need the fixture or time-shifted data.
- The peak figure is from a light read workload; recheck under the real worker load.
