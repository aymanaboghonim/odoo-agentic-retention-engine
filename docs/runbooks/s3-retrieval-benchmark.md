# Runbook: S3 retrieval benchmark

This benchmark compares pgvector and Qdrant using a fixed bilingual corpus of synthetic Odoo demo product records and synthetic policy text. It does not connect to Odoo at runtime, use customer/cart/order data, include price/stock fields, or call a hosted embedding API.

## Run

Prerequisites: Docker, the repository's locked `uv` toolchain, and network access to build the pinned runner image and download the pinned Hugging Face model files on the first run.

Run the complete benchmark from the repository root:

```sh
make benchmark-s3
```

The command starts only the `s3-retrieval-benchmark` Compose project. The Python runner and both backends communicate over the private Compose network; database ports are not published to the host. The PostgreSQL trust-auth setting is limited to this disposable, synthetic benchmark container. Backend storage is temporary, and the launcher removes only this Compose project's containers and volumes on exit.

The runner uses the pinned Python image recorded in [F-024](../facts-register.md) and installs the locked `retrieval-benchmark` dependency group. The first run downloads the model snapshot recorded in [F-007](../facts-register.md); later runs use the host Hugging Face hub cache, mounted without the token cache. The exact model revision is passed to the downloader with token use disabled. No embedding model or backend service is started by `make ci`.

The benchmark runs as the invoking user's UID/GID to avoid root-owned files in the host cache and results directory. Each run writes a unique temporary report; the launcher promotes it to `evals/retrieval/results/s3-benchmark.json` only after retrieval and memory collection both succeed. Commit the report with the spike decision. A nonzero exit means the measurement did not complete; do not treat a partial report as a successful result.

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
- Warm-up: 2 passes per query and mode. Measurement: 5 repetitions per query and mode.
- Quality: report macro-average Recall@5, MRR@5, and nDCG@5 overall and by language, product/policy task, and query challenge. Report hard-negative outrank rate for contrastive cases. Treat paired English/Arabic scenarios as clusters; do not claim the 80 strings are 80 independent samples.
- Latency: client-observed search time excludes query embedding; embedding and end-to-end timings are reported separately. Percentiles use nearest-rank.
- Resources: a host-side monitor samples Docker memory for the two named backend containers without mounting the Docker socket into the runner. Report their maximum memory and the runner process's maximum RSS. Record host details and exact image/model versions.

Do not treat the provisional 90% Recall@5 figure as a pass/fail condition. If the small corpus cannot support a reliable gate or store choice, record the limitation and keep the decision open.
