---
name: verify-facts
description: 'Use when a decision, doc, config or code depends on a price, model name, free-tier limit, version, quota or product capability that can change (Gemini, Azure OpenAI, Odoo, Langfuse, Grafana, Hetzner, Cloudflare, GitHub features). Verifies it from official docs and records it in the facts register.'
---

# Verify facts

Volatile facts must come only from official sources or our own measurements, with the URL and date, in [docs/facts-register.md](../../../docs/facts-register.md). Memory, owner claims, and community answers are starting points, not proof.

## Rules

- Statuses: `unverified`, `verified`, `stale` (checked more than 90 days ago).
- Do not use an `unverified` or `stale` fact in an accepted ADR, a spec or code defaults.
- Use only official sources: the vendor's or project's own docs, pricing pages, release notes, changelogs or blog, source repository, and API responses, or our own measurement. Forum posts, community answers (Stack Overflow, Reddit, GitHub Discussions, `community.*` sites), third-party blogs and reviews, and AI answers are never evidence, whatever their date; use them only to find where to look. `scripts/check_facts_sources.py` (in `make ci`) refuses their domains.
- If no official source states the value, write "unknown (not stated in official docs)" and do not let a design depend on it.
- If you cannot reach official sources, leave the row `unverified` and ask the owner to check. Do not fill in a guess.
- Record what you saw, not what you expect: exact value, plan or tier, region, and the date.

## Procedure

1. Find the row by ID. If none exists, add a new row with the next ID.
2. Open the official page. Copy the exact value, plan or tier and conditions.
3. Update the row: value, source URL, checked on (YYYY-MM-DD), status `verified`.
4. If the real value differs from the claim, say so in the row and in the PR. Update any ADR or spec that used the old value (new ADR if the ADR was accepted).
5. For a measurement (RAM, latency, Recall@5), record the method and the command or script that produced it: write the source as `measured: <command, script, or setup>` (the CI check requires a method after `measured`).
