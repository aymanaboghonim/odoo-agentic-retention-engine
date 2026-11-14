# Course deliverables map

This project grew from the AI Architecture course (see [ACKNOWLEDGEMENTS.md](../ACKNOWLEDGEMENTS.md)). The course structure does not shape this repo. This folder maps course deliverables to their sources of truth; it does not duplicate their content.

## Repository layout

| Location | Purpose |
| :-- | :-- |
| [docs/](../docs/) | Vision, status, architecture, specifications, decisions, and verified facts |
| [docs/architecture/](../docs/architecture/) | System diagrams, data connections, and capacity estimates |
| [docs/adr/](../docs/adr/) | Architecture decision records |
| [docs/specs/](../docs/specs/) | Behavior, safety, security, and evaluation specifications |
| [scripts/](../scripts/) | Repository, evaluation, delivery review, and traceability scripts |
| [course/](./) | This course-to-source map |
| `src/engine/`, `evals/`, `tests/`, `deploy/` | Implemented for the POC-2 vertical slice and hermetic/live evaluations |

## Deliverables

| Course deliverable | Source of truth | Status |
| :-- | :-- | :-- |
| Problem framing, goals, assumptions | [Vision](../docs/00-vision.md) | Documented; targets are untested |
| Building blocks and architecture views | [System overview](../docs/architecture/system-overview.md) | Documented; worker-owned execution is recorded in [ADR-0012](../docs/adr/0012-worker-owned-agent-execution.md) |
| Data connections and channels | [Data connections](../docs/architecture/data-connections.md) | Local POC connections implemented ([ADR-0017](../docs/adr/0017-s4-local-channel-transport.md)); production topology and auth remain open |
| Capacity and cost | [Capacity and cost estimate](../docs/architecture/capacity-cost-estimate.md), [Full-stack measurement](../evals/full-stack/capacity-cost-estimate.md) | S1, S2, S3, and POC-2 full-stack local measurements complete; production sizing open |
| LLM and RAG choices | [ADR-0003](../docs/adr/0003-llm-interface-and-model-selection.md), [ADR-0015](../docs/adr/0015-postgresql-retrieval-store.md), [ADR-0016](../docs/adr/0016-s2-llm-evaluation-and-model-selection.md) | Resolved for POC (`gpt-6-luna` `low` + PostgreSQL FTS/`pgvector` 80:20 RRF) |
| Agent lifecycle and human approval | [Agent state machine](../docs/specs/agent-state-machine.md), [Approval API](../docs/specs/approval-api.md) | Spec and POC-2 implementation complete (#62, #66, #67) |
| Discount and margin safety | [Financial safety gate](../docs/specs/safety-gate.md), [ADR-0018](../docs/adr/0018-financial-safety-gate-decisions.md) | Spec, deterministic code gate, and CI/live tests complete (#56, #69) |
| Security and threat model | [Data protection and AI threat model](../docs/specs/data-protection-threat-model.md) | Spec documented; legal and production residency questions remain open (`Q12`) |
| Evals, release gates, rollback | [Evals and release gates](../docs/specs/evals-release-gates.md), [POC-2 evaluation](../docs/specs/poc2-end-to-end-evaluation.md) | Provisional POC-2 gates measured and approved (#69); final MVP gates open (`Q13`) |
| Architecture decisions | [ADRs](../docs/adr/) | ADR-0001 through ADR-0018 recorded |
| POC exit review and MVP handoff | [POC exit review](../docs/architecture/poc-exit-review.md) | Recorded in Phase 3 (#80); local static walkthrough tracked in #81 |
