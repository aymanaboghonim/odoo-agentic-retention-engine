# Course deliverables map

This project grew from the AI Architecture course (see [ACKNOWLEDGEMENTS.md](../ACKNOWLEDGEMENTS.md)). The course structure does not shape this repo. This folder maps course deliverables to their sources of truth; it does not duplicate their content.

## Repository layout

| Location | Purpose |
| :-- | :-- |
| [docs/](../docs/) | Vision, status, architecture, specifications, decisions, and verified facts |
| [docs/architecture/](../docs/architecture/) | System diagrams, data connections, and capacity estimates |
| [docs/adr/](../docs/adr/) | Architecture decision records |
| [docs/specs/](../docs/specs/) | Behavior, safety, security, and evaluation specifications |
| [scripts/](../scripts/) | Repository and traceability checks |
| [course/](./) | This course-to-source map |
| `src/engine/`, `evals/`, `tests/`, `deploy/` | Planned for implementation phases; not present yet |

## Deliverables

| Course deliverable | Source of truth | Status |
| :-- | :-- | :-- |
| Problem framing, goals, assumptions | [Vision](../docs/00-vision.md) | Documented; targets are untested |
| Building blocks and architecture views | [System overview](../docs/architecture/system-overview.md) | Documented; worker-owned execution is recorded in [ADR-0012](../docs/adr/0012-worker-owned-agent-execution.md) |
| Data connections and channels | [Data connections](../docs/architecture/data-connections.md) | Decided connections documented; ports, auth, and network layout remain open |
| Capacity and cost | [Capacity and cost estimate](../docs/architecture/capacity-cost-estimate.md) | Planning estimate; S1 and provider selection are still needed |
| LLM and RAG choices | [ADR-0003](../docs/adr/0003-llm-interface-and-model-selection.md), [ADR-0006](../docs/adr/0006-retrieval-store-benchmark.md) | Candidate interfaces and evaluation plans documented; final choices follow S2/S3 |
| Agent lifecycle and human approval | [Agent state machine](../docs/specs/agent-state-machine.md) | Spec documented; implementation pending |
| Discount and margin safety | [Financial safety gate](../docs/specs/safety-gate.md) | Rules documented; implementation tests pending |
| Security and threat model | [Data protection and AI threat model](../docs/specs/data-protection-threat-model.md) | Spec documented; legal and implementation questions remain open |
| Evals, release gates, rollback | [Evals and release gates](../docs/specs/evals-release-gates.md) | Spec documented; thresholds and fallback decisions remain open |
| Architecture decisions | [ADRs](../docs/adr/) | In progress; some choices wait for spikes |
| Capstone PDF and slides | Built from the documents above | Planned after the POC |
