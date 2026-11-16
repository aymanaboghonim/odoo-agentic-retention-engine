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

## Throwaway test database (recommended for `pytest` and `make ci`)

For test runs, use a throwaway container instead of the Compose database. Over
`127.0.0.1:<port>` on WSL2 with Docker, the full suite randomly lost connections
(`server closed the connection unexpectedly`, nothing in the server log; `main`
behaved the same). Measured 2026-11-16 in #85 and #86: over a Unix socket the full
suite passed four runs in a row (402 passed, 4 skipped). The steps are in the
[`local-test-db` skill](../../.claude/skills/local-test-db/SKILL.md): mount a short
`/dev/shm/$USER-pg/s` directory at `/var/run/postgresql` and use
`TEST_DATABASE_URL=postgresql://engine:<pw>@/engine?host=/dev/shm/$USER-pg/s`. Stop it
with `docker stop` (not `docker rm -f`) so PostgreSQL removes its socket files.

## Stop

```sh
docker compose -f deploy/docker-compose.yml stop engine-db
```
