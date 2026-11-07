# Runbook: local approval API and CLI

This is a synthetic-only POC interface, not production authentication.
The API binds to `127.0.0.1` and rejects non-loopback clients. It has no
transport encryption; do not expose it through a proxy, remote tunnel, or
shared host. Loopback and a shared token do not isolate users on the same host.
The server does not trust proxy headers.

## Configuration and start

Use the isolated engine database and apply its migrations as described in the
[engine database runbook](engine-db-local.md). In a private shell environment,
set `ENGINE_DATABASE_URL`, `APPROVAL_OPERATOR`, and `APPROVAL_TOKEN`. The
operator name is an audit label, not a verified identity. The token must be
32–512 printable ASCII characters without whitespace; use a unique random
secret and never put it in a command argument or commit it. The optional
`APPROVAL_API_PORT` defaults to 8080.

Apply the engine migrations to the isolated engine database before starting
the API. The API checks that the approval table is available at startup; it
does not create or migrate database tables:

```sh
PYTHONPATH=src uv run --no-sync python -c 'import os; from engine.config import EngineConfig; from engine.db import create_engine, upgrade_schema; engine = create_engine(EngineConfig.from_env(os.environ).database_url); upgrade_schema(engine); engine.dispose()'
```

```sh
PYTHONPATH=src uv run --no-sync python -m engine.approval_api
```

In another private shell, set the same operator, token and port, then run:

```sh
PYTHONPATH=src uv run --no-sync python -m engine.approval_cli list
PYTHONPATH=src uv run --no-sync python -m engine.approval_cli approve --run-id <run-id> --snapshot-hash <hash>
PYTHONPATH=src uv run --no-sync python -m engine.approval_cli reject --run-id <run-id> --snapshot-hash <hash>
```

The list displays the full synthetic snapshot and its hash. Review the content
before deciding; use the corresponding run ID and hash exactly. Only one
decision wins. A conflict means the request is stale or no longer pending;
do not treat it as success. The approval returns the run to policy evaluation;
rejection ends the run. Neither command creates a coupon or sends a message.

Approval has no time expiry. It applies only to the unchanged snapshot and does
not override the gate. The worker must re-read current context and revalidate
the gate and budget before execution (#67). `current_human_approval` checks the
stored snapshot but is not an execution authorization or an atomic execution
claim. #67 must also enforce idempotency and protect against changes between
checking and execution. `Gate.issue_approval` performs the atomic budget
reservation; `Gate.verify` alone does not recheck current ledger usage.
The execution path must enforce the usage cap as well as the other checks.
Today the #65 proposal generator cannot naturally
produce an approval-required discount; tests use synthetic fixtures.

## Tests

With an isolated test database configured as in the
[engine database runbook](engine-db-local.md):

```sh
REQUIRE_DB_TESTS=1 uv run --no-sync pytest -q tests/test_approval.py
```

Tests are hermetic; they do not call Odoo, an LLM, or customer channels.
