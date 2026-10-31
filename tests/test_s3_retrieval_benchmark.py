import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import scripts.s3_retrieval_benchmark as benchmark
from scripts.monitor_s3_benchmark_memory import CONTAINER_NAMES, update_report_memory
from scripts.s3_retrieval_benchmark import (
    CANDIDATE_DEPTH,
    DATASET_PATH,
    EXECUTION_STRATEGY,
    LEXICAL_METHODS,
    MODEL_DIMENSIONS,
    MODEL_NAME,
    MODEL_REVISION,
    MODEL_SOURCE_REPO,
    MODEL_WEIGHT_SHA256,
    PASSAGE_PREFIX,
    QDRANT_CAPABILITY_SUMMARY,
    QUERY_PREFIX,
    RELEASE_GATE,
    RRF_K,
    SINGLE_ROUNDTRIP_MODES,
    WEIGHTED_RRF_CONFIGS,
    PostgresRetriever,
    ProgressReporter,
    QdrantRetriever,
    Query,
    StageFailure,
    _ranking_comparison,
    evaluate_mode,
    hard_negative_outrank_count,
    load_dataset,
    markdown_summary_path,
    mrr_at_k,
    ndcg_at_k,
    parse_docker_stats,
    parse_memory_mib,
    percentile,
    publish_report_pair,
    query_overlap_diagnostics,
    recall_at_k,
    reciprocal_rank_fusion,
    render_markdown_summary,
    run_benchmark,
    run_preflight,
    run_smoke_check,
    write_markdown_summary,
)


def test_retrieval_corpus_is_versioned_synthetic_only() -> None:
    dataset = load_dataset()

    assert dataset.version == "s3-3"
    assert len(dataset.documents) == 34
    product_documents = [document for document in dataset.documents if document.kind == "product"]
    assert len(product_documents) == 29
    assert sum(document.kind == "policy" for document in dataset.documents) == 5
    assert sum(document.source_record_type == "consu" for document in product_documents) == 28
    assert sum(document.source_record_type == "combo" for document in product_documents) == 1
    assert all(document.origin == "odoo_demo_snapshot" for document in product_documents)
    assert len([query for query in dataset.queries if query.language == "en"]) == 40
    assert len([query for query in dataset.queries if query.language == "ar"]) == 40
    assert len({query.scenario_id for query in dataset.queries}) == 40
    assert dataset.review_status == "approved"
    assert len(dataset.corpus_sha256) == 64


def test_dataset_corpus_hash_rejects_document_changes(tmp_path) -> None:
    source = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    source["documents"][0]["text_en"] += " changed"
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ValueError, match="corpus_sha256"):
        load_dataset(dataset_path)


def test_dataset_matches_approved_query_strata() -> None:
    dataset = load_dataset()
    expected = {
        ("product_grounding", "lexical"): 9,
        ("policy_grounding", "lexical"): 3,
        ("product_grounding", "paraphrase"): 13,
        ("policy_grounding", "paraphrase"): 5,
        ("product_grounding", "hard_negative"): 8,
        ("policy_grounding", "hard_negative"): 2,
    }

    for language in ("en", "ar"):
        language_queries = [query for query in dataset.queries if query.language == language]
        assert len(language_queries) == 40
        assert {
            key: sum(
                query.task == key[0] and query.challenge_type == key[1]
                for query in language_queries
            )
            for key in expected
        } == expected


def test_lexical_queries_avoid_agent_and_cart_state_filler() -> None:
    dataset = load_dataset()
    english_fillers = (
        "find ",
        "retrieve ",
        "saved in the cart",
        "left in the cart",
        "in the office catalog",
        "what does ",
    )
    arabic_fillers = ("استرجع", "اعثر", "أضيف إلى السلة", "تُرك في السلة", "كتالوج المكتب")

    for query in dataset.queries:
        if query.challenge_type != "lexical":
            continue
        fillers = english_fillers if query.language == "en" else arabic_fillers
        assert not any(filler in query.text.casefold() for filler in fillers)


def test_dataset_qrels_and_hard_negatives_are_complete() -> None:
    dataset = load_dataset()
    document_ids = {document.id for document in dataset.documents}

    for query in dataset.queries:
        assert query.relevant_ids
        assert query.relevant_ids <= document_ids
        assert query.hard_negative_ids <= document_ids
        assert not query.relevant_ids & query.hard_negative_ids
        if query.challenge_type == "hard_negative":
            assert query.hard_negative_ids


def test_hard_negative_queries_use_positive_constraints_and_zero_grade_negatives() -> None:
    dataset = load_dataset()
    english_contrast_markers = ("not", "instead of", "rather than", "do not")
    arabic_contrast_markers = ("بدلًا من", "بدلا من", "بدلاً من", "ليس", "لا تسترجع")

    for query in dataset.queries:
        if query.challenge_type != "hard_negative":
            continue
        markers = english_contrast_markers if query.language == "en" else arabic_contrast_markers
        words = " " + " ".join(re.findall(r"\w+", query.text.casefold())) + " "
        assert not any(f" {marker} " in words for marker in markers)
        assert all(query.relevance_grades.get(item, 0) == 0 for item in query.hard_negative_ids)


