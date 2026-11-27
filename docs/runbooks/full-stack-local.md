# Runbook: isolated synthetic full stack

This runbook starts a separate Odoo 19.0 database and filestore, an engine
database, the read-only scanner worker, the local-only approval API, and Mailpit.
All records are synthetic. Do not connect this project to another Odoo or
engine database. The Compose project name and volumes are dedicated to this
stack.

## Private configuration and startup

Keep the repository-root `.env` untracked and mode `0600`. The full-stack
passwords, database URLs, instance UUID, ports, and local paths use the
`FULL_STACK_*` variable names in `.env.example`; they are separate from the
existing S1/S2 local Odoo configuration. Store the generated API key for this
stack only in `.env` as `FULL_STACK_ODOO_API_KEY`. The named key is for the
dedicated disposable Odoo database, expires after one year, and must not be
copied to another Odoo instance. Do not print it, put it in a command-line
argument, or commit it. The API key grants RPC access as the local Odoo
administrator; the worker does not receive it.

Set unique values for `FULL_STACK_ODOO_DB_PASSWORD`,
`FULL_STACK_ODOO_RO_PASSWORD`, and `FULL_STACK_ENGINE_DB_PASSWORD`.
`FULL_STACK_ENGINE_DATABASE_URL` and
`FULL_STACK_ODOO_READONLY_DATABASE_URL` must use their respective dedicated
Compose service, database, and role. Keep the existing `.env` values for the
other local projects unchanged.

