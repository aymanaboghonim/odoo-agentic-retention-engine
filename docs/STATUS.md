# Status

Single place for "where we left off". Update it in every PR.

Next action: merge the #56 PR, then plan #63 (Odoo adapter), #64, #65 and #66 just in time. #63 to #66 all depend only on the merged #62. Each issue gets its own detailed plan just in time before work starts. POC-2 scope is planned in #61. Phase 1 is complete: S1 to S4 and their follow-ups (#49, #51, #53) are merged and POC-1 has no open issues. S2 outcome in [ADR-0016](adr/0016-s2-llm-evaluation-and-model-selection.md) (accepted for the POC): GPT-6 Luna `low` with the `tool-clarity` prompt (128/132 vs 115/132 baseline; one prompt-injection regression accepted as possible noise, one run per prompt). Limits: 4 remaining failures, no retained model text, bounded discount matcher. The production discount/margin, coupon and dispatch gate does not exist; end-to-end deterministic rejection is untested and is tracked in #56 (POC-2).

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
- [x] **Phase 1: Spikes** (each ends with an ADR and updated facts)
  - [x] S1 Odoo (#43): Odoo 19.0, demo data has aged abandoned carts so no seed needed, reminder is opt-in, about 360 MiB idle ([ADR-0014](adr/0014-odoo-19-baseline-and-seeding.md)); preflight, stage progress, and `--wait-ready` for the probe/seed added in #53 ([spec](specs/s1-odoo-preflight-progress.md)); verified with hermetic tests only, not against a running Odoo
  - [x] #51 Project-wide preflight, progress, failure/output, and human-review-report standards ([spec](specs/project-wide-run-governance.md); PR #54 merged). S3 remains sequential for isolated per-query ranking/latency only; throughput is not measured.
  - [x] #46 S2 bilingual LLM golden-set evaluation (PR #55; Luna `low` + `tool-clarity` prompt chosen for the POC; production financial gate tracked in #56, POC-2; see [evaluation spec](specs/s2-llm-golden-set-evaluation.md), [remediation spec](specs/s2-prompt-and-output-safety-remediation.md), and accepted [ADR-0016](adr/0016-s2-llm-evaluation-and-model-selection.md))
  - [x] S3 Retrieval (#47): benchmark and decision complete; PostgreSQL FTS + pgvector with 80:20 SQL-CTE fusion is recorded in ADR-0015; PR #52 merged
  - [x] S4 Telegram bot and Mailpit (PR #57 merged; local Mailpit smoke and the required dedicated-chat Telegram check passed)
- [ ] **Phase 2: POC walking skeleton** (milestone POC-2; scope planned 2026-11-04 in #61; one vertical slice: abandoned cart, agent proposal, safety gate, approval, Odoo coupon/activity, Telegram/Mailpit; English and MSA Arabic)
  - [x] #62 Engine foundation ([spec](specs/engine-foundation.md), [runbook](runbooks/engine-db-local.md)): engine DB, fail-closed config, durable run, audit, state machine (first; others depend on it)
  - [x] #56 Production financial gate and agent-output rejection (after #62; [ADR-0018](adr/0018-financial-safety-gate-decisions.md), [spec](specs/safety-gate.md); live Luna `low` re-run 127/132 on 2026-11-05, $0.026; see spec)
  - [ ] #63 Odoo adapter: read-only scan and context, JSON-2 activity and coupon writes (after #62)
  - [ ] #64 Policy retrieval with synthetic bilingual policies (after #62)
  - [ ] #65 Agent proposal step: LLM adapter, validation, cost and budget cap (after #62)
  - [ ] #66 Approval API and CLI (after #62)
  - [ ] #67 Gated execution: coupon and Telegram/Mailpit dispatch, idempotency (after #56, #63, #66)
  - [ ] #68 Full-stack compose, worker trigger, capacity measurement (after #63, #64, #65)
  - [ ] #69 End-to-end test, live EN/MSA eval, provisional gates (last)
  - POC-2 decisions (owner, 2026-11-04): approval by API + CLI with static env operator tokens; coupon written through Odoo JSON-2 in the local instance (Odoo 19 coupon model verified first, fallback only by ADR); per-run cost in the engine DB and structured logs, Langfuse/OTel moved to POC-3; dialects only if owner review is recorded in the S2 worksheet, else English + MSA; deferred: Q12, Q13 final gates (provisional only), production topology, churn/RFM.
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
- [x] 6 ADR-0001 to 0004 captured; ADR-0003 accepted 2026-11-03 for the interface boundary and evaluation-based selection, with the S2 model outcome in ADR-0016 (changes from draft include the separate AI database and batch processing)
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
| Q2 | Final LLM model/configuration | Resolved for the POC 2026-11-03: GPT-6 Luna `low` with the `tool-clarity` prompt, [ADR-0016](adr/0016-s2-llm-evaluation-and-model-selection.md). No production-readiness claim; production choice, thresholds (Q13) and the financial gate (#56) stay open. |
| Q3 | Final retrieval store | Resolved 2026-10-31: PostgreSQL FTS + pgvector with SQL-CTE fusion, 80:20 dense:lexical RRF, and disjunctive lexical candidates. See [ADR-0015](adr/0015-postgresql-retrieval-store.md); PR #52 merged. Revisit at POC exit before MVP design is locked. |
| Q4 | Odoo version (17 or newer) | Done. Resolved 2026-10-29: 19.0, [ADR-0014](adr/0014-odoo-19-baseline-and-seeding.md). 19.0 support/EOL dates unverified. |
| Q5 | C4 tooling: Mermaid or Structurizr | Done. Resolved 2026-10-25: Mermaid, ADR-0010 (diagram syntax: flowcharts, ADR-0011) |
| Q6 | Free email service for MVP | MVP planning |
| Q7 | History cleanup and force push before going public | Owner approval at that time |
| Q8 | ~~Discount thresholds~~ Resolved 2026-10-24: tier approach accepted (see checklist item 1.2). Defaults still to be validated in S1. | Done; validate defaults in S1 |
| Q9 | Should we adopt any skill, agent, instruction, hook or workflow from `github/awesome-copilot`? | Done. Resolved 2026-10-24: no bulk adoption. Consider individual items only for a concrete need, after reviewing content, scripts, permissions and license. |
| Q10 | Planning hierarchy for POC/MVP: phases, parent issues, child tasks, user stories | Done. Resolved 2026-10-25: milestone per phase; parent issue only when an outcome spans multiple issues; keep spec, spike, bug and task types; use user-story format only for user-facing behavior. |
| Q11 | Ports, protocols, authentication and network layout for each connection | Partially resolved 2026-11-03 for local POC channels in [ADR-0017](adr/0017-s4-local-channel-transport.md) and [data-connections.md](architecture/data-connections.md). Production topology, transport and authentication remain open. |
| Q12 | Applicable data protection laws, residency region, retention period, deletion process, chat authentication | Open. Needs legal review before real customer data; see [data-protection-threat-model.md](specs/data-protection-threat-model.md) |
| Q13 | Numeric release gates and rollback triggers, fallback definition, who re-enables the agent | Open. Decide after spikes S2 and S3; see [evals-release-gates.md](specs/evals-release-gates.md) POC-2 handling: provisional values only, set in #69; final gates at POC exit. |
| Q14 | Current POC host plan, capacity, production topology and recurring costs | Open. S1 measured Odoo + PostgreSQL only (about 360 MiB idle, [F-014](facts-register.md)). Full stack in Phase 2; model and retrieval after S2/S3; see [capacity-cost-estimate.md](architecture/capacity-cost-estimate.md) POC-2 handling: full-stack measurement in #68. |
| Q15 | Qualified reviewers for the five named Arabic dialects | Open. Record reviewer approval per dialect in the S2 dataset review worksheet before those cases are runnable POC-2 handling: owner reviews dialects; included in #69 only if approval is recorded, else English + MSA only. |
| Q16 | Active provider RPM/TPM for smoke and full synchronous runs | Owner reconfirmed unchanged on 2026-11-03: Gemini Tier 1, 1,000 RPM / 2,000,000 TPM for both candidates; OpenAI Build, 5,000 RPM / 2,000,000 TPM for both Luna configurations. These active account/project limits remain owner-reported, not independently verified. [F-037](facts-register.md) records official tier-specific Gemini Batch quotas and Luna limits; it does not verify active Gemini project tiers. The official Luna page lists no Free-tier rate row, so Free-tier support remains unknown. |
| Q17 | Full-run input/output token limits and spend ceilings | Owner confirmed hard per-request ceilings of 4,000 input and 8,192 output tokens for all four candidates on 2026-11-02. The full-run caps are $5 for Gemini 3.8 Flash, $2.428160 for Gemini 3.5 Flash-Lite, and $2 combined for GPT-6 Luna. The owner approved raising Flash-Lite from $2.00 to $2.428160 to preserve 8,192 output tokens; this matches the current worst-case reservation. |
| Q18 | Odoo 19 JSON-2 field and write contract for the S2 smoke | Verified for the approved disposable instance: `fields_get`, synthetic cart seeding, exact fixture prices, read-back, creation of the eligible-cart activity, no activity for the recent-cart control, and successful cleanup are recorded in F-028/F-031 and the smoke report. This does not authorize writes to other databases. |
| Q19 | Provider input-count and usage accounting | Gemini's countTokens call uses the full configured request before generation; OpenAI's Responses input-token endpoint is also used before every generation request. The Gemini usage parser derives omitted components only when the total equation uniquely determines them; it fails closed on ambiguous or inconsistent totals. CI covers these cases. The revised smoke returned consistent Gemini usage: 3,366 input, 386 output including 255 reasoning tokens, and 0 tool-use prompt tokens. Billing/quota treatment for count-only requests remains unknown; see F-030, F-034, and F-038. |
| Q20 | Runtime cost monitoring and provider reconciliation | The owner requires automatic cost estimation and monitoring. Research is recorded in F-032/F-033/F-035/F-036 and the S2 runbook: provider response usage supports per-call estimates, while provider-side USD reports are aggregate. The existing Langfuse + OpenTelemetry/Grafana target is suitable for estimates and alerts, but S2 does not implement it. Exact instrumentation, alert thresholds, and reconciliation cadence remain open; this does not change smoke cost reporting or select a model. POC-2 handling: per-run cost in the engine DB and structured logs (#65); Langfuse/OTel moved to POC-3. |
