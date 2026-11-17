# Spec: AWS MVP deployment (Terraform, edge, identity, cost safety)

- Issue: #95 (part A of #88); tooling in #96, live run in #97
- ADRs: [ADR-0019](../adr/0019-aws-ecs-on-ec2-cloud-runtime.md),
  [ADR-0020](../adr/0020-aws-cloudfront-s3-edge-and-backups.md),
  [ADR-0024](../adr/0024-mvp-ecs-task-layout-and-cloudfront-origin.md)
- Related specs: [operator RBAC](operator-rbac-and-expiring-approvals.md),
  [privacy](privacy-consent-retention.md), [run governance](project-wide-run-governance.md)
- Facts: `F-050`, `F-052`, `F-053`, `F-062`..`F-068` in the [facts register](../facts-register.md)
- Status: agreed (owner, 2026-11-17)

## Purpose

Deploy the engine to 1st-party AWS for the MVP portfolio demo so that a bad
change cannot open the host to the internet, leak a secret, or leave billable
resources running. The owner and the AI coding agent are the users: every
change to AWS goes through a reviewed plan.

## Scope and non-goals

In scope: `deploy/terraform/bootstrap/` (state bucket, GitHub OIDC roles) and
`deploy/terraform/mvp/` (network, ECS on one EC2 host, ECR, SSM parameters,
CloudFront + private S3, Cognito, Budgets, nightly stop), the static and plan
policy checks, the `tf_safe.sh` workflow, and the `APPROVAL_API_ROOT_PATH`
engine setting. #96 adds shutdown, cleanup, and status scripts; #97 the deploy
workflow and live smoke.

Non-goals: high availability, a custom domain, the operator UI (Tier 3),
OpenTelemetry export (Tier 2), real customer data (`Q12`), `terraform apply` from CI.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| INF-1 | Terraform uses only allowlisted 1st-party AWS resource types. No Marketplace product or AMI, no `route53domains`, NAT gateway, Elastic IP, load balancer, or RDS. The AMI comes from the AWS-owned ECS-optimized SSM parameter. | `scripts/check_terraform_policy.py` (source and plan); `tests/test_terraform_policy.py::test_rejects_denied_resource_types`, `::test_rejects_non_aws_ami` |
| INF-2 | The only inbound path is the CloudFront VPC origin to the API port (ADR-0024). Ingress uses only a prefix list or a security group, never CIDR blocks, never port 22. The instance takes no public IPv4. | policy check; `tests/test_terraform_policy.py::test_rejects_open_ingress`, `::test_rejects_port_22`, `::test_rejects_public_ipv4_on_instance` |
| INF-3 | Every EBS volume and S3 bucket is encrypted. Every bucket blocks all public access, and the UI bucket is readable only by the CloudFront distribution (OAC). | policy check; `tests/test_terraform_policy.py::test_requires_encryption`, `::test_requires_public_access_block` |
| INF-4 | The instance requires IMDSv2 with hop limit 1, so bridge-mode containers cannot use the instance role. | policy check; `tests/test_terraform_policy.py::test_requires_imdsv2_hop_limit_1`; live check in #97 |
| INF-5 | No secret value is in Terraform state, the repo, or plan output. SecureString parameters use write-only `value_wo` (from ephemeral values) or a placeholder that is set outside Terraform. One exception (owner, 2026-11-17): `random_password.origin_verify`, the `X-Origin-Verify` value, because CloudFront keeps custom header values in the distribution config, so refresh puts it in state. State is in the private, encrypted, versioned bucket; with the VPC origin the header is defense in depth. | policy check (no literal `value` on SecureString); `scripts/scan_secrets.sh` in `make ci`; `tests/test_terraform_policy.py::test_rejects_securestring_literal`, `::test_allows_only_origin_verify_in_state` |
| INF-6 | A nightly stop schedule for the instance always exists, with a timezone (default 23:00 `Africa/Cairo`, owner 2026-11-17). | policy check; `tests/test_terraform_policy.py::test_requires_nightly_stop` |
| INF-7 | No AWS change is applied without a saved plan that passed the plan policy check and whose id the reviewer confirmed (`--confirm`). A replace or delete of a stateful resource needs an explicit `--allow-replace <address>`. Every AWS provider pins `allowed_account_ids`. A destroy plan (cleanup) may contain only deletes. | `scripts/tf_safe.sh`; `tests/test_tf_safe.py::test_apply_refuses_without_checked_plan`, `::test_apply_requires_matching_confirm`; `tests/test_terraform_policy.py::test_rejects_unapproved_replace`, `::test_requires_pinned_account`, `::test_destroy_plan_allows_only_deletes` |
| INF-8 | Cleanup is dry-run by default. `--apply` needs `--confirm <account-id>`, plus the reviewed destroy plan id unless `--sweep-only`. Cleanup reaches 100% of the project: every resource type on the INF-1 allowlist has a delete path, and a full sweep ends with `status --expect-clean` exit 0. Resources not tagged or named for the project are reported, never deleted. Status reports every billable type in ADR-0019 decision 4 and the account-wide risks (Elastic IP, NAT, load balancer, unattached volume, snapshot). All AWS tooling refuses the root identity. | `tests/test_aws_mvp_tooling.py::test_cleanup_covers_every_allowed_type`, `::test_full_sweep_reaches_clean_and_spares_unrelated`, `::test_full_cleanup_with_terraform_then_sweep`, `::test_cleanup_dry_run_makes_no_changes`, `::test_cleanup_apply_requires_matching_confirm_and_plan_id`, `::test_status_expect_clean_counts_account_billables`, `::test_refuses_root_and_default_credentials`, `::test_bootstrap_cleanup_refused_while_mvp_exists` |
| INF-9 | With `APPROVAL_API_ROOT_PATH` unset, approval API routes are unchanged. With it set, routes serve only under the prefix. | `tests/test_approval.py::test_root_path_prefix` |

