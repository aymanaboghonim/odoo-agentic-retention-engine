# POC-2 full-stack capacity and cost measurement

- Date: 2026-11-09
- Issue: #68
- Initial run: `d1cb8e32-adc1-4ab7-b166-3f9ca2bfd9b8` (synthetic cart 37)
- Recovery run: `92fd71be-6a00-4dff-ba84-ea9b10dcfa9a` (synthetic cart 38)
- Model: `gpt-6-luna`, reasoning effort `low` ([facts register](../../docs/facts-register.md))
- Host: Linux/WSL2, 8 logical CPUs

## Proposal accounting

The initial proposal completed generation, was rejected by the financial gate,
and used 487 input plus 147 output tokens. After the sampler race was fixed,
the owner-approved recovery completed with 487 input plus 108 output tokens;
the financial gate returned `auto_approve`. No approval/execution command was
run, and no coupon or message was created or sent.

At the engine's configured rates of USD 0.125 per million input tokens and
USD 0.50 per million output tokens, the combined estimated generation cost is
**USD 0.00024925**. The aggregate reservation was USD 0.009192, within the
approved USD 1 ceiling. These are engine estimates, not an invoice. Billing
treatment for the separate input-token count requests remains unknown (F-038).
Both durable claims are consumed; do not retry or reset the engine database.

## Resource measurements

| Stage | Samples | Max summed CPU | Max summed memory | Duration |
| :-- | --: | --: | --: | --: |
| Idle | 3 | 4.41% | 501.0 MiB | — |
| One bounded scan page | 3 | 6.49% | 501.1 MiB | 5.07 s |
| One authorized proposal | 18 | 70.46% | 2049.2 MiB | 53.08 s |

The original run completed but did not publish because a transient container
exited between `docker ps` and `docker stats`. The sampler was fixed to refresh
the container list on that race; the recovery report above completed. The
reported CPU is the sum of Docker per-container percentages (100% is one CPU).
Memory is the sum of Docker-reported per-container working-set values. Samples
are sequential snapshots, not simultaneous peaks.

This is one local synthetic sample, not a throughput, production sizing,
concurrency, host-wide resource, or billing result. It should not be generalized
beyond the reported host and workload.

- [Recovery Markdown report and raw samples](./results/issue68-92fd71be-6a00-4dff-ba84-ea9b10dcfa9a/README.md)
