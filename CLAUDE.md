@AGENTS.md

# Claude Code notes

The rules above are in `AGENTS.md` (imported). These notes only say how Claude Code applies them here.

## Workflow

- Plan mode never edits repo files. A plan is not durable until it is written into the repo (STATUS, spec, issue); plan files live outside the repo.
- Invoke skills with the `Skill` tool: `pr-workflow`, `adr`, `spec-writing`, `verify-facts`, `rag-eval`, `local-test-db`. If a newly added skill is reported unknown, run `/reload-skills`.
- Push, open a PR, or merge only when the owner says so. The owner merges.
- Path-scoped rules in `.claude/rules/` load when you touch `tests/`, `src/engine/`, or `docs/`.

## Running checks

- Run Python tools as `uv run --no-sync ...` (Python 3.14 only). Run `make ci` before opening a PR.
- Tests need PostgreSQL. Use the `local-test-db` skill; do not point tests at a database you keep.

## Careful

- Do not rebuild, restart, or change the running `poc68-isolated` stack, or write to its databases, unless the owner approves ([full-stack-local.md](docs/runbooks/full-stack-local.md)).
- Do not create `CLAUDE.local.md`: while it exists Claude Code stops reading `AGENTS.md` by default (`F-060`). Personal preferences go in `~/.claude/CLAUDE.md`.
- Facts about Claude Code itself (features, limits, rule syntax) are volatile too: use `F-060` and `F-061`, or verify first.
