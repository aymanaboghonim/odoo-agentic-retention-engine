---
name: local-test-db
description: 'Use when running pytest or make ci locally against PostgreSQL, when a test database is needed (TEST_DATABASE_URL, REQUIRE_DB_TESTS=1), or when the suite fails with "server closed the connection unexpectedly". Starts a throwaway pgvector PostgreSQL over a Unix socket (reliable on WSL2) and cleans it up.'
---

# Local test database

Tests and `make ci` need a PostgreSQL that matches CI. Use a throwaway container. Never point `TEST_DATABASE_URL` at a database you keep: tests truncate and delete rows ([engine-db-local.md](../../../docs/runbooks/engine-db-local.md)). Never reuse the running `poc68-isolated` stack's databases.

## Why a Unix socket

Measured in #85 and #86 on WSL2 with Docker: over `127.0.0.1:<port>` the full suite randomly lost connections (`server closed the connection unexpectedly`), with nothing in the server log, and `main` behaved the same. Over a Unix socket the full suite passed four runs in a row (402 passed, 4 skipped). Use the socket.

## Start

Use the same image tag as `.github/workflows/ci.yml`. The socket path must be short (Unix socket paths are limited to about 107 bytes), so use `/dev/shm`, not the long scratch path. Put the socket directory under a parent you own: the container makes the directory belong to its postgres user, and only the owner of the parent can remove it afterwards.

```bash
BASE=/dev/shm/$USER-pg; SOCK=$BASE/s; NAME=pg-test
mkdir -p "$SOCK" && chmod 777 "$SOCK"
PW=$(openssl rand -hex 16)
docker run -d --rm --name "$NAME" -e POSTGRES_USER=engine -e POSTGRES_DB=engine \
  -e POSTGRES_PASSWORD="$PW" -v "$SOCK":/var/run/postgresql pgvector/pgvector:0.8.7-pg15
until [ -S "$SOCK/.s.PGSQL.5432" ] && docker exec "$NAME" pg_isready -U engine >/dev/null 2>&1; do sleep 1; done
export TEST_DATABASE_URL="postgresql://engine:$PW@/engine?host=$SOCK" REQUIRE_DB_TESTS=1
```

## Run

```bash
uv run --no-sync pytest -q -p no:cacheprovider tests     # full suite
make ci                                                  # pre-commit, pytest, traceability (needs Docker)
```

If an earlier run left rows that break `tests/test_engine_db.py` (foreign-key errors on `DELETE FROM runs`), reset the throwaway database: `docker exec "$NAME" psql -U engine -d engine -c "TRUNCATE runs CASCADE"`. A test that creates runs must clean them up itself (see `_clean` in `tests/test_execution.py`).

## Clean up

Stop the container gracefully so PostgreSQL removes its own socket files, then remove both directories. `docker rm -f` kills it and leaves socket files that belong to the container user. If that happened, delete them with the local image instead of sudo: `docker run --rm --entrypoint rm -v /dev/shm:/d pgvector/pgvector:0.8.7-pg15 -rf /d/$USER-pg`.

```bash
docker stop "$NAME" && rmdir "$SOCK" "$BASE"
```

## Limits

This proves the tests against PostgreSQL only. It does not start Odoo, Mailpit, or the full Compose stack ([full-stack-local.md](../../../docs/runbooks/full-stack-local.md)).
