# Runbook: local policy retrieval

The source is [synthetic.json](../../policies/synthetic.json). Each policy has
a stable ID, version, English and MSA text, and a SHA-256 hash of each UTF-8
text. Update the hash when changing text. Removing an entry removes it from the
active index on the next successful snapshot ingestion.

The index is derived and separate from Odoo. It is not a source of authority
for consent, message scheduling, or financial decisions.

## Preparation

- Start the engine database and apply its migrations using the
  [engine DB runbook](engine-db-local.md). Point `ENGINE_DATABASE_URL` only at
  the isolated engine database; ingestion replaces its entire policy index.
- The runtime uses the existing locked `retrieval-benchmark` dependency group
  and the pinned model revision in [F-007](../facts-register.md#f-007). The
  local cache must already contain the model files; ingestion will not download
  them. The S3 setup describes the model and its cache:
  [S3 runbook](s3-retrieval-benchmark.md).
- Run preflight first. If it reports missing packages, install the locked group
  with `uv sync --locked --group retrieval-benchmark`. A regular `make ci`
  sync may remove this optional group; install it again if needed.

## Preflight and ingestion

```sh
export ENGINE_DATABASE_URL='postgresql://engine:<password>@127.0.0.1:5433/engine'
PYTHONPATH=src uv run --no-sync python -m scripts.ingest_policies --preflight-only
PYTHONPATH=src uv run --no-sync python -m scripts.ingest_policies
```

Preflight checks the source, database table, optional dependencies and cached
snapshot; it does not load the model, test inference or prove retrieval quality.
Ingestion reports stages and a heartbeat, and validates and embeds the complete
snapshot before an atomic database replacement. On failure it leaves the
previous index intact.

## Testing

Hermetic tests use injected vectors and the isolated engine test database:

```sh
export TEST_DATABASE_URL='postgresql://engine:<password>@127.0.0.1:5433/engine'
export REQUIRE_DB_TESTS=1
uv run --no-sync pytest -q tests/test_policy_retrieval.py
```

Those tests check the SQL and result contracts, not actual embedding quality.
The model is loaded only for a separately run local smoke; no hosted embedding
service is called. No numeric quality gate is set in this issue.
