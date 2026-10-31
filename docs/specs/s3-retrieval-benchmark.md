# Spec: S3 bilingual retrieval benchmark

- Issue: #47
- ADRs: ADR-0001 (derived retrieval index), ADR-0006 (retrieval benchmark), ADR-0008 (language policy).
- Status: agreed

## Purpose

Compare pgvector and Qdrant for product and customer-facing policy retrieval in the abandoned-cart retention POC, using a fixed English/Arabic dataset based on synthetic Odoo demo products.

## Scope and non-goals

Scope: one fixed dense embedding model; dense-only, lexical-only, and hybrid retrieval; versioned relevance judgments; latency, RAM, and operational observations; and symmetric closing ablations for weighted RRF, strict lexical gating, and single-roundtrip fusion.

The corpus is a one-time snapshot of Odoo demo `product.template` records of type `consu` or `combo`, plus synthetic policy text. It contains no prices, stock, customers, carts, or orders. The benchmark uses only the checked-in snapshot and never connects to Odoo. Discount eligibility and caps remain code-enforced; retrieval does not authorize or change money.

Non-goals: cross-language retrieval, live Odoo access during a run, production synchronization, monetary operating-cost estimates, and choosing a numeric release gate before results exist.

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| S3-1 | The versioned corpus contains only synthetic demo product text and synthetic policy text, excludes customer/cart data and monetary/stock fields, and has a verified content hash. | `test_retrieval_corpus_is_versioned_synthetic_only` and `test_dataset_corpus_hash_rejects_document_changes` |
| S3-2 | Benchmark execution refuses a dataset pending owner review, never connects to Odoo, and publishes the canonical report only after retrieval and memory collection succeed. | `test_benchmark_refuses_unreviewed_dataset_before_starting_services`, `test_default_checks_do_not_start_benchmark_services`, and `test_launcher_uses_caller_identity_and_run_scoped_report` |
| S3-3 | Forty underlying scenarios have paired English and Arabic queries: 30 product-grounding and 10 policy-grounding scenarios; English and Arabic results are reported separately. | `test_retrieval_corpus_is_versioned_synthetic_only`, `test_dataset_matches_approved_query_strata`, and `test_report_metrics_include_language_recall_and_latency_percentiles` |
| S3-4 | Each language has 12 lexical, 18 paraphrase, and 10 hard-negative cases. Lexical queries omit agent/cart-state filler; non-lexical queries avoid exact target-title tokens except necessary target descriptors; contrastive queries use positive target constraints, do not copy distractor titles or target-description phrases, and listed hard negatives have grade 0. | `test_dataset_matches_approved_query_strata`, `test_lexical_queries_avoid_agent_and_cart_state_filler`, `test_dataset_has_no_full_target_title_in_nonlexical_queries`, `test_nonlexical_queries_avoid_title_tokens_except_required_hard_negative_attribute`, `test_hard_negative_queries_use_positive_constraints_and_zero_grade_negatives`, `test_dataset_rejects_distractor_title_in_hard_negative_query`, `test_product_hard_07_uses_synonyms_for_source_component_terms`, and `test_revised_scenarios_avoid_target_collision_and_grade_alternative` |
| S3-5 | Relevance grades are complete over the fixed corpus: 3 exact, 2 acceptable when requested, 1 related but insufficient, 0 irrelevant. Contrastive hard negatives for exact item/policy grounding are grade 0. | `test_dataset_qrels_and_hard_negatives_are_complete`, `test_dataset_rejects_unknown_relevance_ids`, `test_dataset_rejects_nonzero_hard_negative_grade`, and `test_revised_scenarios_avoid_target_collision_and_grade_alternative` |
| S3-6 | Both stores use the same `intfloat/multilingual-e5-large` revision, query/passage prefixes, and dense-vector settings. | `test_dense_embedding_configuration_matches_across_backends` |
| S3-7 | PostgreSQL lexical candidates use disjunction across normalized query terms so partial term matches are not excluded before ranking; backend lexical methods and versions are named in every result and are not described as equivalent algorithms. | `test_postgres_lexical_query_uses_disjunctive_terms` and `test_report_identifies_backend_lexical_methods` |
| S3-8 | Both backends use the same candidate depth and deterministic tie-break for client-side RRF; the weighted sweep covers 50:50, 70:30, and 80:20 with one shared formula and `k`. | `test_rrf_configuration_and_tie_breaks_are_shared` and `test_weighted_rrf_uses_shared_weights_and_formula` |
| S3-9 | Baseline and ablation Recall@5, MRR@5, nDCG@5, and hard-negative outrank rate are reported overall and by language/task/challenge where applicable; client-side, single-roundtrip, embedding, and end-to-end latency remain distinguishable. | `test_evaluator_reports_task_strata_and_hard_negative_rate`, `test_report_metrics_include_language_recall_and_latency_percentiles`, and `test_ablation_report_has_all_requested_variants` |
| S3-10 | CI tests remain hermetic, and the provisional 90% Recall@5 figure is not a pass/fail gate. | `make ci`, `test_default_checks_do_not_start_benchmark_services`, and `test_report_does_not_apply_provisional_recall_target_as_gate` |
| S3-11 | Capability claims for Qdrant text search and fusion match both the pinned client schemas and live pinned server behavior; unsupported features and algorithm differences are explicit. | `test_report_documents_pinned_qdrant_capabilities` and explicit runtime capability probes in `make benchmark-s3` |
| S3-12 | Strict lexical gating requires every surviving query term in both systems: PostgreSQL uses conjunctive `plainto_tsquery`; Qdrant BM25 uses `TextQuery` plus a full-text `MatchText` filter. | `test_strict_lexical_gate_is_enabled_for_both_backends` |
| S3-13 | Single-roundtrip latency compares PostgreSQL SQL-CTE fusion and Qdrant server-side prefetch fusion against each backend's client-side two-call RRF on the same vectors, queries, candidate depth, and report methodology. | `test_single_roundtrip_fusion_matches_candidate_contracts` |
| S3-14 | Every S3 run publishes a human-readable Markdown review summary beside the machine-readable JSON, generated from the same measurements and promoted only after memory collection succeeds. | `test_markdown_summary_is_human_readable_and_links_machine_report`, `test_memory_monitor_adds_both_container_peaks_to_report`, and `test_launcher_uses_caller_identity_and_run_scoped_report` |

