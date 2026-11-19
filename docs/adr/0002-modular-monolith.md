# ADR-0002: Modular monolith with API and worker, hexagonal inside

- Status: accepted; the LangGraph stack entry and the import-linter gate superseded in part by ADR-0028
- Date: 2026-10-24

## Context

One developer. One budget VPS (4 vCPU, 8 GB RAM) shared with Odoo. Microservices would add network failures, deploy work, and tracing cost with no benefit at this scale.

## Decision

- One AI engine codebase (Python, FastAPI, LangGraph) with two entrypoints: `api` and `worker` (scheduler for cart scan and feature jobs).
- Modules with ports and adapters: `channels`, `rag`, `agent`, `policy`, `llm`, `features`, `odoo_adapter`.
- Module boundaries are enforced in CI (import-linter), not by convention.
- Odoo stays a separate system. The overall landscape is hybrid by nature.
- Extraction triggers to document later: the worker must scale on its own, a module needs a different runtime, or a team boundary appears.

## Alternatives

- Microservices: rejected for now, too much cost for the scale.
- Single script or notebook: rejected, not production-shaped.

## Consequences

- Simple deploy, simple tracing, easy local run.
- Discipline needed to keep boundaries. The CI gate provides it.
