# Data connections and data channels

Issue: #26. This page lists only connections that accepted ADRs already decide. The AWS MVP network is in [AWS MVP network](#aws-mvp-network); what is still open is in [Not decided yet](#not-decided-yet). Do not read a missing value as a default.

## Connections

| From | To | Purpose | Access rule | Decided in |
| :-- | :-- | :-- | :-- | :-- |
| Engine | Odoo database | Read abandoned carts and customer history | Read-only database role. Never writes. | [ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md) |
| Worker | Odoo API | Change state: coupon, campaign, cart | Only path that changes Odoo. Odoo applies business rules and taxes. | [ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md), [ADR-0012](../adr/0012-worker-owned-agent-execution.md) |
| Engine | AI database | Features, audit log, idempotency keys, checkpoints, approvals, vector chunks | Separate Postgres container with its own database and role | [ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md) |
| API and worker | Messaging channels | API accepts inbound events; worker sends approved messages | Through `ChannelPort` (Telegram first) and `EmailPort` (Mailpit in the POC). Only the worker dispatches messages. Local S4 transports are in [ADR-0017](../adr/0017-s4-local-channel-transport.md). | [ADR-0013](../adr/0013-poc-channels-and-whatsapp-verification.md), [ADR-0012](../adr/0012-worker-owned-agent-execution.md), [ADR-0017](../adr/0017-s4-local-channel-transport.md) |

### Local POC channel transports

- Telegram uses the HTTPS Bot API. Local inbound updates use `getUpdates`
  polling; the adapter rejects a configured webhook rather than changing it.
  The bot token is embedded in the API URL and must not appear in logs or
  errors. Normalized inbound events are handed to the API ingress boundary.
  Only the worker-owned dispatch path sends Telegram messages. See
  [F-039](../facts-register.md#f-039) and [ADR-0017](../adr/0017-s4-local-channel-transport.md).
- Local email uses SMTP to Mailpit at `127.0.0.1:1025`; the Mailpit UI/API is
  `http://127.0.0.1:8025`. Mailpit's defaults do not use SMTP encryption,
  SMTP authentication, or UI/API authentication. Both ports are bound to
  loopback and carry synthetic test data only. The pinned image and API
  contract are recorded in [F-040](../facts-register.md#f-040).
- These values and transports apply only to local POC verification. Production
  transport, authentication, and network topology remain undecided.
| Engine | LLM provider | Model calls | Through one `LLMClient` port. Only synthetic data is sent to cloud; PII is masked first. | [ADR-0003](../adr/0003-llm-interface-and-model-selection.md) |
| Engine | Retrieval store | Search product and policy text | Through a `Retriever` port. The store is a derived index, never the source of truth. | [ADR-0006](../adr/0006-retrieval-store-benchmark.md) |
| Engine | Observability services | Traces, metrics, LLM traces | Only synthetic data | [ADR-0007](../adr/0007-free-tier-observability.md) |

## AWS MVP network

Decided in [ADR-0024](../adr/0024-mvp-ecs-task-layout-and-cloudfront-origin.md) and [ADR-0027](../adr/0027-operator-container-in-core-task.md); checked live by the smoke (SM-1..SM-5, [spec](../specs/aws-mvp-deployment.md)).

| From | To | Protocol and port | Auth |
| :-- | :-- | :-- | :-- |
| Browser or CLI | CloudFront | HTTPS 443 (HTTP redirects) | Cognito access token (bearer) on `/api/*` |
| CloudFront | S3 UI bucket | HTTPS | Origin access control (OAC); the bucket blocks public access |
| CloudFront | approval-api | HTTP 8080 through a VPC origin into the private subnet | Security group allows only the CloudFront prefix list; the API also requires `X-Origin-Verify` (403 without it) |
| approval-api | Cognito JWKS | HTTPS (IPv6) | Public keys; the API checks signature, issuer, `client_id`, and `cognito:groups` |
| worker, approval-api, operator | engine-db | PostgreSQL 5432 on the task's bridge network | Engine role (password from SSM) |
| worker, operator | odoo-db | PostgreSQL 5432 on the bridge network | `odoo_ro`, read-only (SM-5) |
| worker, operator | Odoo | HTTP 8069 on the bridge network, JSON-2 | Named Odoo API key (SSM) |
| worker, operator | Mailpit | SMTP 1025 on the bridge network | None (synthetic mail only, not published on the host) |
| worker, operator | OpenAI, Telegram | HTTPS over IPv6 (egress-only internet gateway; no IPv4 internet path) | API key, bot token (SSM) |
| Owner | Odoo UI, Mailpit UI | SSM port forwarding to host ports 18069, 18025 | IAM (SSO); no security group rule |

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

- Production (beyond the single-host MVP) ports, protocols, authentication and network layout remain undecided. The MVP layout is in [AWS MVP network](#aws-mvp-network); local POC channel transport is in [ADR-0017](../adr/0017-s4-local-channel-transport.md).
- Odoo version is 19.0 and the API type is JSON-2 with a bearer API key, local only so far ([F-001](../facts-register.md), [ADR-0014](../adr/0014-odoo-19-baseline-and-seeding.md)). For the AWS MVP, ports, network layout, and the read-only role are in [AWS MVP network](#aws-mvp-network); production use is still open (Q11).
- Final LLM provider and model (spike S2) and retrieval store (spike S3).
- Embedding model. The draft assumes `BAAI/bge-m3` with FastEmbed, which is unverified ([F-007](../facts-register.md)).
