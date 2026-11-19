# POC Exit Review, Signoff Memo, and MVP Handoff

- Issue: #80
- Milestone: POC-3
- Date: 2026-11-14
- Status: signed off for POC scope; MVP prerequisites gated

## 1. Executive Summary & Signoff Decision

The **Odoo Agentic Retention Engine** Proof of Concept (Phases 0, 1, and 2) is complete. Across issues `#62` through `#69` (merged in PR `#79`), the repository proved a single, strictly bounded vertical slice on top of an isolated Odoo 19.0 Community instance:

1. **Read-only abandoned-cart detection**: cursor-persisted scanning against a dedicated read-only PostgreSQL role (`odoo_ro`) with zero direct SQL writes to Odoo ([spec](../specs/odoo-adapter.md)).
2. **Bilingual policy retrieval**: PostgreSQL FTS + `pgvector` hybrid retrieval with 80:20 dense-to-lexical RRF fusion over synthetic English and MSA Arabic policies ([ADR-0015](../adr/0015-postgresql-retrieval-store.md), [spec](../specs/policy-retrieval.md)).
3. **Structured LLM proposal with durable budget reservation**: provider-neutral proposal step (`gpt-6-luna`, `low` reasoning effort) with pre-generation token reservation, strict schema validation, and price binding ([ADR-0016](../adr/0016-s2-llm-evaluation-and-model-selection.md), [spec](../specs/agent-proposal.md)).
4. **Deterministic financial safety gate**: hard-coded enforcement of discount tiers (`auto` ≤ 10%, `human_approval` > 10% to 15%, `reject` > 15%), category minimum margin, one coupon per customer per cart per day, and daily discount budget ([ADR-0018](../adr/0018-financial-safety-gate-decisions.md), [spec](../specs/safety-gate.md)).
5. **Cryptographically bound human approval**: operator approval via loopback API and CLI bound to `snapshot_hash` and `gate_fingerprint`, with immediate pre-execution re-verification by the worker ([ADR-0012](../adr/0012-worker-owned-agent-execution.md), [spec](../specs/approval-api.md)).
6. **Idempotent worker-owned execution**: Odoo 19.0 JSON-2 `loyalty.program` / `loyalty.card` coupon creation (skipped for 0% discount offers), `mail.activity` logging, and localized English/MSA dispatch to Mailpit and a dedicated Telegram test chat ([spec](../specs/gated-execution.md)).

**Signoff verdict:** The POC is signed off as a **lean technical proof of architecture and safety controls**. It does **not** authorize cloud hosting, production deployment, or processing of real customer data until the Phase 3 / MVP blockers listed in Section 4 are resolved.

## 2. Consolidated POC Evidence

