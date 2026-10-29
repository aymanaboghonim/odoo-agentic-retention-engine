# Runbook: local Odoo S1 baseline

This runbook starts an isolated Odoo Community 19.0 instance with synthetic demo data and its own PostgreSQL 15 database. The image digests are pinned in [docker-compose.yml](../../deploy/docker-compose.yml).

## Start the stack

1. Copy `.env.example` to `.env` without overwriting an existing file, then set a unique local-only `ODOO_DB_PASSWORD`. Keep `.env` private (`chmod 600 .env`); never commit it.
2. Start the Odoo profile:

   ```sh
   docker compose --project-name odoo-agentic-retention-engine --env-file .env -f deploy/docker-compose.yml --profile s1 up -d
   ```

3. Wait for module installation to finish and open `http://localhost:8069/web/login`.
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

The probe and optional fixture reject non-loopback Odoo URLs. The fixture never uses SQL. The normal runtime remains read-only against the Odoo database; state-changing runtime operations use the Odoo API/worker path.

## Stop and measure

Stop the containers without deleting the database or filestore:

```sh
docker compose --project-name odoo-agentic-retention-engine --env-file .env -f deploy/docker-compose.yml --profile s1 stop
```

For S1 measurements, record idle and peak `docker stats` for both services, host CPU/RAM, and the database and filestore sizes. Include the exact command, observation window, image digests, and whether initial module installation was included. Do not treat this Odoo + PostgreSQL measurement as full-stack POC capacity.
