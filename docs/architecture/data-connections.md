# Data connections and data channels

Issue: #26. This page lists only connections that accepted ADRs already decide. Ports, protocols, auth mechanisms and network layout are not decided yet and are listed in [Not decided yet](#not-decided-yet). Do not read a missing value as a default.

## Connections

| From | To | Purpose | Access rule | Decided in |
| :-- | :-- | :-- | :-- | :-- |
| Engine | Odoo database | Read abandoned carts and customer history | Read-only database role. Never writes. | [ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md) |
| Worker | Odoo API | Change state: coupon, campaign, cart | Only path that changes Odoo. Odoo applies business rules and taxes. | [ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md), [ADR-0012](../adr/0012-worker-owned-agent-execution.md) |
| Engine | AI database | Features, audit log, idempotency keys, checkpoints, approvals, vector chunks | Separate Postgres container with its own database and role | [ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md) |
| API and worker | Messaging channels | API accepts inbound events; worker sends approved messages | Through `ChannelPort` (Telegram first) and `EmailPort` (Mailpit in the POC). Only the worker dispatches messages. | [ADR-0013](../adr/0013-poc-channels-and-whatsapp-verification.md), [ADR-0012](../adr/0012-worker-owned-agent-execution.md) |
| Engine | LLM provider | Model calls | Through one `LLMClient` port. Only synthetic data is sent to cloud; PII is masked first. | [ADR-0003](../adr/0003-llm-interface-and-model-selection.md) |
| Engine | Retrieval store | Search product and policy text | Through a `Retriever` port. The store is a derived index, never the source of truth. | [ADR-0006](../adr/0006-retrieval-store-benchmark.md) |
| Engine | Observability services | Traces, metrics, LLM traces | Only synthetic data | [ADR-0007](../adr/0007-free-tier-observability.md) |

## Three data channels

Each channel has one job. Mixing them is a design error.

### 1. Read-only SQL

- Reads Odoo data for the abandoned-cart scan and for customer features (RFM, CLV, churn).
- Uses the read-only database role. It cannot change Odoo data.
- The worker runs the scan on a schedule ([ADR-0004](../adr/0004-batch-over-streaming.md)).

### 2. Odoo API

- Used only when the engine must change state: create a coupon, register a campaign, or convert a cart.
- Odoo keeps control of business rules and taxes.
- Only the worker invokes Odoo write operations. The API records triggers and approvals; the worker claims and processes ready runs ([ADR-0012](../adr/0012-worker-owned-agent-execution.md)).
- Every call goes through the safety gate and approval flow first ([safety-gate.md](../specs/safety-gate.md), [agent-state-machine.md](../specs/agent-state-machine.md)).

### 3. Retrieval (RAG)

- Finds product descriptions and policy text to ground customer messages.
- Sits behind the `Retriever` port. pgvector and Qdrant are both candidates until the retrieval benchmark ([ADR-0006](../adr/0006-retrieval-store-benchmark.md)).
- Indexes are rebuilt from source data. Odoo stays the source of truth.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| DC-1 | The engine never writes to the Odoo database. | Not implemented yet. Add a test that the Odoo database role has no write privilege when the adapter exists. |
| DC-2 | State changes in Odoo go only through the Odoo API adapter, after the safety gate. | Not implemented yet. Add an architecture test that only the adapter calls Odoo write methods. |
| DC-3 | Only synthetic data reaches cloud LLM and observability services. | Not implemented yet. Add a test for the PII masking step. |

These checks do not exist yet. They are recorded so the matching implementation issue adds them.

## Not decided yet

- Ports, protocols and authentication for each connection (the original draft lists examples, but they are unverified).
- Network layout, gateway, and what is public or private.
- Odoo version and API type ([F-001](../facts-register.md)).
- Final LLM provider and model (spike S2) and retrieval store (spike S3).
- Embedding model. The draft assumes `BAAI/bge-m3` with FastEmbed, which is unverified ([F-007](../facts-register.md)).
