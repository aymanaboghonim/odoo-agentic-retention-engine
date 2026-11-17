# ADR-0024: MVP network, ECS task layout, and CloudFront VPC origin

- Status: accepted
- Date: 2026-11-17
- Issue: #95 (part A of #88)
- Supersedes in part: [ADR-0019](0019-aws-ecs-on-ec2-cloud-runtime.md) decision 3 (ephemeral public IPv4 on the instance) and [ADR-0020](0020-aws-cloudfront-s3-edge-and-backups.md) decision 3 (backend origin on the EC2 public hostname) and its consequence on refreshing that hostname after each start. All other decisions in both ADRs stay.

## Context

ADR-0020 sends `/api/*` from CloudFront to the EC2 public hostname. Without a custom domain that hop has no valid TLS certificate, so bearer tokens would cross the internet in plain HTTP. The hostname also changes on every stop/start, which needs a Lambda to update the origin. CloudFront VPC origins reach an EC2 instance over the AWS network at no extra cost, but only in a private subnet (`F-069`). A NAT gateway for egress would break the `<= USD 13/month` target. A spike on 2026-11-17 measured the free option: IPv6-only egress through an egress-only internet gateway (`F-070`).

## Decision

1. **Network.** One VPC with an AWS-assigned IPv6 block, an internet gateway (required by VPC origins, not routed), and an egress-only internet gateway. One private dual-stack subnet in a VPC-origin AZ (not `use1-az3`). Its route table has `::/0` to the egress-only gateway and no IPv4 internet route. No NAT, no Elastic IP, no public IPv4 on the instance.
2. **Edge.** CloudFront serves `/api/*` through a VPC origin that targets the instance ARN (`http-only` on the API port, inside the AWS network). The instance security group allows the API port only from the CloudFront origin-facing prefix list (`F-064`). The `X-Origin-Verify` secret from ADR-0020 stays as a second check. No origin-refresh Lambda: the instance ARN and private DNS name survive stop/start.
3. **Agents on IPv6-only egress** (all measured in `F-070`):
   - `AWS_USE_DUALSTACK_ENDPOINT=true` in `/etc/ecs/ecs.config` and on `docker.service` (for the `awslogs` driver).
   - `"UseDualStackEndpoint": true` in the SSM Agent config.
   - Task images use the dual-stack ECR registry name `<account>.dkr-ecr.<region>.on.aws`.
   - Application endpoints must have IPv6. AWS calls use the dual-stack names (`*.api.aws`). OpenAI and Telegram have IPv6 (`F-070`).
4. **ECS task layout.**
   - Bridge network mode, with IMDSv2 required and hop limit 1, so containers cannot use the instance role. They get only their task roles.
   - Docker IPv6 NAT66 on the default bridge (`ipv6`, `fixed-cidr-v6` ULA, `ip6tables`, and `experimental` while Docker is below 27). Boot validates the config and keeps Docker running if it is invalid.
   - One long-running core task: odoo-db, odoo, engine-db, initialize (`SUCCESS` dependency), worker, approval-api, and mailpit.
   - An on-demand proposal task that reaches the core services through host ports. The security group has no inbound rule for those ports.
   - Odoo and Mailpit are reached only through SSM port forwarding.
5. **Data.** The encrypted root gp3 volume holds the PostgreSQL data under `/data`. Terraform ignores AMI changes, so the instance is not replaced by an AMI update. Synthetic data is rebuilt by the deterministic fixtures.

## Alternatives

- **Public subnet, HTTP origin on the public hostname, and a refresh Lambda (ADR-0020 as written):** works, but tokens cross the internet unencrypted, and there are several minutes of propagation after each start. Kept as the fallback if a later change loses IPv6 reachability.
- **VPC origin with a NAT gateway:** simplest egress, but it bills every hour even while the instance is stopped, outside the budget.
- **VPC interface endpoints for each AWS service:** billed per endpoint per hour, and they still give no path to OpenAI or Telegram.
- **`host` or `awsvpc` network mode:** host mode shares the instance network and its metadata service with containers. `awsvpc` puts the API on a task ENI that a VPC origin cannot target, and `t3a.medium` has only 3 network interfaces (`F-063`).

## Consequences

- Good: no public IPv4 charge, no public inbound path, no Lambda, a stable origin across stop/start, and no plain-HTTP hop on the internet.
- Every new outbound dependency needs IPv6 (an AAAA record). The live smoke in #97 checks each one; IPv4-only names fail fast (`F-070`). `ec2messages` has no dual-stack name, but Run Command still worked.
- Replacing the instance needs three steps: detach the VPC origin from the distribution, update the VPC origin, re-attach. It takes about 10 minutes, with API downtime. `tf_safe.sh` must flag an instance replace (INF-7), and the runbook documents the steps.
- CloudFront creates a service-managed security group in the VPC. The cleanup tooling (#96) must wait for it to go before it deletes the VPC.
- The Docker `experimental` flag is tied to Docker 25 on the current AMI. Re-check it when the AMI moves to Docker 27 or later.
