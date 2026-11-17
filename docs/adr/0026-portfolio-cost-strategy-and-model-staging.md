# ADR-0026: Portfolio cost strategy and cloud-side model staging

- Status: accepted
- Date: 2026-11-17
- Issue: #97 (part C of #88)

## Context

The MVP runs as a portfolio demo, not a production service, so idle cost matters more than uptime. Measured on 2026-11-17:

- While the instance exists, its 30 GiB gp3 root volume bills USD 2.40/month, running or stopped (`F-050`). 30 GiB is the floor: the ECS-optimized AMI's own snapshot is 30 GiB (`F-063`).
- Dumping the database to S3 does not remove that cost while the instance exists. All data is synthetic and rebuilt by deterministic fixtures (ADR-0019 decision 5), so a dump is not needed to rebuild.
- The embedding model (2.26 GB) cannot be downloaded on the IPv6-only host, because its CDN is IPv4-only (`F-072`). A first upload from the owner's machine ran at about 4 MB/s down and failed on upload. The upload would also repeat after every full cleanup, which deletes the backups bucket.

## Decision

1. **While the MVP is being built, keep the stack and stop it when idle.** The nightly stop (INF-6) and `aws_mvp_shutdown.sh` keep compute at zero between sessions. Idle cost is about USD 3/month: the EBS root volume plus cents of ECR and S3. The CloudFront URL, Cognito users, and MFA stay.
2. **After the MVP is complete, go ephemeral.** Run a full cleanup (`aws_mvp_cleanup.sh`) and keep only the bootstrap root (state bucket and OIDC role, about USD 0). Rebuild on demand for a live demo: `tf_safe.sh` apply plus the deploy workflow, about 30 minutes. Portfolio evidence (smoke report, measured cost, walkthrough) lives in the repo, not in the running stack.
3. **Stage the model from GitHub Actions, not from an operator machine.** The deploy workflow uploads the pinned model to `models/` in the backups bucket with the same hash and size checks (`scripts/aws_mvp.py model-upload`). It skips the step when `manifest.sha256` is already there. The deploy role gets only `s3:GetObject`/`PutObject` on `models/*` and a prefix-scoped `s3:ListBucket`. The model stays in S3 while the stack exists; the 30-day expiry covers only `backups/`.

## Alternatives

- **Keep the stack running with no idle stop:** about USD 30/month at 730 h (status estimate). Rejected for a portfolio.
- **Ephemeral from now:** about USD 0 idle, but a new `*.cloudfront.net` URL and new Cognito users every rebuild, while work is still in progress. Deferred to after the MVP.
- **Snapshot and delete the root volume between sessions:** a root volume cannot be removed without terminating the instance, and replacing the instance moves the VPC origin (ADR-0024). That is more complex than ephemeral for little saving.
- **Lambda to fetch the model:** works, but adds a function, a role, an ephemeral-storage size, and a 15-minute limit for what one workflow step does. Rejected.
- **Upload from the operator machine:** depends on home bandwidth and repeats after each cleanup. Kept only as a fallback (`model-upload` with a long `s3 cp` timeout).

## Consequences

- Idle cost is about USD 3/month now and about USD 0 after the switch. The trigger to switch is recorded in `docs/STATUS.md`.
- Rebuilds no longer depend on the owner's connection; the model staging runs at datacenter speed in the deploy workflow.
- The deploy role can write to `models/` in the backups bucket. It cannot read or write `backups/` or any other bucket.
