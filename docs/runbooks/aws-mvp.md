# Runbook: AWS MVP deployment, start/stop, and cleanup

Issue #96 (part B of #88). Design: [ADR-0019](../adr/0019-aws-ecs-on-ec2-cloud-runtime.md), [ADR-0020](../adr/0020-aws-cloudfront-s3-edge-and-backups.md), [ADR-0024](../adr/0024-mvp-ecs-task-layout-and-cloudfront-origin.md). Invariants: [AWS MVP deployment spec](../specs/aws-mvp-deployment.md). Every command below is a dry run unless it says `--apply` or `apply --confirm`.

## Rules

- Never use the root user. Every command needs `AWS_PROFILE=aws-mvp` (Identity Center admin). The scripts refuse root and refuse default credentials.
- Never run a bare `terraform apply`. Use `scripts/tf_safe.sh`: it saves a plan, checks it against INF-1..INF-7, prints a plan id, and applies only that plan with `--confirm <plan-id>`. Then it requires an empty re-plan.
- `--apply` on the ops scripts and `--confirm` on `tf_safe.sh` are for the owner, or for the agent after the owner approved that exact plan id.
- `<= USD 13/month` is a target, not a cap. Budget alerts lag hours behind spend (`F-053`); the nightly stop and the cleanup are the real limits.

## SSO session (12 hours)

When a command fails with `SSO session expired`, the agent runs:

```bash
aws sso login --profile aws-mvp
```

The owner only approves access in the browser window. Then check the role (it must not be root):

```bash
aws sts get-caller-identity --profile aws-mvp --query Arn --output text
```

## First deploy

1. Bootstrap (once). Copy `deploy/terraform/bootstrap/terraform.tfvars.example` to `terraform.tfvars` and set `account_id`. Then:

   ```bash
   export AWS_PROFILE=aws-mvp
   scripts/tf_safe.sh deploy/terraform/bootstrap plan
   # owner reviews deploy/terraform/bootstrap/.tfsafe/plan.txt
   scripts/tf_safe.sh deploy/terraform/bootstrap apply --confirm <plan-id>
   terraform -chdir=deploy/terraform/bootstrap output
   ```

   Keep `deploy/terraform/bootstrap/terraform.tfstate` safe (local state, gitignored). Losing it does not delete resources, but Terraform can no longer manage them.

