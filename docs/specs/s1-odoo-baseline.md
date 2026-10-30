# Spec: S1 Odoo baseline

- Issue: #43
- ADRs: [ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md)
- Status: agreed

## Purpose

Resolve the Odoo Community version and setup needed for the POC, inspect its synthetic demo data and abandoned-cart reminder baseline, and measure Odoo plus PostgreSQL resource use for local development.

## Scope and non-goals

Scope: official version and licensing evidence; local Odoo 19.0 Community and PostgreSQL startup; inspection of products, customers, carts, and reminder settings; a synthetic cart fixture only if demo data is insufficient; and reproducible Odoo/PostgreSQL resource measurements.

Non-goals: agent runtime implementation, retrieval or LLM services, production sizing, real customer data, and changes to a real or shared Odoo instance.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| S1-INV-1 | Any optional seed creates synthetic records only through Odoo's supported API in the isolated local development instance; it never writes through SQL. | Seed contract tests and review of the seed transport; the target-guard test rejects non-local endpoints. |
| S1-INV-2 | A seed or probe never silently succeeds after an Odoo/API error or an unsafe target check. | Tests assert explicit failure for connection, authentication, and target-validation errors. |
| S1-INV-3 | S1 capacity evidence measures Odoo and its PostgreSQL service only; it does not claim full-stack POC capacity. | The recorded measurement command and service list in F-014 and the capacity estimate. |

## Behavior

- **Given** local Odoo starts with demo data, **when** the probe inspects products, customers, carts, website settings, and scheduled jobs, **then** it records observed values without changing source records.
- **Given** the demo data lacks the needed aged synthetic carts, **when** the optional fixture runs against the isolated local Odoo API, **then** it creates repeatable synthetic carts and reports the created records.
- **Given** the target is not a local development endpoint, **when** the fixture is invoked, **then** it fails before making a request that could write data.
- **Given** Odoo is unavailable or rejects the request, **when** the probe or fixture runs, **then** it reports failure and does not fall back to direct SQL or another target.

## Risks

- Odoo tags and behavior can change; record the exact selected image tag/digest and official source URLs with the check date.
- Demo data may not match the POC's business cases; inspect it before adding a fixture.
- Container measurements depend on host load and run duration; record the method and conditions and do not treat them as production guarantees.

## Open questions

- Do the selected version's demo records and reminder settings cover the required S1 cases, or is an API seed fixture needed?
- What repeatable local workload and observation window should produce the idle and peak resource measurements?
