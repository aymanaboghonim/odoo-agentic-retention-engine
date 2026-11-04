# Spec: Engine foundation (run state, audit, config)

- Issue: #62
- Parent spec: [agent-state-machine.md](agent-state-machine.md)
- ADRs: ADR-0001 (engine owns its database), ADR-0002 (modular monolith)
- Status: agreed

## Purpose

Give the engine a durable run, an audit trail and an enforced state machine, so
every later POC-2 issue builds on one tested base.

## Scope and non-goals

In scope: engine PostgreSQL schema and Alembic migrations, fail-closed config,
the `RunState` transition table, and a `RunRepository` that is the only way to
change run state. Out of scope: proposals, approvals, policy results content,
Odoo access, the HTTP API and the worker trigger (later issues).

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| AS-1 | A run moves only through transitions in the table; terminal states never return to active states. | `test_run_rejects_invalid_state_transitions`, `test_terminal_states_cannot_return_to_active_states`, `test_invalid_transition_is_not_persisted` |
| AS-2 | Run state and audit survive a restart; every transition writes an audit event in the same database transaction. | `test_run_state_and_audit_survive_restart` |
| AS-7 | A failed run records its reason and is never shown as completed. | `test_failed_run_has_explicit_failure_outcome` |
| FD-1 | Missing or invalid configuration stops startup; no default database is invented. | `test_missing_config_fails_closed` |
| FD-2 | Two writers cannot both move a run from the same state (compare-and-set). | `test_stale_transition_is_rejected` |
| FD-3 | A repeated trigger with the same idempotency key returns the existing run (base for AS-6). | `test_repeated_trigger_returns_same_run` |

## Key behaviors

- The transition table in `src/engine/runs.py` mirrors the parent spec and is
  enforced in Python; the database update is `WHERE state = <expected>`.
- Schema is created only by Alembic migrations in `src/engine/migrations/`.
- Config comes from `ENGINE_DATABASE_URL`; `postgresql://` is accepted and mapped
  to the psycopg driver.
- DB tests use `TEST_DATABASE_URL`. GitHub Actions provides a Postgres service
  container and sets `REQUIRE_DB_TESTS=1`, so missing DB fails there. Locally
  they skip with a notice when the variable is not set (see the
  [runbook](../runbooks/engine-db-local.md)).

## Risks

- Local `make ci` can skip DB tests; GitHub Actions is the enforcing gate.
- Raw SQL could bypass the table; only the repository API is supported.
