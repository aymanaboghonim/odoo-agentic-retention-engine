---
paths:
  - "src/engine/**"
  - "scripts/**"
---

# Engine rules

Hard rules are in `AGENTS.md`; these are the ones most often touched in code.

- The LLM never decides money. Discount limits live in the gate and are tested.
- Never write to the Odoo database. Read through the read-only role; change Odoo only through the supported JSON-2 API.
- No raw customer name, email, or phone at rest or in LLM and log payloads: per-run tokens only, restored from the live Odoo cart at dispatch ([privacy spec](../../docs/specs/privacy-consent-retention.md)).
- Fail closed: missing or invalid settings stop startup; unknown input is rejected.
- Side-effecting commands need a preflight, progress output, and a failure report ([process.md](../../docs/process.md)); output never prints tokens, pepper, or customer data.
