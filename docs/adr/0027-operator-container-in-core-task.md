# ADR-0027: Operator container in the core task, not a one-off proposal task

- Status: accepted
- Date: 2026-11-18
- Issue: #97 (part C of #88)
- Supersedes in part: [ADR-0024](0024-mvp-ecs-task-layout-and-cloudfront-origin.md) decision 4 (the on-demand proposal task that reaches the core services through host ports). The rest of ADR-0024 stays.

## Context

ADR-0024 put one-off work (fixtures, policy ingest, a worker run, `erp_cli`, a model proposal) in a separate ECS task. In bridge mode, a container in another task can reach the core databases only through ports published on the host. The engine's network guard (`WorkerSettings.from_env`) accepts only `engine-db` and `odoo-db` on port 5432. `scripts/erp_minimal_fixtures.py`, `engine.full_stack_proposal`, and `engine.erp_cli` all use this guard. Found live on 2026-11-18 (#97): the host-port URLs (`engine-db:15432`, `odoo-db:15433`) would be refused. The guard keeps the engine on its isolated network, and weakening it would need a spec change.

## Decision

1. Add an `operator` container to the core task. It uses the engine image and runs `sleep infinity`, so it does nothing by itself. It is linked to `engine-db`, `odoo-db`, `odoo`, and `mailpit` on their own ports, like the Compose `proposal` service.
2. It gets the settings and secrets the one-off commands need: database URLs on 5432, the privacy pepper, the OpenAI key, the Telegram token and chat id, and the Odoo API key. It also mounts the model cache read-only.
3. A `model-sync` container in the same task copies the pinned embedding model from S3 and checks its hashes (`F-072`). The operator starts only after `initialize` and `model-sync` exit successfully. The core task role becomes the read-only model role (`models/*` only).
4. Operators run one allowlisted command with `scripts/aws_mvp.py exec` (SSM Run Command, `docker exec` in `operator`). The modules are `engine.erp_cli`, `engine.full_stack_worker`, `engine.privacy_cli`, `scripts.erp_minimal_fixtures`, and `scripts.ingest_policies`. Secret-looking `--env` names are refused.
5. Removed:
   - the proposal task definition;
   - the host-published ports of both databases and of Mailpit SMTP;
   - the two `-host` URL parameters;
   - the unused plain task role;
   - the deploy role's `ecs:RunTask` grant.

   Host ports left: the API (8080, the CloudFront origin) and the Odoo and Mailpit UIs (18069, 18025, reached only through SSM port forwarding; no security group rule).

## Alternatives

- **Relax the engine's network guard** to accept configured hosts and ports: a smaller infrastructure change, but it weakens a safety check. Rejected.
- **ECS Exec into the worker container:** the worker lacks the external secrets and the model cache, and adding them would give the always-on scanner more than it needs. Rejected.
- **awsvpc mode for the one-off task:** it would still not share the core task's network, and `t3a.medium` has only 3 network interfaces (`F-063`). Rejected.

## Consequences

- Good: the engine's guards hold unchanged, fewer ports are open on the host, and the layout matches the local Compose topology.
- Cost: an idle container, about 256 MiB reserved. The external secrets stay in one long-running container's environment on a private host with no public ingress.
- A changed secret (for example the Odoo API key) reaches the operator only at the next core task start. Roll out the core service after changing one.
- Run Command returns at most 24,000 characters of output; long logs are cut in the `exec` output. The operator's own logs (CloudWatch, `core/operator/...`) are unaffected.
