# ADR-0021: Claude Code is the agent harness

- Status: accepted
- Date: 2026-11-16
- Issue: #91
- Facts: [F-060](../facts-register.md), [F-061](../facts-register.md)

## Context

The agent rules, skills, and workflow were written for VS Code Copilot modes (`docs/process.md`, `AGENTS.md`, `.github/skills/`). Claude Code is now the main worker. It only discovers skills in `.claude/skills/`, so in #86 the `Skill` tool answered `Unknown skill: spec-writing` and `verify-facts`. One skill (`rag-eval`) had no frontmatter at all, there was no `CLAUDE.md`, there was no shared permission policy, and nothing stopped an agent from reading `.env`.

## Decision

1. **Skills live in `.claude/skills/`**, moved with `git mv` so there is one copy. Every skill has line-1 frontmatter with `name` (equal to its directory) and a `description` that leads with when to use it (limits in `F-060`). A new `local-test-db` skill records the throwaway PostgreSQL procedure measured in #85 and #86.
2. **`CLAUDE.md` is a thin file that imports `@AGENTS.md`** (the documented shared-file pattern, `F-060`). `AGENTS.md` stays the single, tool-neutral rule file and keeps every hard rule. Its one Copilot-specific line now says plan mode does not edit repo files and edits happen only after the plan is approved.
3. **Path-scoped rules** in `.claude/rules/` (`tests/**`, `src/engine/**` and `scripts/**`, `docs/**`) hold practical lessons next to the files they apply to. They point at the hard rules instead of copying them.
4. **Shared `.claude/settings.json`**: allow only routine, read-only commands (`make ci`, `uv run --no-sync`, read-only `git` and `gh`); deny `Read` of `.env`, `.env.local`, and `secrets/`. `Read` denies are best effort for built-in tools (`F-061`); they do not replace never committing secrets.
5. **A mechanical gate**, `scripts/check_agent_config.py` (pre-commit, tested), fails when a skill cannot be loaded, the `AGENTS.md` skill list and the directories differ, `CLAUDE.md` does not import `AGENTS.md`, or the settings lose the secret denies. A rule that matters gets a check, not a promise ([process.md](../process.md)).
6. **No enforcement hooks now.** The GitHub `protect-main` ruleset and the traceability check already enforce the hard rules, and `process.md` says not to duplicate them locally.

## Alternatives

- **Keep `.github/skills/` and symlink into `.claude/skills/`:** rejected. It keeps a Copilot-era layout nobody uses and needs symlinks maintained.
- **Merge everything into `CLAUDE.md` and symlink `AGENTS.md`:** rejected. One file named for one tool is harder to review, and other tools read `AGENTS.md`.
- **Enforcement hooks (block `git push` to `main`, branch-name guard):** not added. They duplicate the ruleset and CI; revisit when a rule has no other gate.
- **Auto-format hook, custom subagents, Claude GitHub Action for PR review:** not added. Pre-commit already formats, the built-in `code-review` skill covers a read-only reviewer, and the Action needs an API-key secret and an owner decision on cost and scope.

## Consequences

- Skills are invocable through the `Skill` tool with no second copy to keep in sync. Hard rules are unchanged.
- Claude Code features are volatile (`F-060`, `F-061` were checked on 2026-11-16 and expire after 90 days). Allow rules in the committed settings apply only after a teammate accepts the folder trust dialog (`F-061`).
- A new top-level skills directory needs `/reload-skills` in a running session (`F-060`).
- `docs/architecture/poc-exit-review.md` section 5 (custom Copilot agents) is a historical record and is superseded in practice by this ADR.
