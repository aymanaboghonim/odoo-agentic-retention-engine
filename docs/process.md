# Process rules

Rule: if it is not written in a file in this repo, it does not exist. Chat and agent memory are not sources of truth.

## Principles

1. **Spec-driven.** Each feature has a short spec with invariants (things that must always be true) before code.
2. **Mechanical gates over promises.** If a rule matters, a CI check enforces it.
3. **CI-local parity.** `make ci` runs locally exactly what GitHub Actions runs.
4. **Risk-based testing.** Test money, data leaks, prompt injection, idempotency, and integration contracts. No dogmatic TDD. Tests are hermetic (no real network or paid API in CI).
5. **Just-in-time planning.** Only the current milestone has detailed issues. Later phases stay as short checklists in STATUS.md.
6. **One agent, many skills.** One `AGENTS.md` and focused skill files. No multi-agent setup.
7. **Verify volatile facts.** Prices, model names, limits and versions come from official docs, with URL and date, in [facts-register.md](facts-register.md).

## Traceability (code to commit to issue to PR to release)

- Every change starts from a GitHub issue.
- Branch name: `<issue-number>-short-slug`.
- Every commit message contains `Refs #<issue>`.
- Every PR contains `Closes #<issue>`.
- Releases are built from merged PRs. Artifacts are attached to the release.
- Progress is tracked on a minimal GitHub Project v2 board: Backlog, Ready, Doing, Review, Done. Milestones: POC-0 to POC-3.
- No orphan commits. No delivery without an issue.

Bootstrap exception: the tooling for these rules is built in Phase 0b. Phase 0a already follows the rules by hand.

## Decisions

- Architecture decisions are ADRs in `docs/adr/`. An ADR is never edited after acceptance. A new ADR supersedes it.
- Status values: proposed, accepted, superseded.

## Language

- English is the main language for all documents. Keep technical terms in English, without literal translation.
- Arabic is used only for customer-facing UI and messages, test data, and short optional summaries.
- Use simple English.

## Public repo safety

- The repo is private now and will be public later. Secret scanning (gitleaks) runs in CI from Phase 0b.
- All data is synthetic. Cloud free tiers (Langfuse Cloud, Grafana Cloud, LLM APIs) only receive synthetic data.
- Before going public: one history cleanup, and a force push only with explicit owner approval.