2. MVP root. Copy `backend.hcl.example` to `backend.hcl` with the `state_bucket` output. Copy `terraform.tfvars.example` to `terraform.tfvars`, with `account_id` and `budget_email`. Leave `core_desired_count = 0` until images are in ECR (#97).

   ```bash
   scripts/tf_safe.sh deploy/terraform/mvp plan
   scripts/tf_safe.sh deploy/terraform/mvp apply --confirm <plan-id>
   ```

   AWS sends a budget subscription email. The owner confirms it.

3. External secrets. Terraform creates them as `unset`. Write each one without putting the value on the command line:

   ```bash
   scripts/aws_mvp_secrets.sh openai-api-key --from-file <path-to-key-file>   # dry run
   scripts/aws_mvp_secrets.sh openai-api-key --from-file <path-to-key-file> --apply
   ```

   The same works for `telegram-bot-token` and `odoo-api-key`. Without `--from-file` the script prompts with hidden input. The value goes to a private temp file (mode 600) that is deleted after the call.

## Daily use

```bash
scripts/aws_mvp_start.sh --apply --url "$(terraform -chdir=deploy/terraform/mvp output -raw api_base_url)"
scripts/aws_mvp_shutdown.sh --apply      # also runs every night at 23:00 Africa/Cairo (INF-6)
python scripts/aws_mvp_status.py         # read-only; --json, --expect-clean
```

- `start` waits for the instance, the ECS agent, the core service, and a `401` from `/api/approvals` through CloudFront.
- `shutdown` is idempotent. `--disable-edge` also disables CloudFront; the next Terraform apply turns it back on.
- `status` asks each service API directly. The tag search API keeps terminated and inactive records for a while (`F-070`), so `status` does not use it. It also sweeps the whole account for Elastic IPs, NAT gateways, load balancers, unattached volumes, and snapshots.

## Shell and private UIs

Port 22 is closed and the host has no public IP. Use Session Manager (needs the local `session-manager-plugin`):

```bash
aws ssm start-session --target <instance-id>
aws ssm start-session --target <instance-id> --document-name AWS-StartPortForwardingSession \
  --parameters portNumber=18025,localPortNumber=18025   # Mailpit UI; 18069 for Odoo
```

## Cleanup (teardown)

Dry run first. It lists every change and saves a destroy plan for review (no AWS changes):

```bash
scripts/aws_mvp_cleanup.sh
# owner reviews deploy/terraform/mvp/.tfsafe/plan.txt: only deletes are allowed (policy --destroy-plan)
scripts/aws_mvp_cleanup.sh --apply --confirm <account-id> --plan-id <plan-id>
python scripts/aws_mvp_status.py --expect-clean
```

- Apply empties the backups bucket, applies the reviewed destroy plan, then runs a residual sweep that deletes anything still tagged or named for the project. It deletes in dependency order:
  1. ECS service, instance, cluster, and task definitions (including inactive revisions).
  2. CloudFront: disable, wait, delete the distribution, then the VPC origin and OAC.
  3. VPC networking: gateways, subnets, route tables, security groups, and the VPC.
  4. Volumes, snapshots, ECR repositories with their images, log groups, and buckets with every object version.
  5. Cognito domain and pool, the schedule, the budget, and SSM parameters.
  6. IAM instance profiles and roles; on `--bootstrap`, also the GitHub OIDC provider (only if it is tagged for this project).

  Every Terraform resource type has a delete path, and a test enforces this (`test_cleanup_covers_every_allowed_type`). Cleanup ends by checking that nothing of the project is left.
- Lost or broken Terraform state: `scripts/aws_mvp_cleanup.sh --sweep-only`, then `--sweep-only --apply --confirm <account-id>`. It skips Terraform and sweeps by tag and name.
- Anything the account-wide sweep finds that is not tagged for the project (an Elastic IP, NAT gateway, load balancer, unattached volume, or snapshot) is listed as `REVIEW` and never deleted. `status --expect-clean` still fails while it exists, because it still costs money.
- Expect about 12 minutes. The ECS service waits in `DRAINING` and the VPC origin delete is slow (`F-070`).
- The bootstrap root goes last and only when nothing of the mvp root is left: `scripts/aws_mvp_cleanup.sh --bootstrap`, then `--bootstrap --apply --confirm <account-id> --plan-id <plan-id>`. Then run `status --expect-clean --include-bootstrap`.

## Things to know

- **CloudFront stays on pay-as-you-go.** Its Always Free allowance (1 TB, 10M requests per month, `F-065`) covers MVP traffic. Do not subscribe the distribution to a flat-rate plan: the Free plan has no VPC origins, and a subscribed distribution cannot be deleted until the plan is canceled (`F-071`, [ADR-0025](../adr/0025-cloudfront-pay-as-you-go-always-free.md)).

- **Plan JSON is sensitive.** `terraform show -json` includes sensitive variable values, such as the budget email. `tf_safe.sh` keeps plans in the gitignored, mode-700 `.tfsafe/` folder and deletes them after apply. Never attach them to issues or PRs.
- **The origin-verify value is in state** (the INF-5 exception). The state bucket is private, encrypted, versioned, and TLS-only.
- **Replacing the instance** (not stop/start) moves the VPC origin, which cannot change while a distribution uses it (`F-070`). Terraform ignores AMI and boot-script changes so this never happens by accident. A planned replace has three steps, each through `tf_safe.sh`:
  1. Point the `/api/*` origin away (a placeholder) and apply.
  2. Replace the instance, which updates the VPC origin, and apply.
  3. Point the origin back and apply.

  Expect about 10 minutes of API downtime. The policy check flags an instance replace unless `--allow-replace aws_instance.host` is passed.
- **Rotating generated passwords** (`secrets_version`) writes new SSM values, but PostgreSQL keeps the password it started with. Change the database role password first, or rebuild the synthetic data. Fixtures are deterministic (ADR-0019 decision 5).
- **Outbound needs IPv6.** The host has no IPv4 internet path. A new dependency without an AAAA record fails fast; check it before adding it (ADR-0024).

## Limits

These scripts are verified by hermetic tests and by read-only runs of `status` and the `shutdown` dry run on the real account. The first live apply, start, shutdown, and cleanup happen in #97.