def test_dataset_has_no_full_target_title_in_nonlexical_queries() -> None:
    dataset = load_dataset()
    diagnostics = query_overlap_diagnostics(dataset)

    for language in ("en", "ar"):
        assert diagnostics[language]["lexical"]["queries_containing_full_target_title"] == 12
    for language in ("en", "ar"):
        for challenge_type in ("paraphrase", "hard_negative"):
            assert (
                diagnostics[language][challenge_type]["queries_containing_full_target_title"] == 0
            )


def test_nonlexical_queries_avoid_title_tokens_except_required_hard_negative_attribute() -> None:
    dataset = load_dataset()
    documents = {document.id: document for document in dataset.documents}
    for query in dataset.queries:
        if query.challenge_type == "lexical":
            continue
        query_tokens = set(re.findall(r"\w+", query.text.casefold()))
        allowed_tokens = {"with"}
        if query.scenario_id == "product-hard-01" and query.language == "en":
            allowed_tokens.update({"corner", "left"})
        elif query.scenario_id == "product-hard-01" and query.language == "ar":
            allowed_tokens.add("عمل")
        for document_id in query.relevant_ids:
            title_tokens = set(
                re.findall(
                    r"\w+",
                    getattr(documents[document_id], f"title_{query.language}").casefold(),
                )
            )
            assert not (title_tokens & query_tokens) - allowed_tokens


def test_product_hard_07_uses_synonyms_for_source_component_terms() -> None:
    dataset = load_dataset()
    queries = {
        query.language: query for query in dataset.queries if query.scenario_id == "product-hard-07"
    }

    english_terms = set(re.findall(r"\w+", queries["en"].text.casefold()))
    arabic_terms = set(re.findall(r"\w+", queries["ar"].text.casefold()))
    assert not english_terms & {"black", "brown", "chair", "drawer", "combination"}
    assert not arabic_terms & {"الأسود", "البني", "كرسي", "درج", "طقم", "مكتب"}


def test_revised_scenarios_avoid_target_collision_and_grade_alternative() -> None:
    dataset = load_dataset()
    english_queries = {
        query.scenario_id: query for query in dataset.queries if query.language == "en"
    }

    drawer_query = english_queries["product-para-10"]
    organizer_query = english_queries["product-hard-04"]
    assert drawer_query.relevance_grades["product-0024"] == 3
    assert drawer_query.relevance_grades["product-0019"] == 2
    assert drawer_query.relevant_ids.isdisjoint(organizer_query.relevant_ids)

    bundle_query = english_queries["product-para-12"]
    assert bundle_query.relevance_grades["product-0034"] == 3
    assert bundle_query.relevance_grades["product-0008"] == 2


def test_dataset_rejects_non_synthetic_documents(tmp_path) -> None:
    source = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    source["documents"][0]["synthetic"] = False
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ValueError, match="explicitly synthetic"):
        load_dataset(dataset_path)


def test_benchmark_refuses_unreviewed_dataset_before_starting_services(tmp_path) -> None:
    source = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    source["review_status"] = "draft_pending_owner_review"
    dataset_path = tmp_path / "draft-dataset.json"
    dataset_path.write_text(json.dumps(source), encoding="utf-8")
    dataset = load_dataset(dataset_path)

    with pytest.raises(ValueError, match="pending owner review"):
        run_benchmark(dataset, tmp_path / "report.json")

    assert not (tmp_path / "report.json").exists()


def test_preflight_rejects_invalid_inputs_before_docker(tmp_path, monkeypatch) -> None:
    source = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    source["review_status"] = "draft_pending_owner_review"
    dataset_path = tmp_path / "draft-dataset.json"
    dataset_path.write_text(json.dumps(source), encoding="utf-8")

    def docker_must_not_be_checked(_name: str) -> str:
        pytest.fail("Docker checks must not run for an invalid dataset")

    monkeypatch.setattr(benchmark.shutil, "which", docker_must_not_be_checked)
    with pytest.raises(StageFailure, match="pending owner review"):
        run_preflight(dataset_path, tmp_path / "reports" / "report.json")

    assert not (tmp_path / "reports").exists()


def test_preflight_rejects_missing_runner_file_before_docker(tmp_path, monkeypatch) -> None:
    real_is_file = Path.is_file

    def missing_monitor_script(path: Path) -> bool:
        if path == Path("scripts/monitor_s3_benchmark_memory.py"):
            return False
        return real_is_file(path)

    monkeypatch.setattr(Path, "is_file", missing_monitor_script)
    monkeypatch.setattr(
        benchmark.shutil,
        "which",
        lambda _name: pytest.fail("Docker checks must not run when a runner file is missing"),
    )
    with pytest.raises(StageFailure, match="monitor_s3_benchmark_memory.py"):
        run_preflight(DATASET_PATH, tmp_path / "reports" / "report.json")

    assert not (tmp_path / "reports").exists()


