# S3 bilingual retrieval benchmark: review summary

- Dataset: `s3-3`; 34 documents.
- Queries: 80 language queries over 40 paired scenarios; language pairs are not independent samples.
- Run: 2026-10-31T08:12:08.285907+00:00; command: `make benchmark-s3`.
- Embedding: `intfloat/multilingual-e5-large` at revision `ac6781cd1cf88b8306a536d7c9d18a5bd57cc14b`.
- Backends: PostgreSQL 15.19 with pgvector 0.8.7; Qdrant 1.20.0 with qdrant-client 1.20.0.

## Results

Recall@5, MRR@5, and nDCG@5 are macro-averaged over the query set. Search latency is client-observed and excludes query embedding; end-to-end latency includes query embedding.

| Backend | Variant | Recall@5 | MRR@5 | nDCG@5 | Hard-negative outrank | Search p95 (ms) | End-to-end p95 (ms) | Ranking vs client RRF 50:50 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| pgvector | Client-side RRF 50:50 | 0.8875 | 0.7342 | 0.7722 | 15.0% | 2.161 | 178.933 | n/a |
| pgvector | Client-side RRF 70:30 | 0.9187 | 0.7758 | 0.8114 | 15.0% | 2.158 | 171.742 | n/a |
| pgvector | Client-side RRF 80:20 | 0.9187 | 0.7821 | 0.8156 | 15.0% | 2.192 | 180.154 | n/a |
| pgvector | Strict lexical gate | 0.9625 | 0.9035 | 0.9170 | 10.0% | 1.714 | 173.584 | n/a |
| pgvector | Single-roundtrip SQL CTE | 0.8875 | 0.7342 | 0.7722 | 15.0% | 1.998 | 174.754 | 80/80 exact; 100.0% overlap |
| qdrant | Client-side RRF 50:50 | 0.9000 | 0.7273 | 0.7707 | 10.0% | 54.554 | 210.879 | n/a |
| qdrant | Client-side RRF 70:30 | 0.9187 | 0.7562 | 0.7975 | 10.0% | 54.674 | 219.477 | n/a |
| qdrant | Client-side RRF 80:20 | 0.9187 | 0.7698 | 0.8076 | 10.0% | 54.213 | 219.543 | n/a |
| qdrant | Strict lexical gate | 0.9625 | 0.9035 | 0.9170 | 10.0% | 4.323 | 171.339 | n/a |
| qdrant | Single-request native FusionQuery RRF | 0.9500 | 0.7848 | 0.8265 | 5.0% | 2.814 | 176.110 | 17/80 exact; 99.7% overlap |
| qdrant | Single-request RrfQuery parity parameters | 0.9000 | 0.7279 | 0.7712 | 10.0% | 2.766 | 172.017 | 39/80 exact; 99.8% overlap |

Single-roundtrip ranking comparisons use exact ordered-list equality and mean top-20 candidate-set overlap. Similar candidate sets do not imply identical ranking formulas.

## Runtime and verified capabilities

- Hardware: 12th Gen Intel(R) Core(TM) i7-12650H; Linux/WSL2.
- Container peak memory: s3-retrieval-benchmark-pgvector-1: 83.0 MiB; s3-retrieval-benchmark-qdrant-1: 203.1 MiB
- Benchmark process peak RSS: 1845.1 MiB.
- Qdrant text query: native BM25 over a full-text payload index; any-term retrieval; TextQuery has no score-threshold or phrase field
- Qdrant score threshold: supported as a query_points/Prefetch parameter; tested separately, not applied because score scales are backend-specific
- Qdrant strict matching and phrase support: MatchText filter over the full query acts conjunctively; TextIndexParams.phrase_matching and MatchPhrase are supported when phrase matching is enabled on the index.
- Qdrant server fusion: Prefetch accepts TextQuery; FusionQuery(RRF) and RrfQuery with k and weights are supported

## Interpretation and limits

- This is a small local benchmark, not production-performance evidence or a statistically significant store decision.
- PostgreSQL and Qdrant use different lexical tokenization and scoring; strict gates are operationally analogous, not token-identical.
- Qdrant native fusion and client-side RRF are distinct. Review the measured ranking comparison rather than assuming parity.
- The owner selected PostgreSQL FTS + pgvector for POC retrieval in [ADR-0015](../../../docs/adr/0015-postgresql-retrieval-store.md); these benchmark results remain local evidence, not production or statistical proof.
- Detailed language, task, challenge, and per-query results are in the machine-readable report.

## Detailed artifacts

- Machine-readable measurements: [s3-benchmark.json](./s3-benchmark.json).
- Dataset and qrels: [s3_dataset.json](../s3_dataset.json).
- Procedure and limitations: [runbook](../../../docs/runbooks/s3-retrieval-benchmark.md).
- Verified Qdrant capabilities: [F-025](../../../docs/facts-register.md#f-025).
