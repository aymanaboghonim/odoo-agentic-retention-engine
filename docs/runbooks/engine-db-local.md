# Runbook: local engine database

The engine has its own PostgreSQL (pgvector image), separate from Odoo's database.
All data is synthetic.

## Start and test

```sh
# .env must define ENGINE_DB_PASSWORD (and ODOO_DB_PASSWORD, which Compose requires)
docker compose -f deploy/docker-compose.yml up -d --wait engine-db
export TEST_DATABASE_URL=postgresql://engine:<ENGINE_DB_PASSWORD>@127.0.0.1:5433/engine
export REQUIRE_DB_TESTS=1
uv run pytest -q tests/test_engine_runs.py tests/test_engine_db.py
```

The port binds to `127.0.0.1` only (`ENGINE_DB_HOST_PORT`, default `5433`). DB tests
truncate the run tables, so never point `TEST_DATABASE_URL` at a database with data you keep.

## Stop

```sh
docker compose -f deploy/docker-compose.yml stop engine-db
```
