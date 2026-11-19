# ADR-0020: Unified CloudFront OAC + private S3 edge and backups

- Status: accepted; decision 3 (public-hostname backend origin) superseded in part by ADR-0024; decision 1 (how telemetry reaches Grafana Cloud and Langfuse Cloud, and what it may carry) superseded in part by [ADR-0029](0029-observability-topology-and-telemetry-data-rule.md)
- Date: 2026-11-14
- Issue: #84
- Supersedes: [ADR-0007](0007-free-tier-observability.md) (retains Grafana Cloud + Langfuse Cloud observability; moves UI & object storage to AWS)

## Context

`ADR-0007` selected Grafana Cloud and Langfuse Cloud for off-box observability alongside Cloudflare Pages and R2 for UI and backups. For the AWS MVP (`ADR-0019`), splitting edge delivery across Cloudflare and AWS introduces cross-origin CORS complexity and requires separate TLS termination on the backend without purchasing a custom Route 53 domain. Meanwhile, AWS provides a unified 1st-party edge and object-storage stack (`F-052`).

## Decision

1. **Retain off-box observability from `ADR-0007`:** export OpenTelemetry traces, metrics, and logs to **Grafana Cloud free tier** (`F-011`) and LLM traces/evaluations to **Langfuse Cloud Hobby** (`F-010`), carrying forward the strict synthetic/PII-tokenized data rule (`TM-1`, `TM-2`). Real GCC customer data still requires an in-region or self-hosted observability option (`Q12`).
2. **Host static Operator UI on private Amazon S3 + CloudFront OAC:** store static EN/RTL UI assets in a private S3 bucket (`$0.023/GB-mo`, `F-052`) served exclusively through **Amazon CloudFront Origin Access Control (OAC)** with SigV4 signing (`F-052`).
3. **Serve unified HTTPS on `*.cloudfront.net`:** use CloudFront's default `*.cloudfront.net` TLS certificate (`HTTPS only` / `Redirect HTTP to HTTPS` viewer policy, `F-052`) to route `/*` to the private S3 UI origin and `/api/*` to the ECS-on-EC2 backend origin. Lock the backend Security Group to CloudFront's managed prefix list (`com.amazonaws.global.cloudfront.origin-facing`) and validate a secret `X-Origin-Verify` custom header (`F-052`) stored in SSM Parameter Store.
4. **Store artifacts and backups in private encrypted Amazon S3:** replace Cloudflare R2 with a second private SSE-encrypted S3 bucket (`F-052`) for DB snapshots, evaluation reports, and audit exports.

## Alternatives

- **Keep Cloudflare Pages + R2 (`ADR-0007`):** rejected for MVP because it splits edge routing/DNS across two vendors and forces either a paid custom domain or cross-origin API TLS setup.
- **AWS Amplify Hosting or public S3 website endpoint:** rejected. Public S3 website endpoints do not support HTTPS between CloudFront and S3 (`F-052`), whereas OAC keeps S3 completely private with SigV4 verification.

## Consequences

- Zero custom-domain purchase required (`*.cloudfront.net` provides managed TLS), zero browser CORS issues (UI and `/api/*` share one HTTPS origin), and stronger AWS edge security (`OAC` + prefix list + origin verification).
- Because ephemeral EC2 public hostnames change on stop/start (`ADR-0019`), the EC2 startup automation must refresh the CloudFront backend origin domain via AWS CLI/Lambda if no Elastic IP is attached.
- `ADR-0007` is marked `superseded by ADR-0020`.
