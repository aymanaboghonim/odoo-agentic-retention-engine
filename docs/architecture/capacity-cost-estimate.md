# Capacity and cost estimate

Issue: #29. This is a planning estimate, not a deployment decision or a performance guarantee. Unmeasured resources and unselected services stay unknown rather than being guessed. Prices and limits are snapshots; check the linked official sources before spending.

## Planning scope

- **POC:** synthetic data, a single small shared host as a candidate, one engine API and scheduled worker, Odoo, PostgreSQL, and the selected retrieval implementation. This is for development and evaluation, not high availability.
- **Production:** no responsible capacity or cost estimate is possible yet. The region, data-residency requirements, traffic, availability target, retention, model, and retrieval store are open or await spikes S1-S3.

## Capacity

| Workload | Current planning assumption | Evidence and limitation |
| :-- | :-- | :-- |
| POC host | 4 vCPU / 8 GB RAM was proposed in the blueprint as a starting point. | Measured on local synthetic full stack in #68 ([full-stack capacity report](../../evals/full-stack/capacity-cost-estimate.md)): idle 501.0 MiB RAM (4.41% CPU), one bounded scan page 501.1 MiB RAM (6.49% CPU, 5.07 s), one authorized proposal 2,049.2 MiB peak RAM (70.46% CPU, 53.08 s). Fits within 8 GB RAM for sequential POC runs; not a concurrency or production sizing guarantee. |
| Odoo plus demo data | Measured 2026-10-29 (Odoo 19.0, PostgreSQL 15): about 192 MiB + 167 MiB RAM idle, about 0% CPU, 171 MB database and 10 MB filestore. Light read load did not raise RAM. | Install-time and worker-load peaks not measured in S1; full-stack sequential sample recorded in #68 ([F-014](../facts-register.md), [ADR-0014](../adr/0014-odoo-19-baseline-and-seeding.md), [full-stack capacity report](../../evals/full-stack/capacity-cost-estimate.md)). |
| Retrieval and embeddings | PostgreSQL FTS + `pgvector` with 80:20 RRF fusion selected in S3 ([ADR-0015](../adr/0015-postgresql-retrieval-store.md)). | Measured in #47 ([s3-benchmark.md](../../evals/retrieval/results/s3-benchmark.md)): Recall@5 = 1.000 (EN and AR), warm P95 = 7.45 ms, peak container RSS = 26.8 MiB on the 32-case synthetic corpus. |
| AWS MVP host | One `t3a.medium` (2 vCPU, 4,096 MiB, [F-063](../facts-register.md)) on ECS-on-EC2 ([ADR-0019](../adr/0019-aws-ecs-on-ec2-cloud-runtime.md)). | Measured 2026-11-18, idle after the end-to-end runs ([F-075](../facts-register.md)): 768 MiB used, 2751 MiB available; containers about 893 MiB (operator 445, Odoo 131); task reservations 2752 MiB; root disk 9.6 of 30 GiB. Fits with room for a rolling task revision. Peak during a proposal on this host was not measured. |
| Production | Not estimated. | The draft's “two pods on one node” is not a high-availability design: one node failure still removes both pods. Define availability and residency first, then size and price multiple failure domains. |

**S1 and POC-2 measurements (done for local synthetic scope):** see the table above and [evals/full-stack/capacity-cost-estimate.md](../../evals/full-stack/capacity-cost-estimate.md). Production concurrency and multi-node sizing remain open for MVP planning ([poc-exit-review.md](poc-exit-review.md)).

## Cost model

| Cost item | Current evidence | Estimate status |
| :-- | :-- | :-- |
| Compute host | Hetzner's official launch release gives a historical CX32 reference: 4 vCPU, 8 GB RAM, 80 GB disk and 20 TB traffic for €6.80/month or €0.0113/hour, excluding VAT. Its current overview lists category starting prices, not a confirmed current CX32 quote. | **Unknown current price.** Use the historical amount only as a comparison point; confirm the exact plan, region and rate in the console before provisioning. See [F-012](../facts-register.md). |
| Static frontend | Cloudflare Pages static-asset requests are free and unlimited. Pages Functions are billed under Workers instead. | $0 for static asset requests; not a complete frontend/domain cost. |
| Optional object storage | Cloudflare R2 Standard includes 10 GB-month storage, 1 million Class A and 10 million Class B requests each month; egress is free. Above those allowances the listed rates are $0.015/GB-month, $4.50/million Class A and $0.36/million Class B. | $0 while within the free tier; actual usage and whether R2 is needed are unknown. See [F-013](../facts-register.md). |
| LLM calls | Provider/model is selected after S2 ([ADR-0003](../adr/0003-llm-interface-and-model-selection.md)). The draft's $0-$5/month is not supported by a token-volume measurement. | **Unknown.** Calculate after model choice and golden-set measurement: `(input_tokens / 1,000,000 × input_rate) + (output_tokens / 1,000,000 × output_rate)`, multiplied by monthly runs and including retries. |
| Messaging | POC uses Telegram and local Mailpit; the MVP email provider is undecided ([ADR-0013](../adr/0013-poc-channels-and-whatsapp-verification.md)). | POC provider spend is not estimated here. Verify provider charges and recipient/message limits before choosing a paid service. |
| Observability | ADR-0007 targets Grafana Cloud and Langfuse Cloud. Verified free tiers: Grafana metrics 10k active series, logs/traces 50 GB ingest each, 14-day retention; Langfuse Hobby 50k units/month and 30-day data access ([F-010](../facts-register.md), [F-011](../facts-register.md)). | $0 only within provider free-tier limits. Actual event volume, eligibility, and whether data-region terms fit requirements remain to be checked. Langfuse lists no GCC region. |
| Odoo and data services | POC can share the candidate host; separate services change the bill. | Included hosting cost cannot be calculated until S1 confirms whether co-location works. |
| AWS MVP | Target `<= USD 13/month` with the nightly stop ([ADR-0019](../adr/0019-aws-ecs-on-ec2-cloud-runtime.md)); CloudFront stays in its Always Free allowance ([ADR-0025](../adr/0025-cloudfront-pay-as-you-go-always-free.md)). Measured LLM cost of the one live proposal: USD 0.00011513 (629 input, 73 output tokens, #97). | **Unknown until measured.** Cost Explorer daily cost (filter `Project=aorte-mvp`) after about 48 hours is recorded in #107. |

There is no reliable total monthly cost yet. The useful POC total will be the confirmed host charge plus measured LLM usage and any paid monitoring, email, domain, storage or backup services. Do not present the historic CX32 launch price or draft's model budget as a current bill.

## Not decided yet

- Current host plan, location and price. The historical CX32 quote is not a current offer, and Hetzner's listed locations do not establish the required production residency.
- Whether a single 4-vCPU / 8-GB host can run the POC workload; measure in S1.
- Final model, token usage, and LLM spend; decide and measure in S2.
- Retrieval implementation and its resource usage; benchmark in S3.
- Production availability topology, region, retention, and load.
- Email, backups and domain costs at the intended usage; observability costs if the verified free-tier limits are exceeded.
