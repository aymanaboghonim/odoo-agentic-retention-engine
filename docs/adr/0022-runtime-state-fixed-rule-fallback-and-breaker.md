# ADR-0022: Engine runtime state, fixed-rule fallback, and circuit breaker

- Status: accepted
- Date: 2026-11-16
- Issue: #87

## Context

`Gate` protects money, but nothing protects against a misbehaving model or provider. The draft release-gate spec left open what the fallback is, who re-enables the agent, and whether rollback is automatic. The cloud go-live (`#88`) needs a way to stop or degrade the agent without a deployment. Details are in [release-gates-and-kill-switch.md](../specs/release-gates-and-kill-switch.md).

## Decision

1. **A durable runtime state in the engine database** with three values: `active`, `fallback_fixed_rule`, `halted`. It is one row with a version, plus an audit table. Only `admin` can change it (ADR-0012 keeps all side effects in the worker, so the worker reads this state and the API only writes it).
2. **The fallback is our own deterministic 0%-discount reminder** in English and MSA. It uses the same proposal persistence, `Gate`, consent, allowlist, PII tokens, and approval path, and makes no LLM call.
3. **A circuit breaker may only move `active` to `fallback_fixed_rule`.** It never re-enables and never halts. An admin sets `halted` and returns to `active`.
4. **Breaker thresholds have no defaults.** The owner sets them from the measured baseline of the production-scale evaluation; until then the breaker is off and the manual switch is the control.
5. **Fail closed:** an unreadable state behaves as `halted`.

## Alternatives

- **Odoo's built-in abandoned-cart email as the fallback:** rejected. It is off by default (`F-003`, unverified) and would bypass the engine's consent, allowlist, PII, and audit controls.
- **An automatic breaker that can also halt:** rejected for now. A noisy metric could silence the whole engine without a human. Revisit with measured data.
- **Draft thresholds (2% error rate, P95 2,000 ms over 5 minutes):** rejected as defaults. They were never measured and may trip constantly.
- **An environment flag instead of a database row:** rejected. A flag needs a redeploy to change and has no audit trail.

## Consequences

- The agent can be degraded or stopped in seconds by an admin, with a recorded actor and reason.
- Every guard adds one small database read per operation, and each must fail closed.
- A held run (halted or fallback) is resumable until its approval expires ([operator RBAC spec](../specs/operator-rbac-and-expiring-approvals.md)).
- The breaker settings must be measured before the breaker is enabled in the cloud (`#88`).
