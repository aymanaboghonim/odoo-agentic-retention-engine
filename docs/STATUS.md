# Status

Single place for "where we left off". Update it in every PR.

Next action: review and merge the Phase 0a PR (issue #1), then start Phase 0b.

## Phases

- [ ] **Phase 0a: Capture** (issue #1)
  - [x] Vision: [00-vision.md](00-vision.md)
  - [x] Process rules: [process.md](process.md)
  - [x] Facts register: [facts-register.md](facts-register.md)
  - [x] ADR-0001 to ADR-0008 in [adr/](adr/)
  - [x] ACKNOWLEDGEMENTS.md
  - [x] This status file with open questions and extraction checklist
  - [ ] Owner reviews and merges the PR
- [ ] **Phase 0b: Automation** (issues to be created after 0a)
  - [ ] Rename repo to `odoo-agentic-retention-engine`, restructure folders (target tree below), remove old stub READMEs
  - [ ] Apache-2.0 LICENSE, new README
  - [ ] `AGENTS.md` and skills in `.github/skills/`
  - [ ] Issue and PR templates, labels
  - [ ] GitHub Project v2 board and milestones POC-0 to POC-3
  - [ ] `make ci` and GitHub Actions with the same checks: markdown lint, link check, gitleaks, commit and PR link-to-issue check, facts-register freshness check
- [ ] **Phase 0c: Blueprint extraction, then delete `blueprint-draft.md`** (see checklist below)
- [ ] **Phase 1: Spikes** (each ends with an ADR and updated facts)
  - [ ] S1 Odoo: version, demo data content, RAM, seed need
  - [ ] S2 LLM evaluation on golden set
  - [ ] S3 Retrieval: pgvector vs Qdrant, embedding model support
  - [ ] S4 Telegram bot and Mailpit
- [ ] **Phase 2: POC walking skeleton** (detailed issues written just in time)
- [ ] **Phase 3: POC exit review, MVP plan**

## Target repo tree

`AGENTS.md`, `README.md`, `LICENSE`, `ACKNOWLEDGEMENTS.md`, `Makefile`, `.github/` (workflows, templates, `skills/`), `docs/` (vision, status, facts-register, process, `architecture/`, `adr/`, `specs/`, `runbooks/`), `src/engine/`, `evals/`, `tests/`, `deploy/`, `scripts/`, `course/` (thin links to real docs).

## Blueprint extraction checklist

`blueprint-draft.md` must not be deleted until every box is ticked. Section numbers refer to the draft.

- [x] 1 Problem and goals: [00-vision.md](00-vision.md) (SLOs marked "target, untested")
- [ ] 1.2 Safety gate rules to `docs/specs/`. Tiers accepted by owner (2026-10-24):
  - Auto: discount up to 10% and final price above the product category minimum margin.
  - Human approval: above 10% up to 15%, or any bulk campaign.
  - Hard reject in code: above 15%. A CI test must prove it.
  - Extra caps: one coupon per customer per cart per day, and a daily total discount budget.
  - Minimum margin per category and the daily budget are config values. Starting numbers (10%, 15%) are defaults, to be checked in S1 and on the golden set.
- [x] 2 Headless engine pattern: ADR-0002
- [ ] 2 and 4.1 Context and container diagrams (C1, C2, C3 for engine) to `docs/architecture/`
- [x] 3 PoC vs MVP split: [00-vision.md](00-vision.md) and phases above
- [ ] 4.2 Data connections table (ports, auth, exposure) to `docs/architecture/`
- [ ] 4.3 Three data channels (read-only SQL, Odoo API, RAG) to `docs/architecture/`
- [ ] 5 Agent state machine and HITL flow to `docs/specs/`
- [x] 6 ADR-0001 to 0004: re-written as accepted ADRs (changed: separate AI database, batch instead of streaming)
- [ ] 7.1 Compliance: data residency, PII masking, retention, right to be forgotten to `docs/specs/`
- [ ] 7.2 AI threat model (prompt injection, tool misuse, data leakage) to `docs/specs/`
- [ ] 8.1 Evals, release gates, rollback criteria to `docs/specs/`
- [ ] 8.2 Capacity and cost estimate to `docs/architecture/` (numbers go to facts register)
- [ ] 9 Repo structure and course mapping to `course/`

## Capture map (approved decisions and where they live)

- [x] Goal, scope, non-goals, POC to MVP path, no GPU in MVP, synthetic data: vision
- [x] Odoo as black box, separate AI database: ADR-0001
- [x] Modular monolith, import-linter gate: ADR-0002
- [x] LLM interface, candidates, eval-based choice: ADR-0003
- [x] Batch over streaming: ADR-0004
- [x] Telegram first, WhatsApp in MVP, Mailpit: ADR-0005
- [x] pgvector vs Qdrant benchmark: ADR-0006
- [x] Free-tier observability, OPEX focus: ADR-0007
- [x] Language policy: ADR-0008
- [x] Process rules, traceability, risk-based testing: process.md
- [x] Volatile facts to verify: facts-register.md
- [x] Acknowledgement of Eng. Sameh Amin: ACKNOWLEDGEMENTS.md and vision
- [x] Odoo abandoned-cart email is the as-is baseline: vision (pain point 2)
- [ ] Repo rename, license Apache-2.0, history cleanup before public: tracked in Phase 0b (not done yet)
- [ ] C4 tool choice (Mermaid default vs Structurizr): needs an ADR in Phase 0b

## Open questions

| # | Question | Resolved by |
| :-- | :-- | :-- |
| Q1 | Owner weekly hours | Owner, to size milestones |
| Q2 | Final LLM model | S2, then update ADR-0003 |
| Q3 | Final retrieval store | S3, then new ADR superseding ADR-0006 |
| Q4 | Odoo version (17 or newer) | S1 |
| Q5 | C4 tooling: Mermaid or Structurizr | ADR in Phase 0b |
| Q6 | Free email service for MVP | MVP planning |
| Q7 | History cleanup and force push before going public | Owner approval at that time |
| Q8 | ~~Discount thresholds~~ Resolved 2026-10-24: tier approach accepted (see checklist item 1.2). Defaults still to be validated in S1. | Done |
