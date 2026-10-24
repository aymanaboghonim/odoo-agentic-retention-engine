# Vision

Status: living document. Change it by pull request. Numbers marked "target" are not tested yet.

## One sentence

Add an agentic AI layer on top of an existing, mature e-commerce/ERP system, to fix real business pain points, without rebuilding that system.

## Context

The business already runs a stable e-commerce and ERP system. We use **Odoo Community** with its demo data as a realistic stand-in. This is the "as-is" system. We treat it as a black box and as the system of record.

Odoo version, licence, and what the demo data contains are facts to verify (see [facts-register.md](facts-register.md), spike S1).

## Pain points (as-is)

1. **Static offers.** Every customer sees the same offers. No segmentation.
2. **Abandoned carts.** No smart, personal follow-up. Odoo has a generic reminder email only. We use it as the baseline to beat.
3. **Churn and cold start.** No next-best-action based on RFM (recency, frequency, monetary) or CLV (customer lifetime value).
4. **Slow campaigns.** Marketing spends a long time matching slow-moving products to customer segments.

## Goals

- Business goal (target, untested): raise CSAT from 60% to 80% with personal, timely contact.
- Safety goal: 0% breaches of the discount cap and margin rules. This is enforced in code, not by the model.
- Cost goal: run the MVP on one budget VPS (4 vCPU, 8 GB RAM, about 10 USD per month) with free-tier observability. No GPU in the MVP.
- Sovereignty goal: the LLM layer supports Azure OpenAI (for GCC data residency) and other providers through one interface.

### Quality targets (target, untested)

| Target | Value | Note |
| :-- | :-- | :-- |
| Availability | 99.8% | MVP target on one VPS. Must be re-checked against the real design. |
| P95 latency, hybrid search API | under 500 ms | Excludes LLM generation. |
| P50 latency, precomputed features | under 100 ms | |
| Peak throughput | 1000 RPS | Blueprint number. Likely not realistic on one VPS. Re-evaluate before MVP. |
| Discount cap breaches | 0% | Hard gate in code and CI. |
| Retrieval Recall@5 | at least 90% | Measured on a golden set. |

## Non-goals

- Refunds and returns management.
- Rebuilding Odoo, the shop, or payment.
- Heavy streaming stack (Kafka, Flink, Debezium, Airflow) in running code. It is documented as the to-be design only.
- Paid WhatsApp Business setup in the POC.

## Approach

- **POC first.** One vertical slice: abandoned cart, then agent, then a policy-grounded message sent by Telegram and email.
- **MVP second.** Hetzner VPS, domain, Cloudflare Pages UI, R2 storage, WhatsApp adapter, churn next-best-action.
- **Architecture.** A modular monolith (API + worker) next to Odoo. Hexagonal inside. See ADR-0002.
- **Data.** Odoo's database is read-only for us. The AI engine has its own database. See ADR-0001.
- **Language.** Documents are in English. Arabic is required for customer-facing UI, messages, and test data. Arabic RTL support is not negotiable. See ADR-0008.
- **Process.** Spec-driven, mechanical gates, full traceability. See [process.md](process.md).

## Roadmap

See [STATUS.md](STATUS.md) for the live checklist. Phases: 0 repo foundation, 1 spikes, 2 POC, 3 POC exit review and MVP plan.

## Acknowledgement

See [ACKNOWLEDGEMENTS.md](../ACKNOWLEDGEMENTS.md). Special thanks to Eng. Sameh Amin (https://github.com/samehamin), the course instructor.
