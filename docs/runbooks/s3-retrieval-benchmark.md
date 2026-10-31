# Runbook: S3 retrieval benchmark

This benchmark compares pgvector and Qdrant using a fixed bilingual corpus of synthetic Odoo demo product records and synthetic policy text. It does not connect to Odoo at runtime, use customer/cart/order data, include price/stock fields, or call a hosted embedding API.

## Run

Prerequisites: Docker, the repository's locked `uv` toolchain, and network access to build the pinned runner image and download the pinned Hugging Face model files on the first run.

Run the fast host-side checks before a full run:

```sh
make preflight-s3
```

Preflight validates the approved dataset and hash, required repository files, `uv` and Docker availability, Docker daemon readiness, Compose configuration, an unused benchmark project namespace, and writable report/cache directories. It does not start services, build the image, download the model, or assess retrieval quality. `make benchmark-s3` runs the same preflight automatically before starting the benchmark.

Run the end-to-end synthetic smoke separately when you want to validate the backend contract without beginning the full benchmark:

```sh
make smoke-s3
```

This starts the disposable backends, builds the runner image, runs the smoke check, and cleans up the project. It does not load the model or write benchmark reports. The first run may still spend time building the pinned image.

Run the complete benchmark from the repository root:

```sh
make benchmark-s3
```

After preflight, the launcher starts only the `s3-retrieval-benchmark` Compose project, builds the runner image, and runs a bounded backend smoke check before the full benchmark. The smoke check loads one synthetic record and checks dense and lexical retrieval in English and Arabic against both backends without loading the embedding model. A passing smoke check does not guarantee model download, full-run success, or retrieval quality.

The Python runner and both backends communicate over the private Compose network; database ports are not published to the host. The PostgreSQL trust-auth setting is limited to this disposable, synthetic benchmark container. Backend storage is temporary, and the launcher removes only the Compose containers, network, and volumes for the project it started; it keeps the built runner image and host model cache for reuse. If the project namespace already has containers or networks, preflight stops rather than reusing or deleting them.

The runner uses the pinned Python image recorded in [F-024](../facts-register.md) and installs the locked `retrieval-benchmark` dependency group. The first full run downloads the model snapshot recorded in [F-007](../facts-register.md); later runs use the host Hugging Face hub cache, mounted without the token cache. The exact model revision is passed to the downloader with token use disabled. No embedding model or backend service is started by `make ci`.

The benchmark runs as the invoking user's UID/GID to avoid root-owned files in the host cache and results directory. Progress reports the current stage, elapsed time, and completed/total queries where applicable; stages without countable units emit a heartbeat at least every 30 seconds. A failed stage is named and exits nonzero. Each run writes a unique temporary JSON report and a Markdown review summary from the same measurements. The launcher regenerates the summary after memory collection and publishes both to `evals/retrieval/results/s3-benchmark.json` and `evals/retrieval/results/s3-benchmark.md` only after retrieval, Markdown generation, memory collection, and Compose cleanup all succeed. Publication rolls back the pair if either replacement fails; a retained backup is reported if rollback itself fails. Commit both reports with the spike decision. A nonzero exit means the measurement did not complete; do not treat a partial report as a successful result.

The checked-in dataset has owner approval and must keep `review_status: "approved"` to run. The runner refuses any pending dataset before contacting the benchmark services or downloading the model. Odoo data is used only in the one-time snapshot recorded in the dataset metadata. The `corpus_sha256` value is computed from the canonical JSON representation of the `documents` list.

## Fixed procedure

