# Runbook: local approval API and CLI

Operator authentication uses OIDC/JWKS access tokens (#86, spec
[operator-rbac-and-expiring-approvals.md](../specs/operator-rbac-and-expiring-approvals.md)).
The API verifies RS256 tokens, maps one claim to `viewer` (list), `approver`
(list and decide) or `admin`, and stores the token `sub` as the audit actor.
There is no static token. Without `APPROVAL_ORIGIN_VERIFY_SECRET` the API binds
to `127.0.0.1` and rejects non-loopback clients. It has no transport encryption;
do not expose it through a proxy or tunnel. The cloud path (CloudFront in front,
`X-Origin-Verify`, a Cognito JWKS URL) is deployed in `#88`.

## Configuration and start

Use the isolated engine database and apply its migrations as described in the
[engine database runbook](engine-db-local.md). In a private shell environment,
set `ENGINE_DATABASE_URL` and the OIDC settings: `OIDC_ISSUER`, `OIDC_CLIENT_ID`,
`OIDC_ROLES_CLAIM`, and exactly one of `OIDC_JWKS_URL` (https) or `OIDC_JWKS_FILE`.
`OIDC_AUDIENCE` is optional; when set, the token `aud` must match. Approvals live
`APPROVAL_TTL_HOURS` hours (1-168, default 24). The optional `APPROVAL_API_PORT`
defaults to 8080.

For local development create a throwaway issuer, point `OIDC_JWKS_FILE` at the
generated `jwks.json`, and mint a short-lived token into your shell. The `.local/`
directory is git-ignored and holds a private key; never commit or share it:

```sh
PYTHONPATH=src:. uv run --no-sync python -m scripts.dev_oidc init --dir .local/dev-oidc
export OIDC_ISSUER=https://dev-oidc.example.test OIDC_CLIENT_ID=dev-client \
  OIDC_ROLES_CLAIM=cognito:groups OIDC_JWKS_FILE=.local/dev-oidc/jwks.json
export APPROVAL_ACCESS_TOKEN=$(PYTHONPATH=src:. uv run --no-sync python -m scripts.dev_oidc mint \
  --dir .local/dev-oidc --issuer "$OIDC_ISSUER" --client-id "$OIDC_CLIENT_ID" \
  --subject dev-operator --role approver)
```

Tokens last 15 minutes by default; mint a new one when the API answers HTTP 401.
In Compose set `FULL_STACK_OIDC_DIR` to the directory holding `jwks.json`.

Apply the engine migrations to the isolated engine database before starting
the API. The API checks that the approval table is available at startup; it
does not create or migrate database tables:

```sh
PYTHONPATH=src uv run --no-sync python -c 'import os; from engine.config import EngineConfig; from engine.db import create_engine, upgrade_schema; engine = create_engine(EngineConfig.from_env(os.environ).database_url); upgrade_schema(engine); engine.dispose()'
```

```sh
PYTHONPATH=src uv run --no-sync python -m engine.approval_api
```

In another private shell, set `APPROVAL_ACCESS_TOKEN` (and the same port), then run:

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

An approval expires at `expires_at` (creation time plus the TTL). After that it is
not listed, cannot be decided (HTTP 409), and the worker refuses to execute it
(the run ends `rejected` with outcome `approval_expired`, or `failed` if the
expiry hits after execution started). The worker also cancels expired pending
approvals on each scan. Approval applies only to the unchanged snapshot and does
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
