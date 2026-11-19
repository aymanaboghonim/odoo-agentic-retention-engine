# Process rules

Rule: if it is not written in a file in this repo, it does not exist. Chat and agent memory are not sources of truth.

## Principles

1. **Spec-driven.** Each feature has a short spec with invariants (things that must always be true) before code.
2. **Mechanical gates over promises.** If a rule matters, a CI check enforces it.
3. **CI-local parity.** `make ci` runs locally exactly what GitHub Actions runs.
4. **Risk-based testing.** Test money, data leaks, prompt injection, idempotency, and integration contracts. No dogmatic TDD. Behavioral tests are hermetic: they do not call live service APIs or paid LLMs. CI may download pinned development tools and container images.
5. **Just-in-time planning.** Only the current milestone has detailed issues. Later phases stay as short checklists in STATUS.md.
6. **Claude Code modes as roles, one skill set.** Claude Code is the working harness ([ADR-0021](adr/0021-claude-code-agent-harness.md)).
   - Plan mode: planning only. It is read-only. It can write only its plan file, which lives outside the repo, never files in the repo (no code, no docs).
   - Normal mode (after the plan is approved): the only mode that edits repo files, creates issues, and opens PRs. The owner decides when to push and merge.

   A plan is not durable until it is written into the repo (STATUS, specs, issues).
   Rules live in one `AGENTS.md`, which `CLAUDE.md` imports. Domain knowledge and procedures (PR workflow, ADRs, specs, facts, RAG eval, local test database) live in focused skills in `.claude/skills/`, invoked with the `Skill` tool. File-specific lessons live in path-scoped rules in `.claude/rules/`. `scripts/check_agent_config.py` keeps these consistent in CI.
   Custom agents are deferred (see below). Do not create one agent per domain.
7. **Verify volatile facts.** Prices, model names, limits and versions come from official docs, with URL and date, in [facts-register.md](facts-register.md).

### Deferred: custom agents

Not needed now. Add one only when it needs different tool permissions or a different job. Candidates:

- Read-only reviewer: checks a PR against the specs and ADRs and cannot edit. The built-in `code-review` skill covers this for now.
- Release agent: allowed to run `gh` release commands that other modes are not.

Revisit when the MVP adds a dedicated release workflow (decision at POC exit, kept by ADR-0021).

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
- Progress is tracked on a minimal GitHub Project v2 board with three Status values: Todo, In Progress, Done. Only issues are on the board; PRs are not (each PR has `Closes #N`). Phases are Milestones (POC-0 to POC-3). Review state is read from the PR, not the board.
- Board automation uses GitHub built-in project workflows only, no custom Actions and no extra token:
  - new issues are auto-added with Status Todo (filter `is:issue`; existing items are not back-filled, add them by hand)
  - closed issue sets Done (a merged PR with `Closes #N` closes its issue)
  - reopened issue sets In Progress
  - "PR linked to issue" is on, but it may only act on PRs that are on the board. If an issue is not moved to In Progress when its PR is opened, move it by hand.
- No orphan commits. No delivery without an issue.

Bootstrap exception: the tooling for these rules is built in Phase 0b. Phase 0a already follows the rules by hand.

## Long-running work and review outputs

This standard applies across all project phases and domains. It covers benchmarks, evaluations, data preparation, model runs, migrations, and commands that use external services or change state. S3 is the first concrete application, not the scope limit.

- **Preflight first.** Before expensive setup or side effects, validate required inputs, configuration, tools, permissions, and output paths. Check service readiness and a representative contract when appropriate. Fail with the missing prerequisite named. A preflight must be fast and must say what it does not verify.
- **Use a bounded smoke check when useful.** Exercise a small representative contract before the full run. State whether it loads models, checks production-scale behavior, or proves result quality; do not imply guarantees it does not provide. Normal CI must remain hermetic and must not start benchmark services or download models.
- **Show progress.** Report the current stage, elapsed time, and completed/total units when countable. For a stage without countable progress, emit a heartbeat at least every 30 seconds. Progress messages must not expose credentials or sensitive records.
- **Fail explicitly and preserve outputs.** Identify the failed stage, return a nonzero exit, and clean up only resources created by that run. Write reports to temporary paths and publish only after all required work and report generation succeeds. A failed run must not replace the last successful result with partial output.
- **Make review results readable.** A benchmark, evaluation, or other result intended for human review must provide a concise Markdown summary alongside machine-readable data, generated from the same successful run. Include key results, interpretation, configuration/provenance, limitations, and links between the summary and raw data.
- **Choose execution mode with evidence.** Prefer sequential execution for isolated per-request measurements. When parallel execution is plausible, compare controlled runs using the same inputs and measurement settings; record elapsed time, resource impact, and ranking/measurement reliability. If parallelism would invalidate the objective or is not relevant, document why and the limits of that conclusion. A smoke check is not a performance comparison.
- **Inventory existing commands.** When adding or applying this standard, identify existing long-running commands. Fix relevant gaps in scope and create follow-up issues for unrelated gaps rather than expanding the current change.

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
- Done 2026-11-19 (#101): full-history scan clean (gitleaks over all refs: no leaks; none of the real secret values, the AWS account id, or any `.env`, tfvars, state, or plan file in any of the 228 commits; issue, PR, and comment text clean). Nothing to remove, so the cleanup was waived by the owner (`Q7`); no force push. Commit metadata keeps the owner's email.
- Repository settings before going public (2026-11-19): the `protect-main` ruleset requires a PR, squash merge, the `Local-parity checks` status, and linear history, and blocks deletion and force push; workflow tokens are read-only by default; actions must be pinned to a full commit SHA (`sha_pinning_required`); the `aws-mvp` environment deploys only from protected branches. Security reports go through GitHub private vulnerability reporting ([SECURITY.md](../SECURITY.md)).
- After the switch to public (owner): enable secret scanning, push protection, and private vulnerability reporting; add the owner as a required reviewer on the `aws-mvp` environment (refused while private).
