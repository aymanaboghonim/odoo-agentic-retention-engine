---
paths:
  - "docs/**"
---

# Docs rules

Hard rules are in `AGENTS.md`; these are the ones for writing docs.

- Simple English; keep technical terms as they are. Arabic only for customer-facing text, test data, and optional summaries.
- Prices, model names, limits, and versions come from a verified row in the facts register, with a URL and a date. Say "unknown" and add an open question instead of guessing.
- Never edit an accepted ADR; write a new one that supersedes it.
- Every invariant in a spec has a named test or CI check.
- Relative links must resolve (the offline link check runs in `make ci`).