def test_preflight_and_smoke_document_limits(tmp_path, monkeypatch, capsys) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(benchmark.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)
    monkeypatch.setenv("S3_BENCHMARK_HF_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("S3_BENCHMARK_REPORT_DIR", raising=False)
    run_preflight(DATASET_PATH, tmp_path / "reports" / "report.json")

    preflight_output = capsys.readouterr().err
    assert "does not start services, build images, download the model" in preflight_output
    assert all("up" not in command and "build" not in command for command in commands)

    class FakeRetriever:
        def __init__(self, name: str) -> None:
            self.name = name
            self.closed = False

        def load(self, documents, vectors) -> None:
            assert len(documents) == 1
            assert len(vectors[documents[0].id]["en"]) == MODEL_DIMENSIONS

        def search_dense(self, _vector, _language: str, _limit: int) -> list[str]:
            return ["preflight-smoke-001"]

        def search_lexical(self, _text: str, _language: str, _limit: int) -> list[str]:
            return ["preflight-smoke-001"]

        def close(self) -> None:
            self.closed = True

    retrievers = [FakeRetriever("pgvector"), FakeRetriever("qdrant")]
    monkeypatch.setattr(benchmark, "PostgresRetriever", lambda: retrievers[0])
    monkeypatch.setattr(benchmark, "QdrantRetriever", lambda: retrievers[1])
    monkeypatch.setattr(benchmark, "_wait_for_services", lambda: None)
    monkeypatch.setattr(
        benchmark, "_embed", lambda _dataset: pytest.fail("Smoke must not load the model")
    )
    run_smoke_check()

    smoke_output = capsys.readouterr().err
    assert "No embedding model was loaded" in smoke_output
    assert "does not validate model downloads, full-run behavior, or retrieval quality" in (
        smoke_output
    )
    assert all(retriever.closed for retriever in retrievers)


def test_s3_smoke_checks_backends_without_model_download(monkeypatch, capsys) -> None:
    exercised: list[tuple[str, str, str]] = []

    class FakeRetriever:
        def __init__(self, name: str) -> None:
            self.name = name

        def load(self, documents, _vectors) -> None:
            exercised.append((self.name, "load", documents[0].id))

        def search_dense(self, _vector, language: str, _limit: int) -> list[str]:
            exercised.append((self.name, f"dense_{language}", "preflight-smoke-001"))
            return ["preflight-smoke-001"]

        def search_lexical(self, _text: str, language: str, _limit: int) -> list[str]:
            exercised.append((self.name, f"lexical_{language}", "preflight-smoke-001"))
            return ["preflight-smoke-001"]

        def close(self) -> None:
            pass

    monkeypatch.setattr(benchmark, "PostgresRetriever", lambda: FakeRetriever("pgvector"))
    monkeypatch.setattr(benchmark, "QdrantRetriever", lambda: FakeRetriever("qdrant"))
    monkeypatch.setattr(benchmark, "_wait_for_services", lambda: None)
    monkeypatch.setattr(
        benchmark, "_embed", lambda _dataset: pytest.fail("Smoke must not load the model")
    )
    run_smoke_check()

    assert len(exercised) == 10
    assert {row[1] for row in exercised} >= {
        "dense_en",
        "dense_ar",
        "lexical_en",
        "lexical_ar",
    }
    assert "No embedding model was loaded" in capsys.readouterr().err


def test_progress_reports_stage_units_and_elapsed(capsys) -> None:
    reporter = ProgressReporter(heartbeat_seconds=1, progress_interval_seconds=0)
    with reporter.stage("document_load", total=2, unit="backends") as advance:
        advance(1)
        advance(1)

    output = capsys.readouterr().err
    assert "progress stage=document_load status=started" in output
    assert "completed=1 total=2 unit=backends" in output
    assert "completed=2 total=2 unit=backends" in output
    assert "status=completed" in output
    assert "elapsed_seconds=" in output


def test_slow_stage_emits_heartbeat(capsys) -> None:
    reporter = ProgressReporter(heartbeat_seconds=0.01)
    with reporter.stage("model_download"):
        time.sleep(0.035)

    assert "progress stage=model_download status=heartbeat" in capsys.readouterr().err


def test_failure_names_stage_and_cleans_run_resources(monkeypatch, capsys) -> None:
    class FakeRetriever:
        def __init__(self, name: str) -> None:
            self.name = name
            self.closed = False

        def load(self, _documents, _vectors) -> None:
            pass

        def search_dense(self, _vector, _language: str, _limit: int) -> list[str]:
            return ["preflight-smoke-001"]

        def search_lexical(self, _text: str, _language: str, _limit: int) -> list[str]:
            if self.name == "qdrant":
                raise RuntimeError("synthetic query failure")
            return ["preflight-smoke-001"]

        def close(self) -> None:
            self.closed = True

    retrievers = [FakeRetriever("pgvector"), FakeRetriever("qdrant")]
    monkeypatch.setattr(benchmark, "PostgresRetriever", lambda: retrievers[0])
    monkeypatch.setattr(benchmark, "QdrantRetriever", lambda: retrievers[1])
    monkeypatch.setattr(benchmark, "_wait_for_services", lambda: None)

    with pytest.raises(StageFailure, match="smoke_dense_and_lexical_queries"):
        run_smoke_check()

    assert all(retriever.closed for retriever in retrievers)
    output = capsys.readouterr().err
    assert "status=failed" in output
    assert "error_type=RuntimeError" in output


def test_dataset_rejects_unknown_relevance_ids(tmp_path) -> None:
    source = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    source["scenarios"][0]["qrels"]["overrides"]["customer-001"] = 3
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown document"):
        load_dataset(dataset_path)


def test_dataset_rejects_nonzero_hard_negative_grade(tmp_path) -> None:
    source = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    scenario = next(row for row in source["scenarios"] if row["id"] == "product-hard-01")
    scenario["qrels"]["overrides"]["product-0010"] = 1
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ValueError, match="Hard-negative document must have relevance grade 0"):
        load_dataset(dataset_path)