## Terraform safety workflow

`terraform plan` is the dry run. `scripts/tf_safe.sh <root> plan|apply|destroy-plan` enforces:

1. Static gates: `fmt -check`, `init`, `validate`, source policy check.
2. Identity preflight: not root; account and region match the root's variables.
3. `terraform plan -out=<root>.tfplan` (or `-destroy`). Nothing changes in AWS.
4. `terraform show -json` and the plan policy check: INF-1..INF-6 on resolved values, no unapproved replace or delete, and a create/update/replace/destroy count.
5. The owner reviews the summary and the full plan. Destroy and replace lines are called out.
6. `terraform apply <root>.tfplan` applies exactly that plan. Terraform rejects a stale plan. The plan file is then removed.
7. `terraform plan -detailed-exitcode` returns 0 (no drift), then status runs.

## Behavior

- Given a reviewed plan, when `tf_safe.sh apply` runs, then only that plan file is applied and a zero-drift re-plan follows.
- Given no plan file, or one that failed the check, when `tf_safe.sh apply` runs, then it exits nonzero before calling Terraform (INF-7).
- Failure: given an expired SSO session, when any step calls AWS, then it stops with "run `aws sso login --profile aws-mvp`". The agent starts the login and the owner only approves it in the browser.
- Abuse: given a change that adds port 22 ingress, a `0.0.0.0/0` rule, or an `aws_eip`, when CI runs, then the source policy check fails the PR (INF-1, INF-2).
- Abuse: given a plan that would replace the Cognito pool or empty a bucket, when the plan check runs, then it fails unless the owner passes `--allow-replace` for that address (INF-7).

## Risks

- Budgets alerts lag hours behind spend (`F-053`). The nightly stop (INF-6) and the cleanup tooling are the real limits; `<= USD 13/month` is a target, not a cap.
- One host, no HA. Synthetic data on the root volume is lost if the instance is replaced; deterministic fixtures rebuild it.
- A policy check can only catch what it models. New resource types fail closed (not on the allowlist) until reviewed.

## Open questions

- Resolved 2026-11-17: CloudFront stays on pay-as-you-go, whose Always Free allowance covers MVP traffic (`F-065`). The flat-rate Free plan excludes VPC origins (`F-071`, [ADR-0025](../adr/0025-cloudfront-pay-as-you-go-always-free.md)). Actual spend is measured in #97.
