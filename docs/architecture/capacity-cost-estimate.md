# Capacity and cost estimate

Issue: #29. This is a planning estimate, not a deployment decision or a performance guarantee. Unmeasured resources and unselected services stay unknown rather than being guessed. Prices and limits are snapshots; check the linked official sources before spending.

## Planning scope

- **POC:** synthetic data, a single small shared host as a candidate, one engine API and scheduled worker, Odoo, PostgreSQL, and the selected retrieval implementation. This is for development and evaluation, not high availability.
- **Production:** no responsible capacity or cost estimate is possible yet. The region, data-residency requirements, traffic, availability target, retention, model, and retrieval store are open or await spikes S1-S3.

## Capacity

| Workload | Current planning assumption | Evidence and limitation |
| :-- | :-- | :-- |
| POC host | 4 vCPU / 8 GB RAM was proposed in the blueprint as a starting point. | Not measured. It is not yet established that Odoo, the AI database, API, worker and retrieval fit or perform adequately together. |
| Odoo plus demo data | Unknown. | Measure peak and steady memory, CPU and disk in S1; see [F-014](../facts-register.md). |
| Retrieval and embeddings | Unknown until S3 selects the store and embedding model. | Benchmark memory, latency and Recall@5 on the Arabic and English retrieval set ([ADR-0006](../adr/0006-retrieval-store-benchmark.md)). |
| Production | Not estimated. | The draft's “two pods on one node” is not a high-availability design: one node failure still removes both pods. Define availability and residency first, then size and price multiple failure domains. |

**S1 measurement:** deploy the intended synthetic Odoo demo data and POC services on the candidate host. Record peak and idle RAM, CPU, disk, and response latency during the abandoned-cart scan, feature refresh, retrieval, and an interactive run. If the workload approaches host capacity or misses the agreed latency target, size up or separate services before calling the setup viable. No benchmark has been run yet.

## Cost model

| Cost item | Current evidence | Estimate status |
| :-- | :-- | :-- |
| Compute host | Hetzner's official launch release gives a historical CX32 reference: 4 vCPU, 8 GB RAM, 80 GB disk and 20 TB traffic for €6.80/month or €0.0113/hour, excluding VAT. Its current overview lists category starting prices, not a confirmed current CX32 quote. | **Unknown current price.** Use the historical amount only as a comparison point; confirm the exact plan, region and rate in the console before provisioning. See [F-012](../facts-register.md). |
| Static frontend | Cloudflare Pages static-asset requests are free and unlimited. Pages Functions are billed under Workers instead. | $0 for static asset requests; not a complete frontend/domain cost. |
| Optional object storage | Cloudflare R2 Standard includes 10 GB-month storage, 1 million Class A and 10 million Class B requests each month; egress is free. Above those allowances the listed rates are $0.015/GB-month, $4.50/million Class A and $0.36/million Class B. | $0 while within the free tier; actual usage and whether R2 is needed are unknown. See [F-013](../facts-register.md). |
| LLM calls | Provider/model is selected after S2 ([ADR-0003](../adr/0003-llm-interface-and-model-selection.md)). The draft's $0-$5/month is not supported by a token-volume measurement. | **Unknown.** Calculate after model choice and golden-set measurement: `(input_tokens / 1,000,000 × input_rate) + (output_tokens / 1,000,000 × output_rate)`, multiplied by monthly runs and including retries. |
| Messaging | POC uses Telegram and local Mailpit; the MVP email provider is undecided ([ADR-0005](../adr/0005-channels-telegram-first-email-mailpit.md)). | POC provider spend is not estimated here. Verify provider charges and recipient/message limits before choosing a paid service. |
| Observability | ADR-0007 targets Grafana Cloud and Langfuse Cloud, with free-tier facts still unverified ([F-010](../facts-register.md), [F-011](../facts-register.md)). | **Unknown** until actual event volumes and free-tier eligibility/limits are checked. |
| Odoo and data services | POC can share the candidate host; separate services change the bill. | Included hosting cost cannot be calculated until S1 confirms whether co-location works. |

There is no reliable total monthly cost yet. The useful POC total will be the confirmed host charge plus measured LLM usage and any paid monitoring, email, domain, storage or backup services. Do not present the historic CX32 launch price or draft's model budget as a current bill.

## Not decided yet

- Current host plan, location and price. The historical CX32 quote is not a current offer, and Hetzner's listed locations do not establish the required production residency.
- Whether a single 4-vCPU / 8-GB host can run the POC workload; measure in S1.
- Final model, token usage, and LLM spend; decide and measure in S2.
- Retrieval implementation and its resource usage; benchmark in S3.
- Production availability topology, region, retention, and load.
- Email, observability, backups and domain costs at the intended usage.
