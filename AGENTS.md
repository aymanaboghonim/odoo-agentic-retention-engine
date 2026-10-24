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
- Never commit secrets. All data is synthetic. Never write into the Odoo database.
- The LLM never decides money. Discount limits are enforced in code and tested.
- Plan mode does not edit repo files. Only Agent mode does.
- Do not invent facts or results. Say "unknown" and add it to the open questions.
- Docs are in English, simple words, technical terms kept as they are. Arabic only for customer-facing UI and messages, test data and optional summaries.

## Commands

- `make ci`: runs all checks, same as GitHub Actions. Not available yet (issue #8).

## Skills

Load the skill that matches the task, from `.github/skills/`:

- `pr-workflow`: issue, branch, commit, PR, merge, cleanup.
- `adr`: write or supersede an architecture decision.
- `spec-writing`: write a feature spec with invariants.
- `verify-facts`: check a volatile fact and record it.

More skills (Odoo adapter, RAG eval, infra and deploy, release) are added when the first issue needs them.
