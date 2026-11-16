---
paths:
  - "tests/**"
---

# Test rules

Hard rules are in `AGENTS.md`; these are the practical ones for tests.

- Tests are hermetic: no live service APIs, no paid LLM calls. CI uses `REQUIRE_DB_TESTS=1`, so a missing `TEST_DATABASE_URL` is a failure, not a skip (see the `local-test-db` skill).
- A test that creates runs or other rows must delete them (see `_clean` in `tests/test_execution.py`). Leftovers break `tests/test_engine_db.py`.
- Never import one test module from another. mypy then sees the module under two names and fails; put shared helpers in `scripts/` or repeat them.
- Test by risk: money, data leaks, prompt injection, idempotency, integration contracts. Each spec invariant ID maps to a named test.
- Use synthetic data only (`example.test` addresses, fixed synthetic names).
