# Status

Single place for "where we left off". Update it in every PR.

Next action: owner review and merge of the PR for #85 (`Q12` privacy: per-run PII tokens, consent + Odoo blacklist + outbound allowlist, retention TTL sweep, right-to-erasure with HMAC tombstone, generalized `erp` scan mode and ERP operator CLI, `website_sale_loyalty` preflight, minimal Odoo fixtures; see [privacy-consent-retention.md](specs/privacy-consent-retention.md)). Verified live on the isolated `poc68-isolated` stack on 2026-11-15: six fixtures via JSON-2, engine DB migrated to `0011`, one `erp` scan (2 runs, every edge case skipped as designed, re-scan created 0), and one fixed-template ERP dispatch to Mailpit with the name rehydrated and no raw PII in the Engine DB. Not verified live: a model ERP proposal, Telegram ERP dispatch, a positive-discount ERP coupon. `#86` and `#87` can start in parallel; all three block `#88`. #84 (MVP Phase 0) was merged as PR #89 (`2ae690b`).

Phase 3 POC exit deliverables (#80 via PR #82, #81 via PR #83) are merged on `main` (`5b509d7`). The POC exit signoff memo, consolidated evidence, explicit no-go production boundaries, and prioritized MVP handoff plan live in [poc-exit-review.md](architecture/poc-exit-review.md). The offline stakeholder walkthrough is in [poc-walkthrough.html](demo/poc-walkthrough.html) and [local-demo-walkthrough.md](runbooks/local-demo-walkthrough.md) (synthetic demo artifact only; not production hosted). In addition, live end-to-end verification on `poc68-isolated` completed via `scripts/poc3_live_e2e.py` (4 settled `gpt-6-luna` calls under `poc3-live-e2e-demo`, USD 0.00066725 actual / USD 0.018384 reserved under USD 0.50 ceiling): English Mailpit auto-approved reminder (`e0a1af8c-2ca1-4464-93d8-14d5286ad494`, Mailpit message `7H66RpacLFoDYPX6vWKEuV`) and MSA Arabic Telegram 12% offer (`38df884b-76d8-4df3-8996-2ba2fc59f5af`, approved via `approval-api`, Odoo `loyalty.card` `RET-FA64A2D5F1330955AD69F83D0D122E99`, Telegram message `12`); one pre-loyalty run (`cfc95e56-c353-4062-bee7-97858e702cfb`) remains safely held (`coupon` `unknown`) before `website_sale_loyalty` was installed.

All 18 hermetic end-to-end integration tests pass in CI against PostgreSQL. The live 17-case bilingual evaluation completed (17/17 valid contracts, 12/13 action checks, 2/2 price checks, 0 unsafe discounts) under a single durable aggregate budget (USD 0.082728 reserved, USD 0.002275 estimated generation spend). The owner reviewed and approved provisional release gates in [poc2-end-to-end-evaluation.md](specs/poc2-end-to-end-evaluation.md) and [evals-release-gates.md](specs/evals-release-gates.md). All four controlled synthetic delivery reviews (English/MSA on Mailpit and dedicated Telegram test chat) were authorized and successfully dispatched without creating coupons for 0% discount offers.

The final isolated PostgreSQL run passed 17 scanner and capacity-claim tests,
including repeat-scan idempotency and the owner-approved recovery guard; its
disposable test database was removed. The Compose boundary tests also passed.
`make ci` passed against the final staged implementation and report.

PR #77 for #67 was merged. The workflow is documented in [gated-execution-review.md](runbooks/gated-execution-review.md). Fixed and model-generated English and MSA messages were received and owner-reviewed on Mailpit and Telegram; the MSA Mailpit subject follows the approved language. Four sequential model calls completed at an estimated aggregate published-list cost of USD 0.018384. The actual OpenAI project rate tier remains unknown (F-046); these calls were not a rate-limit or retrieval-quality evaluation. Each issue gets its own detailed plan just in time before work starts. POC-2 scope is planned in #61. Phase 1 is complete: S1 to S4 and their follow-ups (#49, #51, #53) are merged and POC-1 has no open issues. S2 outcome in [ADR-0016](adr/0016-s2-llm-evaluation-and-model-selection.md) (accepted for the POC): GPT-6 Luna `low` with the `tool-clarity` prompt (128/132 vs 115/132 baseline; one prompt-injection regression accepted as possible noise, one run per prompt). Limits: 4 remaining failures, no retained model text, bounded discount matcher. The proposal runtime has a new structured-discount prompt and requires its own live evaluation in #69.

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
- [x] **Phase 2: POC walking skeleton** (milestone POC-2; scope planned 2026-11-04 in #61; one vertical slice: abandoned cart, agent proposal, safety gate, approval, Odoo coupon/activity, Telegram/Mailpit; English and MSA Arabic)
  - [x] #62 Engine foundation ([spec](specs/engine-foundation.md), [runbook](runbooks/engine-db-local.md)): engine DB, fail-closed config, durable run, audit, state machine (first; others depend on it)
  - [x] #56 Production financial gate and agent-output rejection (after #62; [ADR-0018](adr/0018-financial-safety-gate-decisions.md), [spec](specs/safety-gate.md); live Luna `low` re-run 127/132 on 2026-11-05, $0.026; see spec)
  - [x] #63 Odoo adapter: read-only scan and context, JSON-2 activity and coupon writes (after #62; [spec](specs/odoo-adapter.md), [runbook](runbooks/odoo-adapter-local.md), F-042; live test passed on the disposable instance)
  - [x] #64 Policy retrieval with synthetic bilingual policies (after #62; [spec](specs/policy-retrieval.md), [runbook](runbooks/policy-retrieval-local.md); retrieval quality gates remain provisional)
  - [x] #65 Agent proposal step: provider adapter, validation, gate handoff and per-run budget ([spec](specs/agent-proposal.md), [runbook](runbooks/agent-proposal-local.md); live evaluation remains #69)
  - [x] #66 Approval API and CLI ([spec](specs/approval-api.md), [runbook](runbooks/approval-local.md); #67 owns the actual execution-time recheck)
  - [x] #67 Gated execution: coupon and Telegram/Mailpit dispatch, idempotency (after #56, #63, #66); fixed and model-generated English/MSA messages owner-reviewed on both channels; localized Mailpit subject bound to approved language; PR #77 merged
  - [x] #68 Full-stack compose, worker trigger, capacity measurement (after #63, #64, #65); PR #78 merged
  - [x] #69 End-to-end test, live EN/MSA eval, provisional gates (last); [spec](specs/poc2-end-to-end-evaluation.md), live evaluation report in [evals/poc2/results/poc2-69-evaluation/](../evals/poc2/results/poc2-69-evaluation/), four owner-reviewed deliveries complete; PR #79 merged
  - POC-2 decisions (owner, 2026-11-04): approval by API + CLI with static env operator tokens; coupon written through Odoo JSON-2 in the local instance (Odoo 19 coupon model verified first, fallback only by ADR); per-run cost in the engine DB and structured logs, Langfuse/OTel moved to POC-3; dialects only if owner review is recorded in the S2 worksheet, else English + MSA; deferred: Q12, Q13 final gates (provisional only), production topology, churn/RFM.
  - #66 approval decisions (owner, 2026-11-07): one named static-token operator and no time expiry for the POC; approval is bound to the displayed snapshot and gate fingerprint. The API does not execute actions. #67 owns the actual immediate-before-execution recheck; see the [spec](specs/approval-api.md) for reasons and limits.
- [x] **Phase 3: POC exit review, MVP plan** (milestone POC-3)
  - [x] #80 POC exit review, signoff memo, and MVP handoff ([poc-exit-review.md](architecture/poc-exit-review.md); PR #82 merged)
  - [x] #81 Local static demo artifact and live E2E walkthrough CLI ([poc-walkthrough.html](demo/poc-walkthrough.html), [local-demo-walkthrough.md](runbooks/local-demo-walkthrough.md), `scripts/poc3_local_demo.py`, `scripts/poc3_live_e2e.py`, `tests/test_poc3_local_demo.py`, `tests/test_poc3_live_e2e.py`; Option B local-only synthetic walkthrough plus live E2E Mailpit/Telegram/Odoo coupon verification on `poc68-isolated`; PR #83 merged)
- [ ] **Phase 4: Cloud MVP** (milestone `MVP`; production-grade portfolio deployment on 1st-party AWS targeting `<= USD 13/month` under scheduled uptime and cleanup, treating Odoo 19.0 + demo data as a black-box ERP baseline per `ADR-0001` with minimal deterministic fixtures, module-readiness checks, and outbound recipient allowlist safety)
  - [x] #84 MVP Phase 0: milestone bootstrap, verified cloud/channel facts (`F-048`–`F-055`), [ADR-0019](adr/0019-aws-ecs-on-ec2-cloud-runtime.md) (Terraform + Amazon ECS on single 4 GiB EC2, ECR production registry / OIDC, SSM, Cognito, scheduled uptime, shutdown/cleanup requirements, and billing/residual risks), [ADR-0020](adr/0020-aws-cloudfront-s3-edge-and-backups.md) (CloudFront OAC + private S3 edge & backups, superseding [ADR-0007](adr/0007-free-tier-observability.md)), and `STATUS.md` refresh
  - **Tier 1 — Security, Privacy, and Governance Prerequisites (detailed issues created; blockers before `#88` cloud Go-Live):**
    - [ ] #85 (`Q12`) Spec + implementation: privacy, deterministic per-run PII tokenization before LLM/telemetry calls, generalized Odoo 19 ERP cart scan with minimal deterministic edge-case fixtures + `website_sale_loyalty` readiness preflight + verified opt-in + outbound allowlist, retention TTL sweeper, and right-to-erasure purge — in review: implemented with hermetic invariant tests TM-1..TM-8 and PR-1..PR-7 ([spec](specs/privacy-consent-retention.md), [runbook](runbooks/privacy-local.md), `F-056`); ERP operator CLI wired; fixtures, ERP scan, and a Mailpit ERP dispatch verified live on `poc68-isolated`
    - [ ] #86 (`Q11`) Spec + implementation: OIDC/JWKS multi-operator RBAC (`viewer` / `approver` / `admin`) backed by Amazon Cognito in AWS (local JWKS in CI), mandatory `expires_at` TTL on approvals, and CloudFront origin-verify middleware
    - [ ] #87 (`Q13`) Spec + implementation: production-scale bilingual release gates (`evals-release-gates.md`), deterministic zero-LLM fixed-rule fallback behind `Gate`, and `admin`-gated / circuit-breaker kill-switch (`engine_runtime_state`)
    - [ ] #88 (`Q11`/`Q14`) Infrastructure + live cloud smoke (blocked by `#85`, `#86`, `#87`): `deploy/terraform/` 1st-party AWS ECS-on-EC2 (`t3a.medium`), ECR production registry + GitHub OIDC, SSM Session Manager (`port 22` closed), SSM Parameter Store, encrypted `gp3` EBS, CloudFront OAC + private S3, Cognito User Pool, AWS Budgets alerts, scheduled stop/start, idempotent shutdown/cleanup/inventory scripts (`aws_mvp_shutdown.sh`, `aws_mvp_cleanup.sh`, `aws_mvp_status.py`), and `docs/architecture/` updates
  - **Tier 2 — Operational Observability & Reconciliation (checklist only; issues created after Tier 1 closes):**
    - [ ] (`Q20`) Wire OpenTelemetry SDK → Grafana Cloud free tier (`F-011`) and Langfuse Cloud Hobby (`F-010`) with PII tokenization (`TM-1`, `TM-2`) + scheduled provider billing reconciler (`F-036`)
    - [ ] Cross-system execution journal reconciliation service/CLI/API for `ExecutionHeld` (`unknown`) Odoo coupon / channel effects
  - **Tier 3 — Channels, Operator UI & Retention Expansion (checklist only; issues created after Tier 2 closes):**
    - [ ] (`Q6`, `ADR-0013`) Production transactional email adapter (1st-party Amazon SES per `F-054` or Resend per `F-044`) + Meta WhatsApp Cloud API verification (`F-009`, `F-055`) and `ChannelPort` adapter while retaining Telegram live primary
    - [ ] Static English + Arabic RTL (`ADR-0008`) Operator Approval & Ops UI hosted on private S3 + CloudFront OAC (`ADR-0020`), keeping side effects worker-owned (`ADR-0012`)
    - [ ] RFM / CLV churn next-best-action vertical slice over read-only Odoo order history (`OdooReader`)
  - MVP decisions (owner, 2026-11-14/15): 1st-party AWS only (zero Marketplace, zero Route 53 domain registration/transfer, zero upfront reservations) with `<= USD 13/month` as an operating target (not a guaranteed hard cap, `F-053`); Terraform + Amazon ECS on single 4 GiB EC2 (`t3a.medium`, scheduled uptime) + mandatory idempotent teardown/cleanup scripts (`ADR-0019`); Amazon ECR as production runtime registry (GHCR optional for CI only); CloudFront OAC + private S3 replacing Cloudflare Pages/R2 while retaining Grafana Cloud + Langfuse Cloud observability (`ADR-0020`); standard OIDC/JWKS in FastAPI backed by Amazon Cognito in AWS; Odoo 19.0 + demo data baseline (`ADR-0014`) expanded with minimal deterministic fixtures + `website_sale_loyalty` preflight (`F-042`) + `Q12` privacy engineering and outbound recipient allowlists.

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
- [x] pgvector vs Qdrant benchmark: ADR-0006 (superseded by ADR-0015)
- [x] Free-tier observability, OPEX focus, CloudFront OAC + S3 edge/backups: [ADR-0020](adr/0020-aws-cloudfront-s3-edge-and-backups.md) (supersedes [ADR-0007](adr/0007-free-tier-observability.md))
- [x] 1st-party AWS ECS-on-EC2 MVP runtime (`<= USD 13/month` target): [ADR-0019](adr/0019-aws-ecs-on-ec2-cloud-runtime.md)
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
| Q6 | Free email service for MVP | MVP Tier 3: evaluate 1st-party Amazon SES ([F-054](facts-register.md)) vs Resend ([F-044](facts-register.md)) after Tier 1–2 controls complete. |
| Q7 | History cleanup and force push before going public | Owner approval at that time |
| Q8 | ~~Discount thresholds~~ Resolved 2026-10-24: tier approach accepted (see checklist item 1.2). Defaults still to be validated in S1. | Done; validate defaults in S1 |
| Q9 | Should we adopt any skill, agent, instruction, hook or workflow from `github/awesome-copilot`? | Done. Resolved 2026-10-24: no bulk adoption. Consider individual items only for a concrete need, after reviewing content, scripts, permissions and license. |
| Q10 | Planning hierarchy for POC/MVP: phases, parent issues, child tasks, user stories | Done. Resolved 2026-10-25: milestone per phase; parent issue only when an outcome spans multiple issues; keep spec, spike, bug and task types; use user-story format only for user-facing behavior. |
| Q11 | Ports, protocols, authentication and network layout for each connection | Partially resolved 2026-11-14 for MVP architecture in [ADR-0019](adr/0019-aws-ecs-on-ec2-cloud-runtime.md) and [ADR-0020](adr/0020-aws-cloudfront-s3-edge-and-backups.md) (CloudFront `*.cloudfront.net` HTTPS + OAC, ECS on EC2 with SSH closed/SSM, Cognito OIDC/JWKS). Implementation tracked in `#86` (OIDC RBAC + expiring approvals) and `#88` (Terraform + network verification). |
| Q12 | Applicable data protection laws, residency region, retention period, deletion process, chat authentication | Engineering controls implemented in `#85` (in review): PII tokenization, explicit opt-in + Odoo blacklist + exact-match outbound allowlist, configurable `PRIVACY_RETENTION_DAYS` sweep, and right-to-erasure with `HMAC-SHA256` tombstone ([spec](specs/privacy-consent-retention.md)). Still open: applicable laws, residency region, the actual retention period value, Odoo-side partner erasure process, and chat authentication; no real customer PII until these are decided. |
| Q13 | Numeric release gates and rollback triggers, fallback definition, who re-enables the agent | Provisional POC-2 gates approved 2026-11-13 in #69 ([poc2-end-to-end-evaluation.md](specs/poc2-end-to-end-evaluation.md), [evals-release-gates.md](specs/evals-release-gates.md)). Final production sample sizes, fixed-rule fallback, and `admin` kill-switch are tracked in MVP Tier 1 `#87`. |
| Q14 | Current POC host plan, capacity, production topology and recurring costs | Local synthetic full-stack capacity measured 2026-11-09 in #68 ([evals/full-stack/capacity-cost-estimate.md](../evals/full-stack/capacity-cost-estimate.md)): 501.0 MiB idle, 501.1 MiB scan, 2,049.2 MiB peak during proposal. MVP cloud target decided in [ADR-0019](adr/0019-aws-ecs-on-ec2-cloud-runtime.md) (`t3a.medium` 4 GiB, scheduled uptime + cleanup targeting `<= USD 13/month`, `F-050`, `F-053`); live cloud measurement and teardown verification tracked in `#88`. |
| Q15 | Qualified reviewers for the five named Arabic dialects | Open. Record reviewer approval per dialect in the S2 dataset review worksheet before those cases are runnable POC-2 handling: owner reviews dialects; included in #69 only if approval is recorded, else English + MSA only. |
| Q16 | Active provider RPM/TPM for smoke and full synchronous runs | Owner reconfirmed unchanged on 2026-11-03: Gemini Tier 1, 1,000 RPM / 2,000,000 TPM for both candidates; OpenAI Build, 5,000 RPM / 2,000,000 TPM for both Luna configurations. These active account/project limits remain owner-reported, not independently verified. [F-037](facts-register.md) records official tier-specific Gemini Batch quotas and Luna limits; it does not verify active Gemini project tiers. The official Luna page lists no Free-tier rate row, so Free-tier support remains unknown. |
| Q17 | Full-run input/output token limits and spend ceilings | Owner confirmed hard per-request ceilings of 4,000 input and 8,192 output tokens for all four candidates on 2026-11-02. The full-run caps are $5 for Gemini 3.8 Flash, $2.428160 for Gemini 3.5 Flash-Lite, and $2 combined for GPT-6 Luna. The owner approved raising Flash-Lite from $2.00 to $2.428160 to preserve 8,192 output tokens; this matches the current worst-case reservation. |
| Q18 | Odoo 19 JSON-2 field and write contract for the S2 smoke | Verified for the approved disposable instance: `fields_get`, synthetic cart seeding, exact fixture prices, read-back, creation of the eligible-cart activity, no activity for the recent-cart control, and successful cleanup are recorded in F-028/F-031 and the smoke report. This does not authorize writes to other databases. |
| Q19 | Provider input-count and usage accounting | Gemini's countTokens call uses the full configured request before generation; OpenAI's Responses input-token endpoint is also used before every generation request. The Gemini usage parser derives omitted components only when the total equation uniquely determines them; it fails closed on ambiguous or inconsistent totals. CI covers these cases. The revised smoke returned consistent Gemini usage: 3,366 input, 386 output including 255 reasoning tokens, and 0 tool-use prompt tokens. Billing/quota treatment for count-only requests remains unknown; see F-030, F-034, and F-038. |
| Q20 | Runtime cost monitoring and provider reconciliation | The owner requires automatic cost estimation and monitoring. Research is recorded in F-032/F-033/F-035/F-036 and the S2 runbook: provider response usage supports per-call estimates, while provider-side USD reports are aggregate. POC-2 records per-run cost in the engine DB and structured logs (`#65`). Wiring OpenTelemetry → Grafana Cloud + Langfuse Cloud Hobby (`ADR-0020`) and automated provider billing reconciliation (`F-036`) is scheduled in MVP Tier 2 after Tier 1 (`#85`–`#88`) closes. |
