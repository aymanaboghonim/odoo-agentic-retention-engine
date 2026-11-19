# ADR-0028: Explicit PostgreSQL run state machine instead of LangGraph

- Status: accepted
- Date: 2026-11-18
- Issue: #100
- Supersedes in part: [ADR-0002](0002-modular-monolith.md) (the "LangGraph" stack entry and the import-linter CI gate) and [ADR-0001](0001-odoo-system-of-record-and-ai-database.md) ("LangGraph checkpoints" in the AI database). The rest of both ADRs stays.

## Context

ADR-0002 planned LangGraph for the agent workflow and import-linter to enforce module boundaries. Neither was built. The engine runs each cart as a run with an explicit transition table (`src/engine/runs.py`, from the [state machine spec](../specs/agent-state-machine.md)). Every state change goes through one repository method that writes the new state and its audit event in the same PostgreSQL transaction (`src/engine/db.py`). Idempotency keys, approvals, and budget reservations live in the same database.

The flow is linear: scan, eligibility, context, one LLM call with structured output and no tools, gate, optional human approval, gate recheck, execute. Safety depends on transactional state: no side effect before approval (AS-3), and no duplicate coupon or message (AS-6).

## Decision

1. Keep the explicit state machine in PostgreSQL as the only store of run state. Do not add LangGraph or another orchestration framework for the current flow.
2. Describe the runtime as an agentic workflow: code runs fixed steps, and the LLM is one bounded step with structured output and no tools. Humans approve actions in the approval band.
3. Reconsider a tool-using agent when a slice needs the model to choose among several tools or steps. The first candidate is the next-best-action slice over Odoo order history (RFM/CLV). That slice starts with a spike and an ADR that compares frameworks (LangGraph included) with extending the state machine. Any tool-using step keeps the same gate before every side effect.
4. Module boundaries are not enforced in CI today. A follow-up issue adds an import boundary check.

## Alternatives

- **Adopt LangGraph now:** it adds no behavior to a linear flow with one LLM call. Its checkpointer would be a second store of run state beside the audited run table, so keeping both consistent becomes a new source of duplicate side effects. The tested invariants (AS-1..AS-7), the release-gate evaluation, and the live AWS run would all need to be repeated. Rejected for the current scope.
- **Keep ADR-0002 unchanged:** the docs would keep describing a framework that is not in the code. Rejected.

## Consequences

- Good: one source of truth for run state, with audit and idempotency in the same transaction. No extra dependency on Python 3.14.
- Cost: retries, checkpoints, and graph views are our own code. A future tool loop needs its own design.
- The C3 diagram in [system-overview.md](../architecture/system-overview.md) shows the state machine instead of LangGraph.
