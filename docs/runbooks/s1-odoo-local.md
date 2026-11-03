# Runbook: local Odoo S1 baseline

This runbook starts an isolated Odoo Community 19.0 instance with synthetic demo data and its own PostgreSQL 15 database. The image digests are pinned in [docker-compose.yml](../../deploy/docker-compose.yml).

## Start the stack

1. Copy `.env.example` to `.env` without overwriting an existing file, then set a unique local-only `ODOO_DB_PASSWORD`. Keep `.env` private (`chmod 600 .env`); never commit it.
2. Start the Odoo profile:

   ```sh
   docker compose --project-name odoo-agentic-retention-engine --env-file .env -f deploy/docker-compose.yml --profile s1 up -d
   ```

3. Wait for module installation to finish and open `http://localhost:8069/web/login`. The script in step 5 can do the waiting for you with `--wait-ready`.
4. Create an Odoo API key for the local administrator in **Preferences → Account Security** (no scope, so JSON-2 accepts it). Store it in `ODOO_API_KEY` in `.env`; do not paste it into chat or logs.
5. Load the local variables into the shell and run the read-only probe:

   ```sh
   set -a
   . ./.env
   set +a
   uv run python scripts/spike_s1_odoo.py
   ```

6. Only if the probe finds no eligible aged abandoned cart, create the deterministic synthetic fixture through the local Odoo API:

   ```sh
   uv run python scripts/spike_s1_odoo.py --seed-aged-cart
   ```

## Preflight and progress

Every run first does a preflight, then the probe, then the optional seed. Each stage prints `progress stage=<name> status=<started|progress|heartbeat|completed|failed> elapsed_seconds=<n>` to stderr. A stage that runs longer than 30 seconds prints a heartbeat; the probe also prints `completed=<n> total=5`. Output never includes the API key or full records.

```sh
uv run python scripts/spike_s1_odoo.py --preflight-only            # check, then stop
uv run python scripts/spike_s1_odoo.py --wait-ready 600            # wait up to 600 s for Odoo, then probe
```

Preflight stages, in order:

1. `preflight-configuration`: `ODOO_URL` is a loopback URL, `ODOO_DB_NAME` and `ODOO_API_KEY` are set.
2. `preflight-readiness`: the login page answers. With `--wait-ready SECONDS` it retries every 5 seconds until the bound passes; without it, it tries once.
3. `preflight-api`: one read-only `website.search_count` call proves the API key and database are accepted.

A failure prints `S1 Odoo check failed at stage <name>: <reason>` and exits 1. Nothing is written before preflight passes, and the seed does not run after a failed stage.

Preflight does not prove that module installation is complete, that demo data exists, that a reminder is enabled, or that the seed will succeed. The probe and seed write no files, and the script does not use Docker, so neither is checked. The Compose `up` command still shows Docker's own output.

The probe and optional fixture reject non-loopback Odoo URLs. The fixture never uses SQL. The normal runtime remains read-only against the Odoo database; state-changing runtime operations use the Odoo API/worker path.

When VS Code is connected to this machine with Remote-SSH, forward port `8069` from the **Ports** panel, then open `http://localhost:8069/web/login` in the integrated browser. The service remains bound to loopback; do not expose it on the LAN.

## Stop and measure

Stop the containers without deleting the database or filestore:

```sh
docker compose --project-name odoo-agentic-retention-engine --env-file .env -f deploy/docker-compose.yml --profile s1 stop
```

For S1 measurements, record idle and peak `docker stats` for both services, host CPU/RAM, and the database and filestore sizes. Include the exact command, observation window, image digests, and whether initial module installation was included. Do not treat this Odoo + PostgreSQL measurement as full-stack POC capacity.
