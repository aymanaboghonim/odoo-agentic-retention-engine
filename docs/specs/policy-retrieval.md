# Spec: Bilingual policy retrieval

- Issue: #64
- ADRs: [ADR-0001](../adr/0001-odoo-system-of-record-and-ai-database.md),
  [ADR-0015](../adr/0015-postgresql-retrieval-store.md)
- Status: agreed (owner, 2026-11-06)

## Purpose

Give the POC grounded, traceable synthetic English and MSA policy passages from
a rebuildable index in the engine database.

## Scope and non-goals

The checked-in source covers consent, contact frequency and hours, message
tone and language, and coupon terms. Policy text does not authorize actions
or decide money: the separate gate enforces financial limits. No Odoo access,
LLM calls, hosted embeddings, final retrieval gate, or production sync.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| PR-1 | Each source has a unique stable ID, version, valid EN/MSA text and verified content hash. | `test_source_validation` |
| PR-2 | Repeating an ingestion is idempotent; changed entries replace old entries and removed entries disappear atomically. | `test_ingestion_idempotent_and_atomic` |
| PR-3 | Search uses explicit language, returns at most five passages with source ID, version and text, and rejects invalid input. | `test_bilingual_retrieval_and_invalid_input` |
| PR-4 | Dense and disjunctive lexical candidates use ADR-0015's weighted RRF, depth and deterministic ties. | `test_hybrid_rrf_matches_reference` |
| PR-5 | Index and query embeddings use the same pinned model and respective passage/query prefixes; incompatible vectors fail closed. | `test_embedding_contract` |
| PR-6 | CI does not download the model or call external services. | `test_bilingual_retrieval_and_invalid_input`, `make ci` |

## Behavior

- Given a valid source snapshot, ingestion validates it and embeds the passages
  before atomically updating the derived index. A repeated snapshot does not
  create duplicate records; policies absent from the snapshot are removed.
- Policy IDs stay stable across edits; versions and hashes identify the source
  passages. Snapshot replacement avoids stale versions and leaves the previous
  index in place if validation, embedding or a database transaction fails.
- A local, pinned embedding model uses the S3 query and passage prefixes. This
  keeps index and query vectors comparable without an external embedding API.
- PostgreSQL applies separate English and Arabic text search configurations.
  Weighted fusion follows ADR-0015, without a confidence threshold: the chosen
  ratio is based on a small benchmark, not a proven safety or release gate.
- Given an EN or MSA query, the retriever returns ranked passages from that
  language with their source IDs and versions. An empty index returns no hits.
- Given malformed data, incompatible model identity, an invalid query, or a
  failed embedder, the operation fails explicitly rather than returning a
  success-shaped fallback.
- Given malicious text in a query or policy, it remains data, not SQL or
  instructions to the retriever.

## Risks

Fake embeddings in hermetic CI verify the database contract, not model quality.
The 80:20 choice is provisional S3 evidence, not a release threshold; Q13 and
expanded quality evaluation remain open. Local model loading and index rebuild
need documented preflight. The policy text is synthetic context, not proof of
customer consent or enforcement of frequency, hours or coupon rules.
