# Spec: Local and GitHub CI parity

- Issue: #8
- ADRs: ADR-0009
- Status: agreed

## Purpose

One command checks the repo locally and in GitHub Actions. The same pinned tools and checks must run in both places.

## Scope and non-goals

Scope: Markdown style, YAML/JSON/TOML syntax, GitHub Actions lint, offline local links, secret scanning, traceability, verified-fact freshness, and Python lint/type/test gates when matching files exist.

Non-goals: test live APIs, use paid services, validate external links over the network, or add application/runtime dependencies before the application exists.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| CI-1 | Local and GitHub CI run the same `make ci` command. | Workflow source and local run |
| CI-2 | Dependency versions are locked and workflow actions are pinned to immutable refs. | `uv sync --locked`; workflow review |
| CI-3 | A feature branch starts with its issue number; every branch commit says `Refs #N`; PR body says `Closes #N`. | `scripts/check_traceability.py` |
| CI-4 | No verified fact is older than 90 days; unknown facts remain visibly unverified. | `scripts/check_facts_freshness.py` |
| CI-5 | Python checks run when Python files exist and skip clearly when they do not. | Ruff, mypy and pytest runner scripts |
| CI-6 | Secret scans cover repository history and redact findings. | `scripts/scan_secrets.sh` |
| CI-7 | Link checks do not require network access. | `scripts/check_links.sh` with lychee offline mode |

## Behavior

- `make setup` installs locked development tools and the pre-commit hook.
- `make ci` syncs the lock, runs pre-commit on all tracked files, then checks branch/commit/PR traceability.
- GitHub Actions invokes `make ci` on pull requests and pushes to `main`.
- A pull request with a missing `Closes #N`, or a branch commit missing `Refs #N`, fails.

## Risks

- Docker is a prerequisite for link, workflow and secret checks; setup and CI fail with a clear message if it is missing.
- Offline link checking validates local links, not remote availability.
- Python 3.15 is not a supported target until selected dependencies have compatible wheels and the project tests it.

## Open questions

- Add online external-link checks later only if they can be made reproducible without flaky CI.
