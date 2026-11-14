# ADR-0019: 1st-party AWS ECS-on-EC2 cloud runtime

- Status: accepted
- Date: 2026-11-14
- Issue: #84

## Context

The POC exit review ([poc-exit-review.md](../architecture/poc-exit-review.md)) gates cloud hosting on resolving production network topology, secrets, and multi-operator RBAC (`Q11`), privacy and consent (`Q12`), and release gates (`Q13`). The MVP portfolio deployment targets a production-grade, 1st-party AWS architecture under a strict `<= USD 13/month` recurring OPEX ceiling, using only 1st-party AWS services and official AMIs (no AWS Marketplace products, no Route 53 domain registration or transfer, and no upfront commitments).

Local full-stack measurement (`F-047`) recorded `501.0 MiB` idle RAM across 6 containers and a **`2,049.2 MiB` (`~2.00 GiB`)** working-set peak during a single ONNX embedding + LLM proposal run. Meanwhile, `ADR-0001` requires a separate read-only Odoo SQL role (`odoo_ro`) alongside the Engine database.

## Decision

1. **Provision with Terraform (`hashicorp/aws`) on Amazon ECS (EC2 launch type):** run a single **`4 GiB` 1st-party ECS-Optimized Amazon Linux 2023 instance** (`t3a.medium` x86_64, 2 vCPU / 4 GiB, `$0.0376/hr` in `us-east-1`, `F-050`) so existing `linux/amd64` CI runners (`ubuntu-latest`) and pinned container digests (`F-051`) work natively without cross-arch emulation (`t4g.medium` ARM64 at `$0.0336/hr` remains a verified lower-cost alternative if ARM CI builds are adopted later).
2. **1st-party AWS security, image, and identity stack:**
   - **Amazon ECR** (`$0.10/GB-mo`, `F-052`) populated via **GitHub Actions OIDC** (zero long-lived IAM user keys).
   - **AWS SSM Session Manager** for shell diagnostics with **SSH port 22 closed**.
   - **AWS SSM Parameter Store** Standard-tier KMS-encrypted `SecureString` parameters (`$0`, `F-053`) injected into ECS task definitions at startup.
   - **Encrypted `gp3` EBS** (`$0.08/GB-mo`, `F-050`) persisting Odoo and Engine PostgreSQL volumes.
   - **Amazon Cognito User Pool** (`$0` within `10,000` MAU free tier, `F-053`) issuing OIDC/JWKS tokens verified by FastAPI for `viewer` / `approver` / `admin` RBAC (`Q11`).
3. **Budget enforcement (`<= USD 13/month`):** run on a scheduled EC2 stop/start window (~`220–240 h/month` active demo/eval time) with **no Elastic IP** (ephemeral public IPv4 while running only, `$0` compute and `$0` idle IPv4 while stopped, `F-050`), paired with **AWS Budgets** email/SNS alerts (`F-053`). Illustrative 220-hour baseline: `220h × ($0.0376 compute + $0.005 public IPv4) + 30 GiB × $0.08 gp3 + ~$0.40 ECR/S3 ≈ USD 12.17/month` before LLM/SES usage.
4. **Black-box ERP stance:** run containerized Odoo 19.0 + demo data (`ADR-0001`) as an external ERP boundary with `odoo_ro` SQL reads, JSON-2 writes, `Q12` PII tokenization, and a strict outbound recipient allowlist guard.

## Alternatives

- **Heroku dynos + Heroku Postgres:** rejected. Common Runtime `Eco`/`Basic`/`Standard-1X` dynos cap at `0.5 GB` RAM (`Standard-2X` at `1 GB` costs `$50/mo`), causing OOM on the `2,049.2 MiB` proposal peak, and `Heroku Postgres Essential` prohibits additional credentials needed for `odoo_ro` (`F-048`).
- **AWS App Runner:** rejected. Closed to new customers (`F-049`).
- **Amazon ECS Express Mode / Fargate + ALB + RDS:** rejected for MVP budget. Provisions an Application Load Balancer and Fargate/RDS charges outside the `<= USD 13/month` limit (`F-049`).
- **Amazon Lightsail:** rejected. Lightsail instances incur monthly/hourly charges even when stopped, whereas EBS-backed EC2 stops compute billing when stopped (`F-050`).

## Consequences

- Provides genuine production AWS primitives (Terraform, ECS Task Definitions, IAM Task Roles, ECR OIDC, SSM, Cognito, Budgets) inside the `$13/month` ceiling with sufficient RAM headroom for CPU ONNX inference.
- Single-node ECS is not multi-AZ high availability, and scheduled stop/start pauses background scans outside active windows; live cloud capacity (`Q14`) must be measured in `#88`.
