# AWS MVP cost report and stopped state (#107)

Date: 2026-11-19. Account and budget email are not recorded here. Source data: AWS Cost Explorer (`get-cost-and-usage`, daily, usage before credits, grouped by the `Project` tag and usage type) and `scripts/aws_mvp.py status`. Facts: [F-076](../../../../docs/facts-register.md) (cost), [F-077](../../../../docs/facts-register.md) (Identity Center key), [F-074](../../../../docs/facts-register.md) (ECR footprint).

## Measured line items (`Project=aorte-mvp`)

| Day (UTC) | Usage type | Quantity | Cost (USD) |
| :-- | :-- | :-- | :-- |
| 2026-11-17 | `BoxUsage:t3a.medium` | 3.0 h | 0.113 |
| 2026-11-17 | `EBS:VolumeUsage.gp3` | 0.292 GB-month | 0.023 |
| 2026-11-18 | `BoxUsage:t3a.medium` | 11.06 h | 0.416 |
| 2026-11-18 | `EBS:VolumeUsage.gp3` | 1.000 GB-month | 0.080 |
| 2026-11-18 | ECR and S3 `TimedStorage-ByteHrs` | 0.204 GB-month | 0.014 |

CloudFront, Cognito, CloudWatch, SSM, and Budgets: USD 0.00. Credits covered all usage (net USD 0.00). All days were still marked "estimated" when read.

Measured rates: USD 0.0376 per running hour and USD 0.08 per GB-month of gp3. Both match the published prices (F-050); ECR USD 0.10 and S3 USD 0.023 per GB-month (F-052).

## Monthly projection (post-#106 footprint)

| Use | Running hours | Estimate (USD/month) |
| :-- | :-- | :-- |
| Stopped all month | 0 | about 2.6 |
| 8 hours a day | 243 | about 11.8 |
| Always on | 730 | about 30.1 |

Fixed part: EBS 30 GiB USD 2.40, ECR 1.63 GB USD 0.16, S3 about 2.3 GB USD 0.05. The USD 13/month target (ADR-0019) holds only with the nightly stop. Not included: CloudFront above its Always Free allowance (F-065) and LLM spend (one live proposal cost USD 0.00011513, #97).

## Untagged cost found and fixed

Untagged `KMS-Keys` in us-east-1 and us-west-2 (about USD 2/month): the IAM Identity Center multi-Region customer managed key and its replica. Identity Center now uses the free AWS owned key in us-east-1 only, and both keys are scheduled for deletion (F-077). The `aorte-spike-88` cost (USD 0.041 on 2026-11-17) was the #95 spike; its leftovers are inactive and free.

## Cleanup dry run and stopped state

- `scripts/aws_mvp_cleanup.sh` (dry run, no changes): 47 changes listed, mvp destroy plan with 75 deletes, plan policy OK (deletes only). Plan `30f68ac35314` was reviewed and then discarded (owner decision: keep the stack for Tiers 2 and 3).
- `scripts/aws_mvp_shutdown.sh --apply` at 10:43 UTC: instance `stopped`, core service desired 1 (restarts with the host), `status` estimate USD 2.60/month known.
- The full live teardown is #120 (end of the MVP).

## Limits

- Daily totals were still estimated, and the full stopped-day total for 2026-11-19 was not yet posted. The projection uses measured rates, not a full billing month.
- Whether a KMS primary key in `PendingReplicaDeletion` is billed is unknown (F-077); check Cost Explorer from 2026-11-20.
