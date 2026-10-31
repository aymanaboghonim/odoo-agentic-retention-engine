# RAG evaluation

Use this skill when creating or changing a retrieval corpus, relevance labels, retrieval benchmark, or its report.

## Rules

- Use only synthetic text. A frozen snapshot may use synthetic Odoo demo product records; exclude customer, cart, order, price, and stock data. Benchmark runs never connect to Odoo.
- Keep corpus and relevance labels versioned. Author labels from corpus facts before viewing backend rankings.
- Stratify queries by the task and failure mode they represent. Record language, task, challenge type, targets, hard negatives, and rationale.
- Use complete graded judgments when comparing ranked results. State the relevance scale and which grades count as relevant for each metric.
- Review bilingual query meaning and labels independently; do not assume machine translation preserves intent.
- Keep same-language retrieval results separate. English/Arabic translations of the same scenario are paired observations, not independent samples.
- Ground every query attribute and relevance label in the frozen corpus; do not invent product or policy details.
- Audit lexical overlap: allow deliberate title/term overlap in lexical cases, and check copied phrases/title leakage in paraphrase and contrastive cases.
- Keep lexical queries focused on search terms, not agent instructions or cart-state filler.
- Write hard-negative queries with positive target constraints only. Do not mention distractor terms as negated alternatives; assign listed hard negatives grade 0 for exact-item grounding.
- Pin the embedding model package, model source revision, vector settings, backend images, and retrieval configuration in the report.
- Report each lexical algorithm by name. Do not call different ranking functions equivalent.
- Compute metrics from reviewed relevance labels. Report Recall@k, graded ranking metrics, and hard-negative behavior by language, task, and challenge type, along with latency methodology and resource observations.
- Keep CI hermetic. Container benchmarks and model downloads run only through an explicit command.
- Follow the project-wide preflight, smoke, progress, failure/output, and review-report rules in `docs/process.md`; a smoke result is not proof of full-run success or retrieval quality.
- Emit stage and query/repetition progress with elapsed time, and publish Markdown plus machine-readable results only after the same run completes successfully.
- Keep execution sequential for isolated latency measurements unless a controlled comparison shows a parallel mode preserves ranking and measurement reliability; document the comparison or why it is not relevant.
- Treat proposed quality thresholds as non-binding until measured results and owner approval support a gate.

## Procedure

1. Freeze only approved synthetic source fields, record provenance and a content hash, and keep the benchmark disconnected from source systems.
2. Define task and challenge strata before writing queries. Select hard negatives from plausible in-corpus near matches; label synthetic decoys explicitly and report them separately.
3. Validate complete qrels, unique IDs, language pairs, coverage quotas, overlap diagnostics, and hard-negative references/zero grades with tests.
4. Require owner/reviewer approval before running a draft dataset. Review Arabic intent and labels independently from backend results.
5. Record the embedding model revision and all backend/image versions before a measurement.
6. Use the same corpus, labels, hardware, warm-up, repetition count, and candidate depth across backends.
7. Save the exact command and machine-readable result. Record lexical scoring differences and any failed or incomplete measurements.
8. Link the report from the issue and decision record. If the evidence is weak, defer the choice or gate instead of guessing.
