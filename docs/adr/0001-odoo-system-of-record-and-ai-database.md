# ADR-0001: Odoo is a black-box system of record; the AI engine has its own database

- Status: accepted
- Date: 2026-10-24

## Context

We need a realistic, mature e-commerce/ERP system with connected data, without building one. The business problem is adding an AI layer to a system that already exists.

## Decision

- Use Odoo Community with demo data as the as-is system (version to verify, fact F-001).
- The AI engine reads Odoo data with a read-only database role (SQL) and calls the Odoo API for any action that changes state (coupon, campaign, cart). Business rules and taxes stay inside Odoo.
- The AI engine never writes into the Odoo database.
- AI-owned data lives in a separate Postgres container (pgvector image) with its own database and role: customer features (RFM, CLV, churn), action and audit log, idempotency keys, LangGraph checkpoints, approvals, and vector chunks.
- A small seed script adds aged abandoned carts only if the demo data lacks them.

## Alternatives

- Build our own shop and data: rejected, wastes time on non-AI work.
- Add AI tables inside the Odoo database (as the first draft said): rejected, breaks isolation and upgrades.

## Consequences

- Clean boundary, close to a real client engagement.
- One more database container (small RAM cost).
- Odoo demo data may need seeding. Spike S1 decides how much.