Optional (#165, #166): to run Grafana Alloy locally, set
`FULL_STACK_ENGINE_MONITOR_PASSWORD` and `FULL_STACK_ODOO_MONITOR_PASSWORD`
(both or neither, each unique) so the initialize service creates the
`aorte_monitor` role (LOGIN and CONNECT only), export the Grafana settings, and
add `--profile observability`. Without the passwords no role is made and Alloy
reports `pg_up` 0.

Start the stack with one command:

```sh
docker compose --project-name poc68-isolated --env-file .env \
  -f deploy/full-stack-compose.yml up --build -d --wait
```

Odoo and Mailpit UIs bind only to loopback (`FULL_STACK_ODOO_PORT`, default
18070; `FULL_STACK_MAILPIT_PORT`, default 18025). The engine DB, worker, and
approval API have no published ports. The approval API remains bound to
container loopback and uses its operator token. The internal service network has no external route. To expose the Odoo and
Mailpit UIs on loopback, both services also join a bridge network; Docker does
not block their outbound traffic on that network. The proposal service alone
joins the separately named provider-egress network. This Compose topology is
for an isolated local development instance, not a network security boundary.
The proposal service's model cache is mounted read-only and offline.
Odoo's own cron threads are disabled in this synthetic stack.

Initialization waits for `website_sale`, creates the separate `odoo_ro` role,
revokes public schema creation, grants that role read-only access, verifies its
privileges, and migrates the engine DB. It does not seed carts or call a model.
If role initialization fails, fix the isolated stack configuration; do not
weaken the role checks or use SQL to create synthetic Odoo business records.

## Create and scan one synthetic fixture

Create a new key in the new Odoo instance using the administrator's Odoo
**Preferences → Account Security** API-key wizard. Store the value as
`FULL_STACK_ODOO_API_KEY` in the root `.env` and set `chmod 600 .env`. Do not
reuse `ODOO_API_KEY` or `DISPOSABLE_ODOO_API_KEY`; those belong to other local
instances.

The fixture preflight is read-only and confirms the local product, website,
database target, and read-only role:

```sh
docker compose --project-name poc68-isolated --env-file .env \
  -f deploy/full-stack-compose.yml run --rm --no-deps proposal \
  python -m scripts.full_stack_fixture --preflight-only
```

Only after reviewing the isolated Compose project, create one synthetic cart
through Odoo JSON-2. The exact flag is required; the script prints the
synthetic cart ID and reference, not credentials:

```sh
docker compose --project-name poc68-isolated --env-file .env \
  -f deploy/full-stack-compose.yml run --rm --no-deps proposal \
  python -m scripts.full_stack_fixture --confirmed-synthetic
```

The scheduled worker scans only records with a `retention-review-` reference
followed by 12 lowercase hex characters and the matching `example.test`
address. It creates durable, idempotent runs and never invokes the model or
sends messages. Its cursor is persisted in the engine DB. Check worker
progress and run records before requesting a proposal.

## Optional one-shot model proposal

Model generation is not part of startup, scanning, or fixture creation. It
requires an explicit one-shot command, an API key in the process environment,
the pinned local model cache (`FULL_STACK_MODEL_CACHE`), and a persisted
aggregate budget. Set the approved live provider key in the shell without
printing it; do not store it in the project `.env`. For example:

```sh
set -a
. "$HOME/.config/waddih/.env"
. ./.env
set +a
```

First obtain the `run_id` for the newly created fixture from the worker or
engine DB. Run the preflight; it does not call the model or load model weights:

```sh
docker compose --project-name poc68-isolated --env-file .env \
  -f deploy/full-stack-compose.yml run --rm --no-deps \
  -e FULL_STACK_CAPACITY_PROPOSAL=YES proposal \
  python -m engine.full_stack_proposal --run-id RUN_UUID --preflight-only
```

After preflight succeeds, the explicitly requested initial run can call the
provider once. Do not retry an uncertain call or delete the engine DB to reset
the budget. Its durable claim allows one attempt, including an attempt whose
response is uncertain. The single sampler-failure recovery is separately
bounded and documented below:

```sh
docker compose --project-name poc68-isolated --env-file .env \
  -f deploy/full-stack-compose.yml run --rm --no-deps \
  -e FULL_STACK_CAPACITY_PROPOSAL=YES proposal \
  python -m engine.full_stack_proposal --run-id RUN_UUID
```

The shared proposal budget is USD 1.00; provider token-count billing and active
provider rate limits remain unknown (F-038, F-046). The existing gate remains
the only financial authority. A proposal does not create a coupon, approve
itself, or dispatch a message.

## Measure and publish a bounded capacity sample

The measurement script fails preflight before running the proposal unless the
stack network and services, output directory, read-only Odoo role, synthetic
run, model cache, budget, and provider key pass. Use a new run UUID and a
pre-existing output directory. The script takes three sequential idle Docker
statistics samples, then measures one bounded scan page and either the
explicitly enabled initial proposal or the owner-approved recovery proposal in
separate stages. It waits one second between sequential Docker statistics
snapshots; command runtime adds to the sample cadence. Each stage is limited to
20 minutes. A completed run publishes a matching Markdown summary and raw JSON
samples atomically; failed runs do not overwrite existing reports.

```sh
mkdir -p evals/full-stack/results
set -a; . "$HOME/.config/waddih/.env"; . ./.env; set +a
PYTHONPATH=src uv run --no-sync python scripts/full_stack_measure.py \
  --env-file .env \
  --output-dir evals/full-stack/results \
  --run-id RUN_UUID --preflight-only
```

After reviewing the preflight output, run the bounded measurement once:

```sh
PYTHONPATH=src uv run --no-sync python scripts/full_stack_measure.py \
  --env-file .env \
  --output-dir evals/full-stack/results \
  --run-id RUN_UUID
```

Once `stage=proposal status=started` appears, that attempt's durable claim may
be consumed even if the command or report fails. Do not reset the database or
retry. Inspect the proposal-call ledger and run state before using the single
owner-approved sampler-failure recovery below. Do not rerun with the same run
UUID or claim. This is a sequential single-workload sample, not a throughput,
production sizing, billing, or concurrency test. Docker container memory is
summed per sample; CPU percentages are summed and 100% represents one CPU.
Host platform and logical CPU count provide provenance, not host-wide resource
measurements.

### Owner-approved recovery after a sampler failure

For the specific #68 sampler race, the owner approved exactly one recovery
proposal within the same USD 1 aggregate budget. The recovery is available only
when the initial run is rejected and its one provider call is settled; an
uncertain call, a different terminal state, or an already-used recovery claim
fails closed. The original claim and budget remain in the engine database.

Create a second synthetic fixture through the JSON-2 command above, run one
bounded scan page, and obtain its new `run_id`. Then preflight the recovery:

```sh
PYTHONPATH=src uv run --no-sync python scripts/full_stack_measure.py \
  --env-file .env \
  --output-dir evals/full-stack/results \
  --run-id RECOVERY_RUN_UUID \
  --recovery-after-sampler-failure --preflight-only
```

Only if that preflight passes, run the recovery measurement once:

```sh
PYTHONPATH=src uv run --no-sync python scripts/full_stack_measure.py \
  --env-file .env \
  --output-dir evals/full-stack/results \
  --run-id RECOVERY_RUN_UUID \
  --recovery-after-sampler-failure
```

The recovery has its own durable claim ID but uses the same proposal review
budget and counts the initial call against the aggregate. If this recovery
fails or its provider result becomes uncertain, do not attempt another call.

To stop only this isolated project without deleting its databases:

```sh
docker compose --project-name poc68-isolated --env-file .env \
  -f deploy/full-stack-compose.yml stop
```