def test_dataset_rejects_distractor_title_in_hard_negative_query(tmp_path) -> None:
    source = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    scenario = next(row for row in source["scenarios"] if row["id"] == "product-hard-01")
    scenario["text_en"] = "Corner Desk Right Sit"
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ValueError, match="copies a distractor title"):
        load_dataset(dataset_path)


def test_dense_embedding_configuration_matches_across_backends() -> None:
    assert MODEL_NAME == "intfloat/multilingual-e5-large"
    assert MODEL_DIMENSIONS == 1024
    assert MODEL_SOURCE_REPO == "Qdrant/multilingual-e5-large-onnx"
    assert MODEL_REVISION == "ac6781cd1cf88b8306a536d7c9d18a5bd57cc14b"
    assert MODEL_WEIGHT_SHA256 == "0cf1883fee81c63819a44e2ba0efa51d4043d9759685a4ebebbde97e0623d15c"


def test_postgres_lexical_query_uses_disjunctive_terms() -> None:
    class Result:
        def fetchall(self) -> list[tuple[str]]:
            return [("matched-document",)]

    class Connection:
        statement = ""
        parameters: tuple[object, ...] = ()

        def execute(self, statement: str, parameters: tuple[object, ...]) -> Result:
            self.statement = statement
            self.parameters = parameters
            return Result()

    retriever = PostgresRetriever.__new__(PostgresRetriever)
    retriever.connection = Connection()

    assert retriever.search_lexical("office chair", "en", 20) == ["matched-document"]
    assert "replace(" in retriever.connection.statement
    assert "plainto_tsquery(%s, %s)::text" in retriever.connection.statement
    assert "' & '" in retriever.connection.statement
    assert "' | '" in retriever.connection.statement
    assert retriever.connection.parameters == ("english", "office chair", 20)
    assert retriever.search_lexical("office chair", "en", 20, strict=True) == ["matched-document"]
    assert "replace(" not in retriever.connection.statement
    assert "plainto_tsquery(%s, %s)" in retriever.connection.statement
    assert retriever.connection.parameters == ("english", "office chair", 20)
    assert QUERY_PREFIX == "query: "
    assert PASSAGE_PREFIX == "passage: "


def test_postgres_single_roundtrip_uses_bounded_dense_and_lexical_candidates() -> None:
    class Result:
        def fetchall(self) -> list[tuple[str]]:
            return [("fused-document",)]

    class Connection:
        statement = ""
        parameters: tuple[object, ...] = ()

        def execute(self, statement: str, parameters: tuple[object, ...]) -> Result:
            self.statement = statement
            self.parameters = parameters
            return Result()

    retriever = PostgresRetriever.__new__(PostgresRetriever)
    retriever.connection = Connection()

    assert retriever.search_hybrid_single_roundtrip(
        [0.25, 0.5], "office chair", "en", 20, "hybrid_sql_cte"
    ) == ["fused-document"]
    assert "dense_candidates AS" in retriever.connection.statement
    assert "lexical_candidates AS" in retriever.connection.statement
    assert "UNION ALL" in retriever.connection.statement
    assert "SUM(weight / (%s + rank + 1))" in retriever.connection.statement
    assert retriever.connection.parameters == (
        "english",
        "office chair",
        "[0.25,0.5]",
        CANDIDATE_DEPTH,
        CANDIDATE_DEPTH,
        RRF_K,
        0.5,
        0.5,
        20,
    )


def test_report_identifies_backend_lexical_methods() -> None:
    postgres_methods = LEXICAL_METHODS["pgvector"]
    qdrant_method = LEXICAL_METHODS["qdrant"]
    assert isinstance(postgres_methods, dict)
    assert isinstance(qdrant_method, str)
    assert "ts_rank_cd" in postgres_methods["arabic"]
    assert qdrant_method.startswith("native BM25")


def test_rrf_configuration_and_tie_breaks_are_shared() -> None:
    result = reciprocal_rank_fusion([["b", "a", "a"], ["a", "b"]], limit=2)

    assert result == ["a", "b"]
    assert RRF_K == 60
    assert CANDIDATE_DEPTH == 20
    assert WEIGHTED_RRF_CONFIGS == {
        "50_50": (0.5, 0.5),
        "70_30": (0.7, 0.3),
        "80_20": (0.8, 0.2),
    }
    assert SINGLE_ROUNDTRIP_MODES == {
        "pgvector": ("hybrid_sql_cte",),
        "qdrant": ("hybrid_server_fusion_default", "hybrid_server_rrf_parity"),
    }


