# ADR-0009: Python 3.14 baseline and locked CI tooling

- Status: accepted
- Date: 2026-10-24

## Context

Issue #8 needs a reproducible local and GitHub CI command before application code exists. The app is likely to use compiled AI/data dependencies, so wheel availability matters on the developer's WSL2 machine and CPU-only VPS.

The current queried PyPI releases of ONNX Runtime and Hugging Face Tokenizers publish CPython 3.14 wheels, but not CPython 3.15 wheels (F-017). Python 3.15 is not part of the POC support promise.

## Decision

- Set the supported Python range to `>=3.14,<3.15`; CI uses Python 3.14 only. This allows 3.14 patch updates but does not claim compatibility with an untested minor version.
- Use uv with `pyproject.toml`, a committed `uv.lock`, and a `dev` dependency group for CI tools. Keep project runtime dependencies empty until the app is designed.
- The CI tool versions are pinned in the lockfile and pre-commit hooks; GitHub Actions use immutable commit SHAs. See F-018 and F-019.
- `make setup` and `make ci` are the local entry points. GitHub Actions calls `make ci` unchanged.
- Revisit Python 3.15 after the POC dependency set is selected and tests show compatible wheels and behavior. A new ADR is required to widen support.

## Alternatives

- Python 3.13: rejected as the baseline because 3.14 is established and the official toolchain is available on it; 3.13 support can be added only if dependencies require it.
- Python 3.15 now: rejected because current critical compiled dependencies do not publish 3.15 wheels.
- Floating package versions or install-on-demand tools: rejected because local and CI results would drift.
- Add application dependencies now: rejected because the application stack is not yet selected.

## Consequences

- One supported/tested Python minor version keeps CI small and avoids unsupported wheel builds.
- Dev tools are reproducible, but lockfile updates must be intentional and reviewed.
- The app's package/runtime dependency set remains open for the POC design.
