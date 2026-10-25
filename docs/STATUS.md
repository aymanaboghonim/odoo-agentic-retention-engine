# Status

Single place for "where we left off". Update it in every PR.

Next action: issue #31 deletes `blueprint-draft.md` and closes Phase 0c; every extraction item is now recorded in its source document. Then begin Phase 1 spikes.

## Phases

- [x] **Phase 0a: Capture** (issue #1, PR #2, merged)
- [x] **Phase 0b: Automation** (milestone POC-0)
  - [x] #3 `.gitignore` and `.env.example` (first, before any code)
  - [x] #4 Apache-2.0 LICENSE, new README
  - [x] #5 Restructure folders to the target tree, remove old stub READMEs
  - [x] #6 `AGENTS.md` and skills in `.github/skills/` (pr-workflow, adr, spec-writing, verify-facts; odoo-adapter, rag-eval, infra-deploy, release are added when first needed; custom agents deferred to Phase 3)
  - [x] #7 Issue and PR templates, labels
  - [x] #8 `make ci` and GitHub Actions with the same checks, Python 3.14 only, locked dev tools and no app runtime dependencies yet (PR #17, merged)
  - [x] #9 Active `protect-main` ruleset verified: PR + `Local-parity checks`, linear history, no force-push/deletion, no bypass actors; squash-only and auto-delete are enabled (PR #18, merged).
  - [x] #10 GitHub Project v2 board #14 (Status: Todo, In Progress, Done; built-in workflows only, checked in the UI) and milestones POC-1 to POC-3. F-015 verified (PR #19, merged).
  - [x] #11 Rename repo to `odoo-agentic-retention-engine` (done 2026-10-25; ruleset and board checked after rename; PR #20, merged)
  - [x] #21 ADR-0010 C4 diagram tooling: Mermaid (PR #22, merged)
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
- [x] 1.2 Safety gate rules: [safety-gate.md](specs/safety-gate.md). Tiers accepted by owner (2026-10-24):
  - Auto: discount up to 10% and final price above the product category minimum margin.
  - Human approval: above 10% up to 15%, or any bulk campaign.
  - Hard reject in code: above 15%. A CI test must prove it.
  - Extra caps: one coupon per customer per cart per day, and a daily total discount budget.
  - Minimum margin per category and the daily budget are config values. Starting numbers (10%, 15%) are defaults, to be checked in S1 and on the golden set.
- [x] 2 Headless engine pattern: ADR-0002
- [x] 2 and 4.1 Context and container diagrams (C1, C2, C3 for engine): [system-overview.md](architecture/system-overview.md)
- [x] 3 PoC vs MVP split: [00-vision.md](00-vision.md) and phases above
- [x] #35 Refine the diagrams: LLM provider, human approval, workflow-to-adapters path, and worker-owned side effects ([ADR-0012](adr/0012-worker-owned-agent-execution.md))
- [x] 4.2 and 4.3 Data connections and three data channels (decided items only; ports and auth deferred): [data-connections.md](architecture/data-connections.md)
- [x] 5 Agent state machine and HITL flow: [agent-state-machine.md](specs/agent-state-machine.md)
- [x] 6 ADR-0001 to 0004: re-written as accepted ADRs (changed: separate AI database, batch instead of streaming)
- [x] 7.1 and 7.2 Data protection and AI threat model (legal details are open questions): [data-protection-threat-model.md](specs/data-protection-threat-model.md)
- [x] 8.1 Evals, release gates, rollback criteria (numeric thresholds are open questions): [evals-release-gates.md](specs/evals-release-gates.md)
- [x] 8.2 Capacity and cost estimate (POC host needs S1 measurement; production and variable costs remain open): [capacity-cost-estimate.md](architecture/capacity-cost-estimate.md)
- [x] 9 Repo structure and course mapping to [course/README.md](../course/README.md)

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
- [ ] History cleanup before public: owner approval needed (Q7). Repo rename and Apache-2.0 license are done.
- [x] C4 tool choice: Mermaid, ADR-0010; diagram syntax changed to flowcharts, ADR-0011

## Open questions

| # | Question | Resolution / status |
| :-- | :-- | :-- |
| Q1 | Owner weekly hours | Owner, to size milestones |
| Q2 | Final LLM model | S2, then update ADR-0003 |
| Q3 | Final retrieval store | S3, then new ADR superseding ADR-0006 |
| Q4 | Odoo version (17 or newer) | S1 |
| Q5 | C4 tooling: Mermaid or Structurizr | Done. Resolved 2026-10-25: Mermaid, ADR-0010 (diagram syntax: flowcharts, ADR-0011) |
| Q6 | Free email service for MVP | MVP planning |
| Q7 | History cleanup and force push before going public | Owner approval at that time |
| Q8 | ~~Discount thresholds~~ Resolved 2026-10-24: tier approach accepted (see checklist item 1.2). Defaults still to be validated in S1. | Done; validate defaults in S1 |
| Q9 | Should we adopt any skill, agent, instruction, hook or workflow from `github/awesome-copilot`? | Done. Resolved 2026-10-24: no bulk adoption. Consider individual items only for a concrete need, after reviewing content, scripts, permissions and license. |
| Q10 | Planning hierarchy for POC/MVP: phases, parent issues, child tasks, user stories | Open. Proposed: milestone per phase; use a parent issue only for an outcome split across several issues; keep existing spec, spike, bug and task issue types; user-story format only for user-facing behavior. Decide before POC-1 breakdown. |
| Q11 | Ports, protocols, authentication and network layout for each connection | Open. Decide with the first adapter issue or the infra spike; then update [data-connections.md](architecture/data-connections.md) |
| Q12 | Applicable data protection laws, residency region, retention period, deletion process, chat authentication | Open. Needs legal review before real customer data; see [data-protection-threat-model.md](specs/data-protection-threat-model.md) |
| Q13 | Numeric release gates and rollback triggers, fallback definition, who re-enables the agent | Open. Decide after spikes S2 and S3; see [evals-release-gates.md](specs/evals-release-gates.md) |
| Q14 | Current POC host plan, capacity, production topology and recurring costs | Open. Measure Odoo and stack use in S1; estimate model and retrieval after S2/S3; see [capacity-cost-estimate.md](architecture/capacity-cost-estimate.md) |
