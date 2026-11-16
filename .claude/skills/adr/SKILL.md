---
name: adr
description: 'Use when a change makes, records, changes, or supersedes an architecture decision (database, framework, channel, model, hosting, tooling, process), or when the owner asks for an ADR. Creates a numbered ADR in docs/adr with context, decision, alternatives, and consequences; never edits an accepted ADR.'
---

# Architecture decision record (ADR)

ADRs live in [docs/adr/](../../../docs/adr/). Existing ADRs are the style guide.

## Rules

- File name: `NNNN-short-kebab-title.md`. NNNN is the next free number.
- Status: `proposed`, `accepted` or `superseded`.
- Never edit an accepted ADR, except to set its status to `superseded by ADR-NNNN`. To change a decision, write a new ADR.
- A decision that depends on a volatile fact (price, limit, version) must link to its row in the facts register. Do not accept an ADR while a fact it needs is `unverified`.
- Keep it to one page. Simple English.

## Template

```markdown
# ADR-NNNN: Title

- Status: proposed
- Date: YYYY-MM-DD

## Context

What problem, and what limits (budget, hardware, team, compliance).

## Decision

What we do. Be specific.

## Alternatives

Each option and why it was not chosen.

## Consequences

Good and bad results, and what we must watch.
```

## Procedure

1. Check [docs/adr/](../../../docs/adr/) so you do not repeat an existing decision.
2. Write the ADR from the template. Use `proposed` if a spike or measurement will decide.
3. Record any open question in the Open questions table in [docs/STATUS.md](../../../docs/STATUS.md), with what resolves it.
4. If it replaces an older ADR, set the old status to `superseded by ADR-NNNN`.
5. Link the ADR in the PR and mention the issue.