| Capability / Gate | Measured Outcome | Primary Evidence |
| :-- | :-- | :-- |
| Hermetic CI & E2E verification | 357 passed, 4 skipped (`make ci`), including 18 DB-backed end-to-end walking-skeleton tests | [poc2-end-to-end-evaluation.md](../specs/poc2-end-to-end-evaluation.md) |
| Live bilingual evaluation (`#69`) | 17/17 (100%) valid runtime contracts; 12/13 (92.3%) action conformance; 2/2 (100%) structured price checks | [evals/poc2/results/poc2-69-evaluation/README.md](../../evals/poc2/results/poc2-69-evaluation/README.md) |
| Financial safety gate (`#56`, `#69`) | 0 executable unsafe discounts across 127/132 S2 rerun and 4/4 hostile/over-cap POC-2 live cases | [safety-gate.md](../specs/safety-gate.md), [evals-release-gates.md](../specs/evals-release-gates.md) |
| Controlled synthetic delivery (`#67`, `#69`) | 4/4 owner-reviewed deliveries (`mailpit-en`, `mailpit-ar`, `telegram-en`, `telegram-ar`); 0 coupons created for 0% offers | [gated-execution-review.md](../runbooks/gated-execution-review.md), [poc2-end-to-end-evaluation.md](../specs/poc2-end-to-end-evaluation.md) |
| Retrieval benchmark (`#47`) | Recall@5 = 1.000 (EN and AR), P95 = 7.45 ms on 32-case synthetic corpus; peak container RSS = 26.8 MiB. **Correction 2026-11-19 (#113):** these figures are not in the linked report; it shows Recall@5 0.9187, search P95 2.192 ms (80 queries, 34 documents), pgvector container peak 83.0 MiB. | [ADR-0015](../adr/0015-postgresql-retrieval-store.md), [s3-benchmark.md](../../evals/retrieval/results/s3-benchmark.md) |
| Full-stack capacity sample (`#68`) | Idle: 501.0 MiB RAM (4.41% CPU); 1 scan page: 501.1 MiB (6.49% CPU, 5.07 s); 1 proposal: 2,049.2 MiB peak (70.46% CPU, 53.08 s) | [evals/full-stack/capacity-cost-estimate.md](../../evals/full-stack/capacity-cost-estimate.md) |
| Live evaluation spend (`#69`) | USD 0.002275 estimated generation spend (USD 0.082728 worst-case reserved) under USD 1.00 hard ceiling | [evals/poc2/results/poc2-69-evaluation/README.md](../../evals/poc2/results/poc2-69-evaluation/README.md) |

## 3. Explicit No-Go Boundaries (What the POC Does Not Authorize)

The following boundaries remain strictly enforced until superseded by MVP ADRs and specs:

1. **No production or cloud exposure**: The Compose topologies (`deploy/full-stack-compose.yml`, `deploy/poc2-mailpit-review-compose.yml`) are local development fixtures, not network security boundaries. No service may be exposed to the public internet without an approved production topology, TLS termination, and authentication design (Open Question `Q11`).
2. **No real customer data (PII)**: All databases, prompts, logs, and channel deliveries in the POC use synthetic `.test` domains and dedicated test chats. Processing real customer data is blocked on legal/residency review, retention/deletion workflows, and customer opt-in verification (Open Question `Q12`, [data-protection-threat-model.md](../specs/data-protection-threat-model.md)).
3. **No autonomous >10% discounts or model-decided money**: The LLM never decides financial eligibility. Any offer >10% up to 15% requires human operator approval bound to the current cart snapshot; >15% is hard-rejected in code ([safety-gate.md](../specs/safety-gate.md)).
4. **No dialect production claims**: The runtime contract supports `en` and `ar` (MSA). Exploratory dialect inputs were tested in `#69`, but dialect generation is not certified for customer contact (Open Question `Q15`).
5. **No unmonitored background spend**: The scheduled worker scans carts only and never calls paid LLM APIs or dispatches messages autonomously in the full-stack compose setup ([full-stack-worker.md](../specs/full-stack-worker.md)).

## 4. Prioritized MVP Handoff Plan

When transitioning from this POC to an MVP build, work must proceed in the following dependency order:

### Tier 1 — Security, Privacy, and Governance Prerequisites (Blockers before any real data or hosting)

1. **Data protection, residency, and consent spec (`Q12`)**:
   - Define applicable jurisdiction/residency region, PII minimization before LLM calls, retention TTLs, right-to-erasure workflow across the engine DB, and verified customer channel opt-in.
2. **Production network topology, authentication, and secrets (`Q11`)**:
   - Replace the single static operator bearer token (`#66`) with authenticated operator identity, RBAC, and expiring approval windows.
   - Define container network isolation, reverse proxy/TLS, and secret injection outside local `.env` files.
3. **Final numeric release gates and automated rollback switch (`Q13`)**:
   - Promote provisional POC-2 gates in [evals-release-gates.md](../specs/evals-release-gates.md) to production sample sizes, define the exact fixed-rule fallback, and implement an operator kill-switch / rollback state.

### Tier 2 — Operational Observability and Reconciliation

1. **Observability and cost reconciliation (`Q20`, ADR-0007)**:
   - Wire structured run/proposal telemetry to OpenTelemetry / Grafana Cloud and Langfuse (within verified free-tier limits and approved residency rules).
   - Add automated reconciliation between engine-estimated token spend and provider billing APIs (`F-038`, `F-046`).
2. **Cross-system reconciliation runbook and dead-letter handling**:
   - Because Odoo JSON-2 writes and external channel dispatch do not share a distributed transaction, add an operator reconciliation CLI/view for `execution_failed` or partial-effect journal entries.

### Tier 3 — Channel and Product Surface Expansion

1. **Production email and messaging adapters (`Q6`, ADR-0013)**:
   - Replace local Mailpit with a verified transactional email provider with webhook delivery/bounce tracking.
   - Evaluate WhatsApp Business API verification and cost prerequisites before replacing or augmenting Telegram.
2. **Lightweight operator approval UI (Cloudflare Pages candidate)**:
   - Build a minimal authenticated operator UI on top of the existing `/approvals` API contract (`#66`), keeping all execution worker-owned (`ADR-0012`). For the POC exit itself, stakeholder review uses the local-only static demo artifact ([poc-walkthrough.html](../demo/poc-walkthrough.html), [local-demo-walkthrough.md](../runbooks/local-demo-walkthrough.md); `#81`).
3. **Deferred retention use cases**:
   - RFM / CLV churn next-best-action and slow-moving inventory campaigns ([00-vision.md](../00-vision.md)) remain deferred until the abandoned-cart slice is operating under Tier 1 and Tier 2 controls.

## 5. Custom Agents Review (Deferred Item from `docs/process.md`)

`docs/process.md` scheduled a check at Phase 3 on whether custom Copilot agents are needed.

- **Decision at POC exit**: Keep the built-in modes (`Plan`, `Agent`, `Ask`) plus the repository skills in `.github/skills/` (`pr-workflow`, `adr`, `spec-writing`, `verify-facts`). Do not add custom agent profiles during POC exit; revisit only if MVP introduces a dedicated release workflow.
