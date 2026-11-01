# Status

Single place for "where we left off". Update it in every PR.

Next action: owner reviews and approves the v3 [human-readable S2 golden set](../evals/llm/s2_review_worksheet.md); qualified reviewers record each dialect. Dataset v1 was returned with changes requested and v2 received final wording corrections, so no earlier approval carries forward. PR #55 remains draft and issue #46 remains open: passing CI validates the harness only, not the golden set or a model evaluation. No live evaluation has run. Only after dataset approval and the Odoo/provider/token gates are complete may the bounded smoke run; stop for report review before any full Batch comparison.

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
- [x] **Phase 0c: Blueprint extraction complete; `blueprint-draft.md` removed in issue #31**
- [ ] **Phase 1: Spikes** (each ends with an ADR and updated facts)
  - [x] S1 Odoo (#43): Odoo 19.0, demo data has aged abandoned carts so no seed needed, reminder is opt-in, about 360 MiB idle ([ADR-0014](adr/0014-odoo-19-baseline-and-seeding.md)); startup/probe progress follow-up #53
  - [x] #51 Project-wide preflight, progress, failure/output, and human-review-report standards ([spec](specs/project-wide-run-governance.md); PR #54 merged). S3 remains sequential for isolated per-query ranking/latency only; throughput is not measured.
  - [ ] #46 S2 bilingual LLM golden-set evaluation (in progress; see [spec](specs/s2-llm-golden-set-evaluation.md) and proposed [ADR-0016](adr/0016-s2-llm-evaluation-and-model-selection.md))
  - [x] S3 Retrieval (#47): benchmark and decision complete; PostgreSQL FTS + pgvector with 80:20 SQL-CTE fusion is recorded in ADR-0015; PR #52 merged
  - [ ] S4 Telegram bot and Mailpit
- [ ] **Phase 2: POC walking skeleton** (detailed issues written just in time)
- [ ] **Phase 3: POC exit review, MVP plan**

## Target repo tree

`AGENTS.md`, `README.md`, `LICENSE`, `ACKNOWLEDGEMENTS.md`, `Makefile`, `.github/` (workflows, templates, `skills/`), `docs/` (vision, status, facts-register, process, `architecture/`, `adr/`, `specs/`, `runbooks/`), `src/engine/`, `evals/`, `tests/`, `deploy/`, `scripts/`, `course/` (thin links to real docs).

## Blueprint extraction checklist

The draft was a planning input, not an authority. Accepted ADRs and owner-approved decisions in specs and STATUS take precedence over conflicting draft claims. Volatile facts are governed by the facts register; unverified claims remain assumptions until checked.

Section numbers below referred to the removed draft.

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
- [x] 6 ADR-0001 to 0004 captured; ADR-0003 remains proposed until S2 records the model outcome in ADR-0016 (changes from draft include the separate AI database and batch processing)
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
- [x] Telegram first for POC, WhatsApp planned for MVP subject to verification, Mailpit: ADR-0013
- [x] pgvector vs Qdrant benchmark: ADR-0006
- [x] Free-tier observability, OPEX focus: ADR-0007
- [x] Language policy: ADR-0008
- [x] Process rules, traceability, risk-based testing: process.md
- [x] Volatile facts to verify: facts-register.md
- [x] S2 owner choices and assumptions: [S2 spec](specs/s2-llm-golden-set-evaluation.md); model outcome remains proposed in [ADR-0016](adr/0016-s2-llm-evaluation-and-model-selection.md)
- [x] Acknowledgement of Eng. Sameh Amin: ACKNOWLEDGEMENTS.md and vision
- [x] Odoo abandoned-cart reminder is recorded as an unverified baseline assumption (F-003); verify in S1.
- [x] C4 tool choice: Mermaid, ADR-0010; diagram syntax changed to flowcharts, ADR-0011

## Open questions

| # | Question | Resolution / status |
| :-- | :-- | :-- |
| Q2 | Final LLM model/configuration | Complete the reviewed S2 comparison, then finalize [ADR-0016](adr/0016-s2-llm-evaluation-and-model-selection.md) and ADR-0003 |
| Q3 | Final retrieval store | Resolved 2026-10-31: PostgreSQL FTS + pgvector with SQL-CTE fusion, 80:20 dense:lexical RRF, and disjunctive lexical candidates. See [ADR-0015](adr/0015-postgresql-retrieval-store.md); PR #52 merged. Revisit at POC exit before MVP design is locked. |
| Q4 | Odoo version (17 or newer) | Done. Resolved 2026-10-29: 19.0, [ADR-0014](adr/0014-odoo-19-baseline-and-seeding.md). 19.0 support/EOL dates unverified. |
| Q5 | C4 tooling: Mermaid or Structurizr | Done. Resolved 2026-10-25: Mermaid, ADR-0010 (diagram syntax: flowcharts, ADR-0011) |
| Q6 | Free email service for MVP | MVP planning |
| Q7 | History cleanup and force push before going public | Owner approval at that time |
| Q8 | ~~Discount thresholds~~ Resolved 2026-10-24: tier approach accepted (see checklist item 1.2). Defaults still to be validated in S1. | Done; validate defaults in S1 |
| Q9 | Should we adopt any skill, agent, instruction, hook or workflow from `github/awesome-copilot`? | Done. Resolved 2026-10-24: no bulk adoption. Consider individual items only for a concrete need, after reviewing content, scripts, permissions and license. |
| Q10 | Planning hierarchy for POC/MVP: phases, parent issues, child tasks, user stories | Done. Resolved 2026-10-25: milestone per phase; parent issue only when an outcome spans multiple issues; keep spec, spike, bug and task types; use user-story format only for user-facing behavior. |
| Q11 | Ports, protocols, authentication and network layout for each connection | Open. Decide with the first relevant adapter issue; then update [data-connections.md](architecture/data-connections.md) |
| Q12 | Applicable data protection laws, residency region, retention period, deletion process, chat authentication | Open. Needs legal review before real customer data; see [data-protection-threat-model.md](specs/data-protection-threat-model.md) |
| Q13 | Numeric release gates and rollback triggers, fallback definition, who re-enables the agent | Open. Decide after spikes S2 and S3; see [evals-release-gates.md](specs/evals-release-gates.md) |
| Q14 | Current POC host plan, capacity, production topology and recurring costs | Open. S1 measured Odoo + PostgreSQL only (about 360 MiB idle, [F-014](facts-register.md)). Full stack in Phase 2; model and retrieval after S2/S3; see [capacity-cost-estimate.md](architecture/capacity-cost-estimate.md) |
| Q15 | Qualified reviewers for the five named Arabic dialects | Open. Record reviewer approval per dialect in the S2 dataset review worksheet before those cases are runnable |
| Q16 | Active provider RPM/TPM for smoke and Batch queue limits | Open. Verify from the provider project dashboards and record source/date before any live request or Batch submission |
| Q17 | Full-run input/output token limits | Open by design. Derive from the frozen dataset and smoke usage, then get owner approval before the full run; do not truncate prompts or outputs |
| Q18 | Odoo 19 JSON-2 field and write contract for the S2 smoke | Open. The read-only `fields_get` preflight is implemented but has not been run against the disposable instance; confirm the fields and create/read/activity lifecycle before any provider smoke, then update F-028 |
