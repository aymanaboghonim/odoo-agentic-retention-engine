# Process rules

Rule: if it is not written in a file in this repo, it does not exist. Chat and agent memory are not sources of truth.

## Principles

1. **Spec-driven.** Each feature has a short spec with invariants (things that must always be true) before code.
2. **Mechanical gates over promises.** If a rule matters, a CI check enforces it.
3. **CI-local parity.** `make ci` runs locally exactly what GitHub Actions runs.
4. **Risk-based testing.** Test money, data leaks, prompt injection, idempotency, and integration contracts. No dogmatic TDD. Behavioral tests are hermetic: they do not call live service APIs or paid LLMs. CI may download pinned development tools and container images.
5. **Just-in-time planning.** Only the current milestone has detailed issues. Later phases stay as short checklists in STATUS.md.
6. **Built-in modes as roles, one skill set.** Use the VS Code Copilot modes as they are:
   - Plan: planning only. It is read-only. It can write only to the agent memory file, never to files in the repo (no code, no docs).
   - Agent: execution. It is the only mode that edits repo files, creates issues and opens PRs.
   - Ask: questions and chat.

   A plan from Plan mode is not durable until Agent mode writes it into the repo (STATUS, specs, issues).
   Rules live in one `AGENTS.md`. Domain knowledge (infra, testing, RAG eval, release) lives in focused skill files that load on demand.
   Custom agents are deferred (see below). Do not create one agent per domain.
7. **Verify volatile facts.** Prices, model names, limits and versions come from official docs, with URL and date, in [facts-register.md](facts-register.md).

### Deferred: custom agents

Not needed now. Add one only when it needs different tool permissions or a different job. Candidates:

- Read-only reviewer: checks a PR against the specs and ADRs and cannot edit.
- Release agent: allowed to run `gh` release commands that other modes are not.

Revisit at the POC exit review (Phase 3).

## Branching and merging (GitHub flow)

- One long-lived branch: `main`. It is always releasable.
- Work happens on short-lived branches named `<issue-number>-short-slug`, deleted after merge.
- **Never push directly to `main`.** This applies to humans and agents. Every change goes through a PR.
- Merge method: squash merge only. The squash commit uses the PR title and body, so `Closes #<issue>` stays on `main`. One issue, one PR, one commit on `main`.
- No `develop` or release branches. Releases are tags on `main`.
- Required before merge: `make ci` passes. Required reviews are off (solo developer).

### How the no-push-to-main rule is enforced

- The active `protect-main` GitHub ruleset on `main` is the authoritative control. It requires a pull request and the `Local-parity checks` status, requires linear history, blocks force pushes and deletion, allows squash as the only PR merge method, and has an empty bypass list (F-016).
- Repository settings also allow squash only and automatically delete merged branches (F-016).
- `AGENTS.md` tells humans and agents never to push directly to `main`; the ruleset enforces this on GitHub.
- Do not add custom local wrappers or CI API checks that duplicate the ruleset. If the ruleset is later disabled, reopen issue #9 before claiming direct pushes are prevented.

## Traceability (code to commit to issue to PR to release)

- Every change starts from a GitHub issue.
- Branch name: `<issue-number>-short-slug`.
- Every commit message contains `Refs #<issue>`.
- Every PR contains `Closes #<issue>`.
- Releases are built from merged PRs. Artifacts are attached to the release.
- Progress is tracked on a minimal GitHub Project v2 board with three Status values: Todo, In progress, Done. Phases are Milestones (POC-0 to POC-3). Review state is read from the PR, not the board.
- Board automation uses GitHub built-in project workflows only, no custom Actions and no extra token:
  - new issues and PRs are auto-added with Status Todo
  - closed issue or merged PR sets Done
  - reopened issue sets In progress
  - moving to In progress when a PR is opened is done by hand if no built-in rule exists (verify in F-015)
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
