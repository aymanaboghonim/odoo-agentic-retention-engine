# Agent rules

Project: an agentic AI layer on top of Odoo (abandoned carts, retention). Read [docs/00-vision.md](docs/00-vision.md) for the goal.

## Start every task here

1. Read [docs/STATUS.md](docs/STATUS.md): the next action and open questions.
2. Read [docs/process.md](docs/process.md): the full process rules. This file only lists the hard ones.
3. Find the GitHub issue for the task. No issue, no work: ask the owner or create one first.

## Hard rules (never break)

- Never push to `main`. Work on branch `<issue>-short-slug`. Open a PR with `Closes #<issue>`. Every commit has `Refs #<issue>`. PRs are squash-merged.
- If it is not written in a file in this repo, it does not exist. Do not rely on chat or memory.
- Do not use a price, model name, limit or version from memory. Use a verified row in [docs/facts-register.md](docs/facts-register.md), or verify it first.
- Decisions go in an ADR in [docs/adr/](docs/adr/). Never edit an accepted ADR. Write a new one that supersedes it.
- Update `docs/STATUS.md` in every PR.
- Never commit secrets. All data is synthetic. The agent runtime never writes directly to the Odoo database: it reads with a read-only database role, and runtime state changes go through the supported Odoo API/worker path. A local setup fixture may create synthetic data through Odoo's supported API only in the isolated local development instance. Never seed through SQL or target a real/shared Odoo instance.
- The LLM never decides money. Discount limits are enforced in code and tested.
- Long-running or side-effecting commands follow the project-wide preflight, progress, failure, and report rules in `docs/process.md`; do not launch the full operation before its documented preflight.
- Plan mode does not edit repo files. Only Agent mode does.
- Do not invent facts or results. Say "unknown" and add it to the open questions.
- Docs are in English, simple words, technical terms kept as they are. Arabic only for customer-facing UI and messages, test data and optional summaries.

## Commands

- `make setup`: syncs locked development tools and installs the pre-commit hook.
- `make ci`: runs all checks, same as GitHub Actions.
- Python toolchain: 3.14 only (`>=3.14,<3.15`); dev dependencies are in `pyproject.toml` and `uv.lock`.

## Skills

Load the skill that matches the task, from `.github/skills/`:

- `pr-workflow`: issue, branch, commit, PR, merge, cleanup.
- `adr`: write or supersede an architecture decision.
- `spec-writing`: write a feature spec with invariants.
- `verify-facts`: check a volatile fact and record it.

More skills (Odoo adapter, RAG eval, infra and deploy, release) are added when the first issue needs them.