def test_weighted_rrf_uses_shared_weights_and_formula() -> None:
    rankings = [["dense-first", "lexical-first"], ["lexical-first", "dense-first"]]

    assert reciprocal_rank_fusion(rankings, weights=WEIGHTED_RRF_CONFIGS["70_30"]) == [
        "dense-first",
        "lexical-first",
    ]
    assert reciprocal_rank_fusion(rankings, weights=WEIGHTED_RRF_CONFIGS["80_20"]) == [
        "dense-first",
        "lexical-first",
    ]
    assert reciprocal_rank_fusion(rankings, weights=WEIGHTED_RRF_CONFIGS["50_50"]) == [
        "dense-first",
        "lexical-first",
    ]
    with pytest.raises(ValueError, match="sum to 1"):
        reciprocal_rank_fusion(rankings, weights=(0.7, 0.4))


def test_ablation_report_has_all_requested_variants() -> None:
    assert tuple(WEIGHTED_RRF_CONFIGS) == ("50_50", "70_30", "80_20")
    assert SINGLE_ROUNDTRIP_MODES["pgvector"] == ("hybrid_sql_cte",)
    assert SINGLE_ROUNDTRIP_MODES["qdrant"] == (
        "hybrid_server_fusion_default",
        "hybrid_server_rrf_parity",
    )


def test_ranking_comparison_reports_exact_order_and_candidate_overlap() -> None:
    comparison = _ranking_comparison(
        {"q1": ["a", "b"], "q2": ["c", "d"]},
        {"q1": ["a", "b"], "q2": ["d", "c"]},
    )

    assert comparison == {
        "exact_order_match_queries": 1,
        "query_count": 2,
        "exact_order_match_rate": 0.5,
        "mean_candidate_set_overlap": 1.0,
    }


def test_report_documents_pinned_qdrant_capabilities() -> None:
    assert "any-term retrieval" in QDRANT_CAPABILITY_SUMMARY["text_query"]
    assert "query_points/Prefetch" in QDRANT_CAPABILITY_SUMMARY["score_threshold"]
    assert "MatchPhrase" in QDRANT_CAPABILITY_SUMMARY["phrase_matching"]
    assert "RrfQuery" in QDRANT_CAPABILITY_SUMMARY["server_fusion"]
    assert "distinct" in QDRANT_CAPABILITY_SUMMARY["native_vs_parity_note"]


