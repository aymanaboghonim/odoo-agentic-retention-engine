# ADR-0006: Retrieval store: benchmark pgvector and Qdrant, then decide

- Status: proposed (decided after spike S3)
- Date: 2026-10-24

## Context

Scale is small (thousands of products, a few hundred document chunks). Postgres with pgvector and full-text search can be enough and is simpler. Qdrant has native sparse vectors and hybrid fusion. We do not know which gives better Arabic and English results. Facts F-007 and F-008 are unverified.

## Decision

- Put retrieval behind a `Retriever` port. Keep both implementations during the POC: Postgres (pgvector + tsvector) and Qdrant.
- Benchmark both on one Arabic and English retrieval set. Measure Recall@5, latency, RAM, and operations cost.
- The winner becomes the default in a follow-up ADR that supersedes this one.
- Retrieval stores are derived indexes. They can be rebuilt. They are never the source of truth.
- Switch triggers to document: millions of chunks, heavy filtered search, or clearly better hybrid quality.

## Consequences

- Extra benchmark work, but the decision is based on data.
- Short-term cost of maintaining two adapters.
