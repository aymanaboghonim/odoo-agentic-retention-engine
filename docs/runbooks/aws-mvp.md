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

## Live deploy and smoke (#97)

Owner decisions (2026-11-17): the model comes through the S3 backups bucket; channels are Mailpit and the dedicated Telegram test chat; at most one model proposal with a USD 0.50 cap; the stack stays about 48 hours for cost measurement, then full cleanup. Steps marked **owner** are hand-offs. Every Terraform change goes through `tf_safe.sh` plan, review, and `--confirm`.

1. Bootstrap root (First deploy, step 1). Then create the GitHub environment `aws-mvp` with the owner as required reviewer, and set its variable `AWS_DEPLOY_ROLE_ARN` to the `deploy_role_arn` output.
2. MVP root with `core_desired_count = 0` (First deploy, step 2). **Owner:** confirm the AWS budget subscription email.
3. Stage the embedding model. Hugging Face's file CDN is IPv4-only, so the host cannot download it (`F-072`). Run from a machine with IPv4; the upload is 2.26 GB:

   ```bash
   python -m scripts.aws_mvp model-upload --bucket "$(terraform -chdir=deploy/terraform/mvp output -raw backups_bucket)" --apply
   ```

4. Images. Run the workflow and approve it (**owner**):

   ```bash
   gh workflow run deploy-aws.yml
   ```

   It mirrors the pinned third-party images (`deploy/aws-mvp-images.json`), pushes `aorte-mvp/engine:<git sha>`, and registers new task definitions. Each image entry has two pins: `digest` (what Compose uses, often a multi-platform index) and `aws_digest` (its `linux/amd64` manifest). Only `aws_digest` is copied to ECR and used by the task definitions; the mirror first checks that it is the `linux/amd64` child of `digest` (#106). To update an image, change both pins.

   Images left from older multi-platform copies are removed with a dry run first. The command refuses while the core task definition still uses a digest it would delete:

   ```bash
   python scripts/aws_mvp.py ecr-prune            # lists what would be deleted per repository
   python scripts/aws_mvp.py ecr-prune --apply    # after review; keeps every amd64 pin
   ```

5. Secrets: `openai-api-key`, `telegram-bot-token`, and `telegram-chat-id` (**owner** provides the token and chat id) through `scripts/aws_mvp_secrets.sh`.
6. Apply `core_desired_count = 1` through `tf_safe.sh`, then `scripts/aws_mvp_start.sh --apply --url <api_base_url>`.
7. Odoo (**owner**): forward port 18069 with Session Manager, change the demo admin password, and create a named API key. Store it with `scripts/aws_mvp_secrets.sh odoo-api-key`.
8. Operators (**owner**): create two users in the Cognito pool, one in `viewer` and one in `approver`. Sign in once per user to set up TOTP MFA, and keep each token in a mode-600 file:

   ```bash
   python scripts/cognito_login.py --domain "$(terraform -chdir=deploy/terraform/mvp output -raw cognito_login_domain)" \
     --client-id "$(terraform -chdir=deploy/terraform/mvp output -raw cognito_client_id)" --out ~/.config/aorte/approver.token
   ```

   Add `--no-browser` and open the printed authorize URL yourself. Cognito keeps a login session cookie, so a second sign-in in the same browser silently reuses the first user. To switch users (for example approver, then viewer), sign in one user at a time:

   1. Start the helper for the next user (`--out ~/.config/aorte/viewer.token`). It listens on `localhost:8765`.
   2. In the browser, open the logout URL that the helper prints (`/logout?client_id=...&logout_uri=http://localhost:8765/logout`). This clears the Cognito session.
   3. Open the printed authorize URL in the same tab and sign in as the next user with password and TOTP.
   4. Check that the `groups=[...]` the helper prints matches the user. If it does not, delete the token file and repeat.

   Access tokens last 15 minutes. Get both tokens just before the smoke (step 10) needs them.

9. Operator commands (ADR-0027: `docker exec` in the core task's `operator` container over SSM): policies, then fixtures (the preflight first):

   ```bash
   python -m scripts.aws_mvp exec --apply -- python -m scripts.ingest_policies
   python -m scripts.aws_mvp exec --apply -- python -m scripts.erp_minimal_fixtures --preflight-only
   python -m scripts.aws_mvp exec --apply -- python -m scripts.erp_minimal_fixtures --confirmed-synthetic
   ```

10. Smoke (SM-1..SM-5) with a report:

    ```bash
    python scripts/aws_mvp_smoke.py --apply --base-url <cloudfront_url> \
      --viewer-token ~/.config/aorte/viewer.token --approver-token ~/.config/aorte/approver.token \
      --out evals/aws-mvp/results/<run-id>
    ```

11. End-to-end retention flow. Same commands as [privacy-local.md](privacy-local.md), run in the operator container (`aws_mvp.py exec`, ADR-0027): worker `--once` with `SCAN_MODE=erp`, then `erp_cli preflight`, `propose` (one model proposal at most, USD 0.50 cap), `show`, and `execute`. `execute` needs the exact run id and destination hash from `show`; the **owner** confirms each delivery (Mailpit, then Telegram).

    Pass the run settings with `--env` on every `exec`. The values used on 2026-11-18 (owner-approved demo values for `Q12`; the gate values reuse POC-2):

    | Setting | Value |
    | :-- | :-- |
    | `EXECUTION_ERP_MODE` | `YES` |
    | `EXECUTION_ALLOWLIST_DOMAINS` | `example.test` |
    | `ERP_SCAN_CHANNEL` | `email` (Mailpit is the email transport, not a channel) |
    | `CAMPAIGN_COOLDOWN_DAYS` | `7` |
    | `PRIVACY_RETENTION_DAYS` | `30` |
    | `GATE_TIMEZONE`, `GATE_CURRENCY`, `GATE_DAILY_BUDGET` | `Asia/Riyadh`, `USD`, `450` |
    | `GATE_CATEGORY_MIN_MARGINS` | `{"Furniture / Office":"0.2"}` |
    | `propose` only: `PROPOSAL_ERP_MODE`, `PROPOSAL_BUDGET_USD`, `PROPOSAL_REVIEW_BUDGET_USD` | `YES`, `0.50`, `0.50` |
    | `propose` only: `PROPOSAL_INPUT_TOKEN_LIMIT`, `PROPOSAL_OUTPUT_TOKEN_LIMIT`, `PROPOSAL_REVIEW_ID` | `4000`, `8192`, a review id |

    `exec` refuses `--env` names that end in `_KEY`, `_TOKEN`, `_PASSWORD`, `_SECRET`, or `_PEPPER`; secrets come only from SSM through the task definition.

12. Cost allocation tag (**owner** approves; billing setting). The `Project` key appears in Billing about a day after tagged resources exist, and activation applies only to cost from then on:

    ```bash
    aws ce list-cost-allocation-tags --region us-east-1 --tag-keys Project
    aws ce update-cost-allocation-tags-status --region us-east-1 --cost-allocation-tags-status TagKey=Project,Status=Active
    ```

    Activating before the key appears fails with `Tag keys not found: Project` (seen 2026-11-17), so retry the next day. Then backfill from the apply start, so the whole measurement is tagged (one backfill request per 24 hours):

    ```bash
    aws ce start-cost-allocation-tag-backfill --region us-east-1 --backfill-from <apply-start, e.g. 2026-11-17T00:00:00Z>
    ```

    Until it is active, use account-wide cost. That is valid only while `status` finds nothing else billable in the account.

13. Measure: RAM and disk on the host (`docker stats`, `free -m`, `df -h` over SSM Run Command), and image sizes (`docker images`, `aws ecr describe-images`). The 2026-11-18 values are in `F-075`. After about 48 hours, read the daily cost in Cost Explorer, filtered by `Project=aorte-mvp`. Then run Cleanup and `status --expect-clean --include-bootstrap` (#107).

Status and cleanup do not use AWS Resource Groups or the tag search API: that API kept stale records after deletion (`F-070`), and some resources (the CloudFront service security group, Cognito groups and clients) are not tagged. The tools query each service API by tag and name instead, and sweep the whole account for untagged billables.

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

The first live apply, deploy, smoke (all of SM-1..SM-5), end-to-end flow, and nightly stop ran on 2026-11-18 (#97, [smoke report](../../evals/aws-mvp/results/2026-11-18-smoke/summary.md)). The live cleanup and the measured cost come in #107.
