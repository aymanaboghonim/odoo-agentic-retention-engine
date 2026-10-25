# ADR-0012: Worker-owned agent execution and side effects

- Status: accepted
- Date: 2026-10-25

## Context

The engine has an API and a scheduled worker in one modular monolith ([ADR-0002](0002-modular-monolith.md)). Agent runs need durable state, human approval, and safe execution ([agent-state-machine.md](../specs/agent-state-machine.md)). If both entrypoints execute actions, retries and approval handling can cause duplicate or inconsistent side effects.

## Decision

- The API accepts external triggers, inbound channel events, and human approval decisions. It validates and persists the request or decision in the AI database; it does not run agent workflows or perform customer-facing side effects.
- The worker processes all persisted runs, including interactive and scheduled triggers. It claims ready runs from the AI database, runs the workflow, and is the only entrypoint allowed to call Odoo write operations or dispatch customer messages.
- The worker uses the adapters for context reads and LLM calls before policy evaluation. Odoo writes and message dispatch happen only after the safety gate and any required approval pass.
- The AI database is the durable handoff between API and worker. The worker must claim runs idempotently and record state transitions.

## Alternatives

- **Allow both API and worker to execute:** rejected because side effects would have two competing execution paths.
- **Let the API execute interactive runs, worker execute scheduled runs:** rejected because approval decisions and retries would need different execution behavior based on trigger type.

## Consequences

- One execution path handles scheduled and interactive work, simplifying safety checks, deduplication, and retries.
- Interactive requests may wait for worker polling before completion; API responses should expose the durable run state rather than imply synchronous completion.
- The implementation must provide safe run claiming and idempotency before enabling external side effects.
