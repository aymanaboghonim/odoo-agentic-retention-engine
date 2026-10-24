---
name: verify-facts
description: 'Use when a decision, doc, config or code depends on a price, model name, free-tier limit, version, quota or product capability that can change (Gemini, Azure OpenAI, Odoo, Langfuse, Grafana, Hetzner, Cloudflare, GitHub features). Verifies it from official docs and records it in the facts register.'
---

# Verify facts

Volatile facts must come from official documentation, with the URL and date, in [docs/facts-register.md](../../../docs/facts-register.md). Memory and owner claims are starting points, not proof.

## Rules

- Statuses: `unverified`, `verified`, `stale` (checked more than 90 days ago).
- Do not use an `unverified` or `stale` fact in an accepted ADR, a spec or code defaults.
- Prefer official docs, pricing pages and API responses. Not blogs, forums or model memory.
- If you cannot reach official sources, leave the row `unverified` and ask the owner to check. Do not fill in a guess.
- Record what you saw, not what you expect: exact value, plan or tier, region, and the date.

## Procedure

1. Find the row by ID. If none exists, add a new row with the next ID.
2. Open the official page. Copy the exact value, plan or tier and conditions.
3. Update the row: value, source URL, checked on (YYYY-MM-DD), status `verified`.
4. If the real value differs from the claim, say so in the row and in the PR. Update any ADR or spec that used the old value (new ADR if the ADR was accepted).
5. For a measurement (RAM, latency, Recall@5), record the method and the command or script that produced it, and mark the source as "measured".