- Dataset: `s3-3`, with 29 product documents (28 `consu`, 1 `combo`), 5 synthetic policy documents, and 40 paired scenarios rendered in English and Arabic (40 query strings per language).
- Query mix per language: 30 product-grounding and 10 policy-grounding queries; 12 lexical, 18 paraphrase, and 10 contrastive hard-negative queries. English/Arabic versions of one scenario are paired and are not counted as independent scenarios.
- Query construction: lexical queries use direct catalog or policy terms without cart-state or agent-workflow filler; paraphrase and contrastive queries avoid copying a full target title. Query details must be supported by the frozen corpus.
- Relevance: each scenario declares a default grade of 0 for all documents plus explicit 0–3 overrides. Grades 2 and 3 count as relevant for Recall@k; nDCG uses graded judgments. Product substitutes/complements are relevant only when the query explicitly asks for them.
- Hard negatives: each contrastive query states positive target constraints without naming a distractor in a negated alternative. Each listed near match is grade 0 for exact product/policy grounding. If synthetic decoys are needed, they must be explicitly tagged and reported separately from natural catalog cases.
- Dense embeddings: `intfloat/multilingual-e5-large`; prefix queries with `query:` followed by a space and documents with `passage:` followed by a space.
- Lexical retrieval: PostgreSQL `english`/`arabic` text-search configurations use disjunctive `plainto_tsquery` terms with `ts_rank_cd`; Qdrant 1.20.0 uses BM25-scored full-text payload indexes with `TextQuery`. Both allow partial-term matches, but use different tokenization and scoring algorithms.
- Weighted client-side hybrid retrieval: run dense:lexical RRF at 50:50, 70:30, and 80:20 with zero-based ranks, `k = 60`, candidate depth 20, and document ID as the tie-break. The 50:50 run is the baseline.
- Strict lexical-gate ablation: PostgreSQL uses conjunctive `plainto_tsquery`; Qdrant uses BM25 `TextQuery` with a full-query `MatchText` filter. These are operationally analogous but use different tokenization and language rules.
- Single-roundtrip ablations: PostgreSQL uses one SQL statement with dense/lexical candidate CTEs and the shared RRF formula. Qdrant uses one request with two `Prefetch` queries and both native `FusionQuery(RRF)` and `RrfQuery(k=61, weights=[1, 1])`. The native server default is not formula-equivalent to client-side RRF. Each single-roundtrip result is compared per query with the client-side 50:50 ranking; report exact-order matches, candidate overlap, and latency rather than asserting parity.
- Qdrant capabilities: report the pinned-client/server probes for any-term `TextQuery`, `score_threshold`, strict `MatchText`, phrase matching, and server fusion. Do not apply a score threshold across stores because lexical score scales differ.
- Human review: open `evals/retrieval/results/s3-benchmark.md` first for the concise results, interpretation, provenance, resource measurements, and limitations; use the linked JSON report for full metrics and details. The Markdown summary is generated from the same completed report, not edited as a separate source of measurements.
- Warm-up: 2 passes per query and mode. Measurement: 5 repetitions per query and mode.
- Quality: report macro-average Recall@5, MRR@5, and nDCG@5 overall and by language, product/policy task, and query challenge. Report hard-negative outrank rate for contrastive cases. Treat paired English/Arabic scenarios as clusters; do not claim the 80 strings are 80 independent samples.
- Latency: client-observed search time excludes query embedding; embedding and end-to-end timings are reported separately. Percentiles use nearest-rank.
- Resources: a host-side monitor samples Docker memory for the two named backend containers without mounting the Docker socket into the runner. Report their maximum memory and the runner process's maximum RSS. Record host details and exact image/model versions.
- Execution mode: the benchmark remains sequential. It measures isolated per-query ranking and latency, not throughput; running backends or queries concurrently would add shared CPU/database contention and change the latency being compared. No parallel-versus-sequential performance pilot is claimed, and this decision does not establish throughput capacity or an optimal production execution mode.

## Existing command inventory

- `make setup` uses `uv sync --locked`, which checks the lockfile and emits package progress; its existing tool-version and Docker checks are the fast preflight. No extra progress framework is needed.
- `make ci` reports each pre-commit check and traceability result, so it already gives per-check progress and a final status.
- `make benchmark-s3` is the first full application of the project-wide preflight, smoke, progress, failure, and report rules in this runbook.
- The S1 Odoo Compose startup and `scripts/spike_s1_odoo.py` probe/seed do not yet show stage progress. Track that out-of-scope gap in [issue #53](https://github.com/aymanaboghonim/odoo-agentic-retention-engine/issues/53).

Do not treat the provisional 90% Recall@5 figure as a pass/fail condition. If the small corpus cannot support a reliable gate or store choice, record the limitation and keep the decision open.
