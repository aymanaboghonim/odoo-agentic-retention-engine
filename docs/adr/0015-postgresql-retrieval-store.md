# ADR-0015: PostgreSQL FTS and pgvector for POC retrieval

- Status: accepted
- Date: 2026-10-31

## Context

[ADR-0001](0001-odoo-system-of-record-and-ai-database.md) assigns AI-owned data to a separate PostgreSQL database. Spike S3 compared PostgreSQL with pgvector and full-text search against Qdrant using 34 synthetic documents and 80 English/Arabic queries across 40 paired scenarios. The pairs are not independent samples; results are local evidence, not production or statistical proof. See the [review summary](../../evals/retrieval/results/s3-benchmark.md) and [full report](../../evals/retrieval/results/s3-benchmark.json).

## Decision

- Use PostgreSQL full-text search and pgvector as the only active POC retrieval backend. Retrieval data is a derived, rebuildable index, not a source of truth.
- Keep database logic behind the `Retriever` port. The PostgreSQL adapter uses one server-side SQL CTE for dense and disjunctive lexical candidates and weighted reciprocal-rank fusion.
- Use dense:lexical weights of 80:20, `k = 60`, candidate depth 20, and deterministic document-ID tie-breaking. Keep lexical candidates disjunctive so partial term matches can be ranked; strict lexical gating remains an ablation, not the default.
- Do not maintain a Qdrant runtime adapter during the POC. Reconsider the store at POC exit, before MVP design is locked, if expanded use-case evidence shows a material quality, latency, filtering, scale, or operations advantage.

## Alternatives

- **Qdrant as the POC store:** at 80:20 its Recall@5 matched PostgreSQL (0.9187), but PostgreSQL had higher MRR@5 (0.7821 vs. 0.7698) and nDCG@5 (0.8156 vs. 0.8076). The local run also measured lower PostgreSQL container peak memory (83.0 vs. 203.1 MiB); neither memory nor latency is production evidence. Qdrant's server-side fusion did not reproduce client-side RRF ordering exactly.
- **Keep both stores active:** rejected to avoid maintaining two runtime adapters before the POC proves a need. The `Retriever` port preserves a seam for a later switch.
- **Client-side two-call fusion:** PostgreSQL's 50:50 SQL-CTE run matched client-side ranking for all 80 queries and measured 1.998 ms search p95 versus 2.161 ms. The selected 80:20 ratio was evaluated in the weighted client-side sweep, not separately in the CTE; verify the 80:20 CTE path during the POC.
- **Strict lexical gate:** it scored higher on this small dataset, but requiring every query term can exclude vocabulary-mismatch and paraphrase candidates. Keep it as a measured alternative.

## Consequences

- One database supports vector and lexical retrieval, and the port limits coupling to its query API.
- The 80:20 choice is specific to the small S3 evaluation set. Validate it on the expanded retention-engine golden set and review the store again at POC exit.
- PostgreSQL Arabic text search and the pinned model are documented in [F-008](../facts-register.md#f-008) and [F-007](../facts-register.md#f-007); benchmark package and Qdrant capability versions are in [F-023](../facts-register.md#f-023) and [F-025](../facts-register.md#f-025).