def test_markdown_summary_is_human_readable_and_links_machine_report(
    tmp_path: Path,
) -> None:
    report = json.loads(
        Path("evals/retrieval/results/s3-benchmark.json").read_text(encoding="utf-8")
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    summary_path = write_markdown_summary(report_path, report)
    markdown = summary_path.read_text(encoding="utf-8")

    assert summary_path == tmp_path / "report.md"
    assert markdown == render_markdown_summary(report)
    assert markdown.startswith("# S3 bilingual retrieval benchmark: review summary")
    assert "| Backend | Variant | Recall@5 | MRR@5 | nDCG@5 |" in markdown
    assert "Hard-negative outrank" in markdown
    assert "Client-side RRF 50:50" in markdown
    assert "Single-roundtrip SQL CTE" in markdown
    assert "Single-request native FusionQuery RRF" in markdown
    assert "statistically significant store decision" in markdown
    assert "not production-performance evidence" in markdown
    assert "PostgreSQL FTS + pgvector for POC retrieval in [ADR-0015]" in markdown
    assert "Qdrant score threshold:" in markdown
    assert "[s3-benchmark.json](./s3-benchmark.json)" in markdown


def test_execution_mode_metadata_is_reported() -> None:
    report = json.loads(
        Path("evals/retrieval/results/s3-benchmark.json").read_text(encoding="utf-8")
    )
    methodology = report["methodology"]
    assert isinstance(methodology, dict)
    methodology["execution_strategy"] = EXECUTION_STRATEGY

    markdown = render_markdown_summary(report)

    assert EXECUTION_STRATEGY["mode"] == "sequential"
    assert "Execution mode: sequential" in markdown
    assert "not throughput" in EXECUTION_STRATEGY["rationale"]


def test_benchmark_report_records_execution_mode(tmp_path: Path, monkeypatch) -> None:
    dataset = load_dataset()

    class FakePostgres:
        name = "pgvector"
        version = "PostgreSQL 15.0"

        def load(self, _documents, _vectors) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeQdrant:
        name = "qdrant"
        version_info = {"version": "1.20.0"}

        def load(self, _documents, _vectors) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(benchmark, "_wait_for_services", lambda: None)
    monkeypatch.setattr(benchmark, "PostgresRetriever", FakePostgres)
    monkeypatch.setattr(benchmark, "QdrantRetriever", FakeQdrant)
    monkeypatch.setattr(
        benchmark,
        "_embed",
        lambda _dataset: ({}, {}, {"query": 1.0}, lambda _text: [0.0]),
    )
    monkeypatch.setattr(benchmark, "_pgvector_version", lambda: "0.8.7")
    monkeypatch.setattr(benchmark, "_distribution_version", lambda _name: "test-version")
    monkeypatch.setattr(
        benchmark,
        "_embedding_latency_report",
        lambda _latencies: {"p50_ms": 1.0, "p95_ms": 1.0, "query_count": 1},
    )
    monkeypatch.setattr(
        benchmark, "_hardware_info", lambda: {"platform": "test", "cpu_model": "test"}
    )
    monkeypatch.setattr(benchmark, "_benchmark_process_max_rss_mib", lambda: 1.0)

    def fake_run_mode(_retriever, run_dataset, _vectors, _encode, _mode, on_query_complete=None):
        if on_query_complete is not None:
            for _query in run_dataset.queries:
                on_query_complete()
        return ({}, {query.id: ["doc"] for query in run_dataset.queries})

    monkeypatch.setattr(benchmark, "_run_mode", fake_run_mode)
    monkeypatch.setattr(benchmark, "write_markdown_summary", lambda _path, _report: _path)
    report_path = tmp_path / "report.json"

    run_benchmark(dataset, report_path)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["methodology"]["execution_strategy"] == EXECUTION_STRATEGY


def test_report_pair_publication_rolls_back_after_failure(tmp_path: Path, monkeypatch) -> None:
    staged_report = tmp_path / ".s3-benchmark.staged"
    staged_summary = markdown_summary_path(staged_report)
    final_report = tmp_path / "s3-benchmark.json"
    final_summary = markdown_summary_path(final_report)
    staged_report.write_text('{"run": "new"}\n', encoding="utf-8")
    staged_summary.write_text("# new run\n", encoding="utf-8")
    final_report.write_text('{"run": "previous"}\n', encoding="utf-8")
    final_summary.write_text("# previous run\n", encoding="utf-8")

    real_replace = benchmark.os.replace

    def fail_new_report(source, destination) -> None:
        if Path(source) == staged_report:
            raise OSError("injected report-publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(benchmark.os, "replace", fail_new_report)
    with pytest.raises(OSError, match="injected report-publish failure"):
        publish_report_pair(staged_report, final_report)

    assert final_report.read_text(encoding="utf-8") == '{"run": "previous"}\n'
    assert final_summary.read_text(encoding="utf-8") == "# previous run\n"


@pytest.mark.parametrize(
    ("failure_stage", "expected_status"),
    (("benchmark", 23), ("cleanup", 17)),
)
def test_launcher_failure_keeps_published_reports(
    tmp_path: Path, failure_stage: str, expected_status: int
) -> None:
    workspace = tmp_path / "workspace"
    scripts = workspace / "scripts"
    results = workspace / "evals" / "retrieval" / "results"
    bin_dir = tmp_path / "bin"
    home = tmp_path / "home"
    scripts.mkdir(parents=True)
    results.mkdir(parents=True)
    bin_dir.mkdir()
    home.mkdir()
    (scripts / "run_s3_benchmark.sh").write_text(
        Path("scripts/run_s3_benchmark.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (results / "s3-benchmark.json").write_text('{"run": "previous"}\n', encoding="utf-8")
    (results / "s3-benchmark.md").write_text("# previous run\n", encoding="utf-8")

    uv = bin_dir / "uv"
    uv.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    uv.chmod(0o755)
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$S3_TEST_DOCKER_LOG"
case " $* " in
  *" benchmark --smoke "*)
    exit 0
    ;;
  *" benchmark --report "*)
    previous=""
    for argument in "$@"; do
      if [[ "$previous" == "--report" ]]; then
        report_container_path="$argument"
      fi
      previous="$argument"
    done
    if [[ "$S3_TEST_FAILURE_STAGE" == "cleanup" ]]; then
      report_name="$(basename "$report_container_path")"
      printf '{"run": "new"}\\n' > "$S3_BENCHMARK_REPORT_DIR/$report_name"
      printf '# new run\\n' > "$S3_BENCHMARK_REPORT_DIR/$report_name.md"
      exit 0
    fi
    exit 23
    ;;
  *" down --volumes "*)
    if [[ "$S3_TEST_FAILURE_STAGE" == "cleanup" && ! -e "$S3_TEST_DOWN_MARKER" ]]; then
      touch "$S3_TEST_DOWN_MARKER"
      exit 17
    fi
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
    environment["HOME"] = str(home)
    environment["HF_HOME"] = str(home / ".cache" / "huggingface")
    environment["S3_TEST_DOCKER_LOG"] = str(tmp_path / "docker.log")
    environment["S3_TEST_DOWN_MARKER"] = str(tmp_path / "down-once")
    environment["S3_TEST_FAILURE_STAGE"] = failure_stage
    result = subprocess.run(
        ["bash", "scripts/run_s3_benchmark.sh"],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == expected_status
    assert (results / "s3-benchmark.json").read_text(encoding="utf-8") == ('{"run": "previous"}\n')
    assert (results / "s3-benchmark.md").read_text(encoding="utf-8") == "# previous run\n"
    assert not list(results.glob(".s3-benchmark.*"))
    assert "status=failed" in result.stderr
    if failure_stage == "cleanup":
        assert "stage=cleanup_compose status=failed" in result.stderr
    else:
        assert "stage=benchmark status=failed" in result.stderr

    launcher = Path("scripts/run_s3_benchmark.sh").read_text(encoding="utf-8")
    assert launcher.index("run_stage preflight") < launcher.index("run_stage docker_startup")
    assert launcher.index("run_stage backend_smoke") < launcher.index("run_stage benchmark")
    smoke_exit = launcher.index("if ((smoke_only)); then")
    assert launcher.index("run_stage backend_smoke") < smoke_exit
    assert smoke_exit < launcher.index("run_stage benchmark")
    cleanup_index = launcher.rindex("run_stage cleanup_compose")
    publish_index = launcher.index("run_stage publish_reports")
    assert cleanup_index > launcher.index('wait "$monitor_pid"')
    assert cleanup_index < publish_index
    assert publish_index > launcher.index("run_stage benchmark")
    assert "--publish-staged-report" in launcher
    assert "mv -f --" not in launcher
    assert "if ((compose_started)); then" in launcher


def test_markdown_summary_path_handles_canonical_and_staged_reports() -> None:
    assert markdown_summary_path(Path("/reports/s3-benchmark.json")) == Path(
        "/reports/s3-benchmark.md"
    )
    assert markdown_summary_path(Path("/reports/.s3-benchmark.abc")) == Path(
        "/reports/.s3-benchmark.abc.md"
    )


def test_strict_lexical_gate_is_enabled_for_both_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    class Model:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    models = SimpleNamespace(
        TextQuery=Model,
        Filter=Model,
        FieldCondition=Model,
        MatchText=Model,
    )
    qdrant_module = ModuleType("qdrant_client")
    setattr(qdrant_module, "models", models)
    monkeypatch.setitem(sys.modules, "qdrant_client", qdrant_module)

    class Client:
        query = None

        def query_points(self, **kwargs: object) -> SimpleNamespace:
            self.query = kwargs
            return SimpleNamespace(points=[SimpleNamespace(payload={"doc_id": "match"})])

    retriever = QdrantRetriever.__new__(QdrantRetriever)
    retriever.client = Client()

    assert retriever.search_lexical("office chair", "en", 20, strict=True) == ["match"]
    query = retriever.client.query
    assert query is not None
    query_filter = query["query_filter"]
    assert query_filter.must[0].match.text == "office chair"


def test_single_roundtrip_fusion_matches_candidate_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Model:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    models = SimpleNamespace(
        Prefetch=Model,
        TextQuery=Model,
        FusionQuery=Model,
        Fusion=SimpleNamespace(RRF="rrf"),
        RrfQuery=Model,
        Rrf=Model,
    )
    qdrant_module = ModuleType("qdrant_client")
    setattr(qdrant_module, "models", models)
    monkeypatch.setitem(sys.modules, "qdrant_client", qdrant_module)

    class Client:
        query = None

        def query_points(self, **kwargs: object) -> SimpleNamespace:
            self.query = kwargs
            return SimpleNamespace(points=[SimpleNamespace(payload={"doc_id": "fused"})])

    retriever = QdrantRetriever.__new__(QdrantRetriever)
    retriever.client = Client()

    assert retriever.search_hybrid_single_roundtrip(
        [0.25, 0.5], "office chair", "en", 20, "hybrid_server_rrf_parity"
    ) == ["fused"]
    query = retriever.client.query
    assert query is not None
    assert len(query["prefetch"]) == 2
    assert [item.limit for item in query["prefetch"]] == [CANDIDATE_DEPTH, CANDIDATE_DEPTH]
    assert query["query"].rrf.k == RRF_K + 1
    assert query["query"].rrf.weights == [1.0, 1.0]


def test_recall_at_k_counts_unique_relevant_documents() -> None:
    assert recall_at_k(["wanted", "wanted", "other"], frozenset({"wanted"}), 5) == 1
    assert recall_at_k(["other"], frozenset({"wanted"}), 5) == 0


def test_mrr_and_ndcg_use_relevance_grades() -> None:
    grades = {"exact": 3, "related": 1}

    assert mrr_at_k(["related", "exact"], frozenset({"exact"}), 5) == 0.5
    assert ndcg_at_k(["related", "exact"], grades, 5) < 1
    assert ndcg_at_k(["exact", "related"], grades, 5) == 1


def test_hard_negative_outrank_rate_counts_only_negatives_above_target() -> None:
    relevant = frozenset({"target"})
    hard_negatives = frozenset({"near-1", "near-2"})

    assert hard_negative_outrank_count(["near-1", "target"], relevant, hard_negatives) == (1, 2)
    assert hard_negative_outrank_count(["target", "near-1"], relevant, hard_negatives) == (0, 2)


def test_evaluator_reports_task_strata_and_hard_negative_rate() -> None:
    product_query = Query(
        "scenario-en",
        "scenario",
        "en",
        "query",
        "product_grounding",
        "hard_negative",
        {"target": 3, "near": 0},
        frozenset({"near"}),
    )
    policy_query = Query(
        "scenario-ar",
        "scenario-ar",
        "ar",
        "policy query",
        "policy_grounding",
        "lexical",
        {"policy": 3},
        frozenset(),
    )
    result = evaluate_mode(
        [product_query, policy_query],
        {"scenario-en": ["near", "target"], "scenario-ar": ["policy"]},
        [1.0, 1.0],
        [2.0, 2.0],
        includes_query_embedding=True,
    )

    task_metrics = result["metrics_by_task"]
    assert isinstance(task_metrics, dict)
    product_metrics = task_metrics["product_grounding"]
    assert isinstance(product_metrics, dict)
    product_ndcg = product_metrics["ndcg_at_5"]
    assert isinstance(product_ndcg, float)
    assert product_ndcg < 1
    challenge_metrics = result["metrics_by_challenge_type"]
    assert isinstance(challenge_metrics, dict)
    hard_negative_metrics = challenge_metrics["hard_negative"]
    assert isinstance(hard_negative_metrics, dict)
    assert hard_negative_metrics["mrr_at_5"] == 0.5
    assert result["hard_negative_outrank_rate"] == 1.0
    assert result["hard_negative_pairs"] == 1


def test_recall_rejects_empty_relevance_labels() -> None:
    with pytest.raises(ValueError, match="at least one relevant"):
        recall_at_k(["doc"], frozenset(), 5)


def test_nearest_rank_percentiles_are_deterministic() -> None:
    assert percentile([4, 1, 3, 2], 50) == 2
    assert percentile([4, 1, 3, 2], 95) == 4


def test_report_metrics_include_language_recall_and_latency_percentiles() -> None:
    queries = (
        Query(
            "en-1",
            "scenario-1",
            "en",
            "one",
            "product_grounding",
            "lexical",
            {"doc-1": 3},
            frozenset(),
        ),
        Query(
            "ar-1",
            "scenario-2",
            "ar",
            "اثنان",
            "policy_grounding",
            "paraphrase",
            {"doc-2": 3},
            frozenset(),
        ),
    )
    result = evaluate_mode(
        queries,
        {"en-1": ["doc-1"], "ar-1": ["missing"]},
        [1.0, 2.0, 3.0, 4.0],
        [2.0, 3.0, 4.0, 5.0],
        includes_query_embedding=True,
    )

    assert result["recall_at_5"] == 0.5
    metrics_by_language = result["metrics_by_language"]
    assert isinstance(metrics_by_language, dict)
    assert metrics_by_language["en"]["recall_at_5"] == 1.0
    assert metrics_by_language["ar"]["recall_at_5"] == 0.0
    assert result["mrr_at_5"] == 0.5
    assert result["ndcg_at_5"] == 0.5
    assert result["search_latency_ms"] == {"p50": 2.0, "p95": 4.0, "samples": 4}
    assert result["end_to_end_latency_ms"] == {
        "p50": 3.0,
        "p95": 5.0,
        "samples": 4,
        "includes_query_embedding": True,
    }


def test_docker_stats_are_scoped_parseable_container_memory() -> None:
    result = parse_docker_stats(
        "s3-retrieval-benchmark-pgvector-1\t64.5MiB / 1GiB\n"
        "s3-retrieval-benchmark-qdrant-1\t0.25GiB / 2GiB\n"
    )

    assert result == {
        "s3-retrieval-benchmark-pgvector-1": 64.5,
        "s3-retrieval-benchmark-qdrant-1": 256.0,
    }


def test_memory_monitor_adds_both_container_peaks_to_report(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report = json.loads(
        Path("evals/retrieval/results/s3-benchmark.json").read_text(encoding="utf-8")
    )
    report.pop("container_memory_max_mib", None)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    maximums = {name: 64.0 for name in CONTAINER_NAMES}

    update_report_memory(report_path, maximums)

    assert (
        json.loads(report_path.read_text(encoding="utf-8"))["container_memory_max_mib"] == maximums
    )
    summary = markdown_summary_path(report_path).read_text(encoding="utf-8")
    assert "Container peak memory:" in summary
    assert "64.0 MiB" in summary


def test_memory_monitor_rejects_missing_container_measurement(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"benchmark": "s3"}), encoding="utf-8")

    with pytest.raises(ValueError, match="Expected memory for both benchmark containers"):
        update_report_memory(report_path, {CONTAINER_NAMES[0]: 64.0})

    assert "container_memory_max_mib" not in json.loads(report_path.read_text(encoding="utf-8"))


def test_docker_stats_reject_unknown_memory_units() -> None:
    with pytest.raises(ValueError, match="Unsupported Docker memory"):
        parse_memory_mib("64 MB")


def test_report_does_not_apply_provisional_recall_target_as_gate() -> None:
    assert RELEASE_GATE is None


def test_default_checks_do_not_start_benchmark_services() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    ci_target = makefile.split("\nci:\n", maxsplit=1)[1].split("\nverify-uv:\n", maxsplit=1)[0]

    assert "docker compose" not in ci_target
    assert "run_s3_benchmark.sh" not in ci_target


def test_launcher_uses_caller_identity_and_run_scoped_report() -> None:
    compose = Path("deploy/s3-benchmark-compose.yml").read_text(encoding="utf-8")
    launcher = Path("scripts/run_s3_benchmark.sh").read_text(encoding="utf-8")

    compose_user = (
        'user: "${S3_BENCHMARK_UID:?Set S3_BENCHMARK_UID}:'
        '${S3_BENCHMARK_GID:?Set S3_BENCHMARK_GID}"'
    )
    assert compose_user in compose
    assert 'export S3_BENCHMARK_UID="$(id -u)"' in launcher
    assert 'export S3_BENCHMARK_GID="$(id -g)"' in launcher
    assert 'mktemp "$report_dir/.s3-benchmark.XXXXXX"' in launcher
    assert '--report "$run_report_path"' in launcher
    assert 'run_markdown_path="${run_report_path}.md"' in launcher
    assert '[[ ! -s "$run_markdown_path" ]]' in launcher
    assert '--publish-staged-report "$run_report_path" --report "$final_report_path"' in launcher
    assert "mv -f --" not in launcher