## Behavior

- **Given** the draft corpus, **when** the benchmark starts, **then** it exits with an explicit owner-review error before connecting to either retrieval backend or downloading a model.
- **Given** a benchmark run that fails or whose memory collection fails, **when** the launcher exits, **then** its run-scoped report is not promoted over the canonical result.
- **Given** an approved dataset, **when** the benchmark runs, **then** it uses the checked-in snapshot and complete qrels without opening an Odoo connection.
- **Given** a relevance judgment, **when** a metric is calculated, **then** grades 2 and 3 count as relevant for Recall@5; nDCG@5 uses the full 0–3 scale.
- **Given** a contrastive query, **when** it is authored, **then** it uses positive target constraints without naming a distractor in a negated alternative; each listed hard negative has relevance grade 0.
- **Given** a contrastive query, **when** a backend ranks a listed hard negative above the first relevant result, **then** that pair contributes to the hard-negative outrank rate.
- **Given** either backend's lexical retrieval, **when** a query contains multiple terms, **then** documents matching any normalized query term remain candidates for ranking; PostgreSQL uses `plainto_tsquery` terms joined with OR and `ts_rank_cd`, while Qdrant uses a BM25-scored full-text payload index and `TextQuery`. The report names both methods and does not claim scoring parity.
- **Given** either backend's dense and lexical ranked lists, **when** hybrid mode runs, **then** the same client-side RRF implementation fuses candidate lists of depth 20 using zero-based ranks and `k = 60`.
- **Given** the weighted hybrid sweep, **when** a ratio is measured, **then** the same formula `sum(weight / (k + zero_based_rank + 1))` runs on both stores for 50:50, 70:30, and 80:20 dense:lexical weights.
- **Given** a strict-gate hybrid query, **when** the lexical list is produced, **then** only documents matching every normalized query term remain eligible before fusion; the report records the distinct PostgreSQL and Qdrant filter implementations.
- **Given** a single-roundtrip hybrid run, **when** latency is measured, **then** PostgreSQL fuses ranked candidates in one SQL statement and Qdrant uses a single query request with dense and BM25 prefetches. Qdrant's native RRF parameters are recorded, and its result is not called formula-equivalent unless ranking parity is checked.
- **Given** a Qdrant feature claim, **when** the benchmark reports it, **then** the claim is supported by the locked `qdrant-client` schema and an operation against the pinned server.
- **Given** a completed benchmark intended for owner review, **when** the launcher promotes results, **then** it publishes both machine-readable JSON and a concise Markdown summary with links, key results, interpretation, provenance, and limitations.
- **Given** an absent backend, model, or required input, **when** the benchmark starts, **then** it exits with an explicit error and produces no success-shaped report.

## Risks

- The 40 English/Arabic pairs are 40 underlying scenarios, not 80 independent cases. Report paired results and avoid unsupported significance claims.
- The demo catalog is small and not representative of the expected production catalog size. If it cannot distinguish the stores, keep the decision open rather than adding arbitrary products or claiming generalization.
- Product and policy retrieval are different tasks; report them separately as well as overall.
- Queries and relevance labels must be grounded in the frozen document text. Bilingual pairs need equal intent, not word-for-word translation.
- PostgreSQL `ts_rank_cd` and Qdrant BM25 use different lexical ranking algorithms. Their practical quality is compared, not mathematical equivalence.
- Qdrant's native RRF weighted-rank formula differs from the benchmark's client-side weighted-contribution formula. Compare native server fusion latency and quality separately; do not merge those results into the weighted client-side sweep.
- Strict gates and phrase filters have different tokenization, stopword, stemming, and language behavior. Report the exact mechanisms and results rather than claiming they are semantically identical.
- Embedding time or model memory can obscure database differences. Report embedding, backend search, and end-to-end measurements separately.
- If Qdrant is selected, its index remains derived data. Phase 2 must decide source ownership, freshness, retry/idempotency, and rebuild or reconciliation behavior before implementation.
- Container resource use depends on image versions and hardware. Record exact images, host details, warm-up, repetitions, and commands.

## Open questions

- Does the reviewed set distinguish the stores well enough for a decision, or should S3 remain open pending a larger corpus?
- Do measurements justify a numeric Recall@5 gate, or should it remain open after S3?
