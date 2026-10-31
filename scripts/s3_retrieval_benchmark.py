from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Protocol, Sequence, TextIO
from urllib.request import urlopen

DATASET_PATH = Path("evals/retrieval/s3_dataset.json")
MODEL_NAME = "intfloat/multilingual-e5-large"
MODEL_DIMENSIONS = 1024
MODEL_SOURCE_REPO = "Qdrant/multilingual-e5-large-onnx"
MODEL_REVISION = "ac6781cd1cf88b8306a536d7c9d18a5bd57cc14b"
MODEL_WEIGHT_SHA256 = "0cf1883fee81c63819a44e2ba0efa51d4043d9759685a4ebebbde97e0623d15c"
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
COLLECTION_NAME = "s3_retrieval_benchmark"
POSTGRES_DSN = os.environ.get(
    "S3_BENCHMARK_POSTGRES_DSN", "postgresql://s3bench@pgvector:5432/s3bench"
)
QDRANT_URL = os.environ.get("S3_BENCHMARK_QDRANT_URL", "http://qdrant:6333")
WARMUP_ROUNDS = 2
MEASUREMENT_REPETITIONS = 5
CANDIDATE_DEPTH = 20
RRF_K = 60
RECALL_K = 5
WEIGHTED_RRF_CONFIGS = {
    "50_50": (0.5, 0.5),
    "70_30": (0.7, 0.3),
    "80_20": (0.8, 0.2),
}
SINGLE_ROUNDTRIP_MODES = {
    "pgvector": ("hybrid_sql_cte",),
    "qdrant": ("hybrid_server_fusion_default", "hybrid_server_rrf_parity"),
}
LANGUAGE_CONFIGS = {"en": "english", "ar": "arabic"}
LEXICAL_METHODS = {
    "pgvector": {
        "english": "PostgreSQL english configuration with ts_rank_cd",
        "arabic": "PostgreSQL arabic configuration with ts_rank_cd",
    },
    "qdrant": "native BM25 full-text index; word tokenizer; lowercase; scoring enabled",
}
QDRANT_CAPABILITY_SUMMARY = {
    "text_query": (
        "native BM25 over a full-text payload index; any-term retrieval; "
        "TextQuery has no score-threshold or phrase field"
    ),
    "score_threshold": (
        "supported as a query_points/Prefetch parameter; tested separately, "
        "not applied because score scales are backend-specific"
    ),
    "strict_lexical": "MatchText filter over the full query acts conjunctively",
    "phrase_matching": (
        "TextIndexParams.phrase_matching and MatchPhrase are supported when "
        "phrase matching is enabled on the index"
    ),
    "server_fusion": (
        "Prefetch accepts TextQuery; FusionQuery(RRF) and RrfQuery with k and weights are supported"
    ),
    "native_vs_parity_note": (
        "FusionQuery native defaults and weighted client RRF are distinct; "
        "the RrfQuery path is evaluated separately for ranking parity"
    ),
}
RELEASE_GATE = None
HEARTBEAT_SECONDS = 30.0
PROGRESS_INTERVAL_SECONDS = 2.0
EXECUTION_STRATEGY = {
    "mode": "sequential",
    "parallel_comparison": "not relevant to the S3 acceptance metrics",
    "rationale": (
        "S3 measures isolated per-query ranking and latency, not throughput. "
        "Parallel backend or query execution would add shared CPU and database contention "
        "and change the latency being compared. The single-threaded embedding stage also "
        "remains serial; a separate load test is needed if throughput becomes a requirement."
    ),
}


class StageFailure(RuntimeError):
    def __init__(self, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(f"{stage}: {type(cause).__name__}: {cause}")


class ProgressReporter:
    def __init__(
        self,
        *,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        progress_interval_seconds: float = PROGRESS_INTERVAL_SECONDS,
        output: TextIO | None = None,
    ) -> None:
        if heartbeat_seconds <= 0 or progress_interval_seconds < 0:
            raise ValueError("Heartbeat must be positive and the progress interval non-negative")
        self.heartbeat_seconds = heartbeat_seconds
        self.progress_interval_seconds = progress_interval_seconds
        self.output = output or sys.stderr
        self._write_lock = threading.Lock()

    @contextmanager
    def stage(
        self, name: str, *, total: int | None = None, unit: str | None = None
    ) -> Iterator[Callable[[int], None]]:
        if total is not None and (total < 0 or not unit):
            raise ValueError("Progress totals require a non-negative total and a unit")

        started = time.monotonic()
        last_output = [started]
        completed = [0]

        def write(status: str, error_type: str | None = None) -> None:
            now = time.monotonic()
            fields = (
                f" completed={completed[0]} total={total} unit={unit}" if total is not None else ""
            )
            error = f" error_type={error_type}" if error_type else ""
            print(
                f"progress stage={name} status={status}{fields}"
                f"{error} elapsed_seconds={now - started:.1f}",
                file=self.output,
                flush=True,
            )
            last_output[0] = now

        def heartbeat() -> None:
            while not stop_event.wait(self.heartbeat_seconds):
                with self._write_lock:
                    write("heartbeat")

        stop_event = threading.Event()
        with self._write_lock:
            write("started")
        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()

        def advance(amount: int = 1) -> None:
            with self._write_lock:
                completed[0] += amount
                if total is not None and completed[0] > total:
                    raise ValueError(f"Progress for {name} exceeded its declared total")
                now = time.monotonic()
                if completed[0] == total or now - last_output[0] >= self.progress_interval_seconds:
                    write("progress")

        try:
            yield advance
        except StageFailure:
            raise
        except Exception as error:
            with self._write_lock:
                write("failed", type(error).__name__)
            raise StageFailure(name, error) from error
        else:
            with self._write_lock:
                write("completed")
        finally:
            stop_event.set()
            heartbeat_thread.join()


@dataclass(frozen=True)
class Document:
    id: str
    kind: str
    title_en: str
    title_ar: str
    text_en: str
    text_ar: str
    origin: str
    source_record_id: int | None = None
    source_record_type: str | None = None
    category_en: str | None = None


@dataclass(frozen=True)
class Query:
    id: str
    scenario_id: str
    language: str
    text: str
    task: str
    challenge_type: str
    relevance_grades: dict[str, int]
    hard_negative_ids: frozenset[str]

    @property
    def relevant_ids(self) -> frozenset[str]:
        return frozenset(
            document_id for document_id, grade in self.relevance_grades.items() if grade >= 2
        )


@dataclass(frozen=True)
class Dataset:
    version: str
    review_status: str
    corpus_sha256: str
    documents: tuple[Document, ...]
    queries: tuple[Query, ...]


class Retriever(Protocol):
    name: str

    def search_dense(self, vector: Sequence[float], language: str, limit: int) -> list[str]: ...

    def search_lexical(
        self, text: str, language: str, limit: int, *, strict: bool = False
    ) -> list[str]: ...

    def search_hybrid_single_roundtrip(
        self,
        vector: Sequence[float],
        text: str,
        language: str,
        limit: int,
        mode: str,
    ) -> list[str]: ...


def load_dataset(path: Path = DATASET_PATH) -> Dataset:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Dataset must be a JSON object")
    if raw.get("source") != (
        "one-time snapshot of synthetic Odoo demo product.template records "
        "(consu/combo) plus synthetic policy text; no customer/cart/order data; "
        "benchmark runtime has no Odoo connection"
    ):
        raise ValueError("Dataset source must identify its synthetic snapshot and exclusions")
    version = raw.get("dataset_version")
    if not isinstance(version, str) or not version:
        raise ValueError("Dataset must declare a version")
    review_status = raw.get("review_status")
    if review_status not in {"draft_pending_owner_review", "approved"}:
        raise ValueError("Dataset review_status must be draft_pending_owner_review or approved")

    raw_documents = raw.get("documents")
    raw_scenarios = raw.get("scenarios")
    source_snapshot = raw.get("source_snapshot")
    if not isinstance(raw_documents, list) or not isinstance(raw_scenarios, list):
        raise ValueError("Dataset must contain document and scenario lists")
    if not isinstance(source_snapshot, dict):
        raise ValueError("Dataset must describe the frozen source snapshot")
    if set(source_snapshot) != {
        "model",
        "snapshot_date",
        "total_template_count",
        "included_template_count",
        "included_type_counts",
        "excluded_service_count",
    } or not isinstance(source_snapshot.get("snapshot_date"), str):
        raise ValueError("Dataset source snapshot metadata is invalid")

    included_type_counts = source_snapshot.get("included_type_counts")
    if (
        source_snapshot.get("model") != "product.template"
        or source_snapshot.get("total_template_count") != 39
        or source_snapshot.get("included_template_count") != 29
        or source_snapshot.get("excluded_service_count") != 10
        or included_type_counts != {"consu": 28, "combo": 1}
    ):
        raise ValueError("Dataset source snapshot does not match the reviewed S1 catalog")

    documents: list[Document] = []
    document_ids: set[str] = set()
    source_record_ids: set[int] = set()
    source_types: dict[str, int] = {}
    for row in raw_documents:
        if not isinstance(row, dict) or row.get("synthetic") is not True:
            raise ValueError("Every corpus document must be explicitly synthetic")
        common_fields = {
            "id",
            "kind",
            "synthetic",
            "origin",
            "title_en",
            "title_ar",
            "text_en",
            "text_ar",
        }
        kind = _required_string(row, "kind")
        if kind == "product":
            allowed = common_fields | {
                "source_record_id",
                "source_record_type",
                "category_en",
            }
        elif kind == "policy":
            allowed = common_fields
        else:
            raise ValueError(f"Unsupported document kind for {_required_string(row, 'id')}")
        if set(row) != allowed:
            raise ValueError("Corpus document fields do not match the approved snapshot schema")
        document = Document(
            id=_required_string(row, "id"),
            kind=kind,
            title_en=_required_string(row, "title_en"),
            title_ar=_required_string(row, "title_ar"),
            text_en=_required_string(row, "text_en"),
            text_ar=_required_string(row, "text_ar"),
            origin=_required_string(row, "origin"),
            source_record_id=row.get("source_record_id")
            if type(row.get("source_record_id")) is int
            else None,
            source_record_type=_required_string(row, "source_record_type")
            if "source_record_type" in row
            else None,
            category_en=_required_string(row, "category_en") if "category_en" in row else None,
        )
        if document.id in document_ids:
            raise ValueError(f"Duplicate document id: {document.id}")
        if document.kind == "product":
            if (
                document.origin != "odoo_demo_snapshot"
                or document.source_record_id is None
                or document.source_record_id <= 0
                or document.source_record_type not in {"consu", "combo"}
                or document.category_en is None
            ):
                raise ValueError(f"Invalid Odoo snapshot metadata for {document.id}")
            if document.source_record_id in source_record_ids:
                raise ValueError(f"Duplicate Odoo source record ID: {document.source_record_id}")
            source_record_ids.add(document.source_record_id)
            source_types[document.source_record_type] = (
                source_types.get(document.source_record_type, 0) + 1
            )
        elif document.origin != "synthetic_policy":
            raise ValueError(f"Invalid synthetic policy provenance for {document.id}")
        document_ids.add(document.id)
        documents.append(document)
    if (
        len(documents) != 34
        or sum(document.kind == "product" for document in documents) != 29
        or sum(document.kind == "policy" for document in documents) != 5
        or source_types != {"consu": 28, "combo": 1}
    ):
        raise ValueError("Dataset must contain 29 catalog products and 5 synthetic policies")

    canonical_documents = json.dumps(
        raw_documents, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    corpus_sha256 = hashlib.sha256(canonical_documents).hexdigest()
    if raw.get("corpus_sha256") != corpus_sha256:
        raise ValueError("Dataset corpus_sha256 does not match the frozen documents")

    queries: list[Query] = []
    query_ids: set[str] = set()
    scenario_ids: set[str] = set()
    task_challenge_counts: dict[tuple[str, str], int] = {}
    documents_by_id = {document.id: document for document in documents}
    for row in raw_scenarios:
        if not isinstance(row, dict):
            raise ValueError("Every scenario must be a JSON object")
        expected_fields = {
            "id",
            "task",
            "challenge_type",
            "text_en",
            "text_ar",
            "qrels",
            "hard_negative_ids",
            "rationale",
        }
        if set(row) != expected_fields:
            raise ValueError("Scenario fields do not match the bilingual qrels schema")
        scenario_id = _required_string(row, "id")
        task = _required_string(row, "task")
        challenge_type = _required_string(row, "challenge_type")
        if scenario_id in scenario_ids:
            raise ValueError(f"Duplicate scenario ID: {scenario_id}")
        if task not in {"product_grounding", "policy_grounding"}:
            raise ValueError(f"Unsupported task for scenario {scenario_id}: {task}")
        if challenge_type not in {"lexical", "paraphrase", "hard_negative"}:
            raise ValueError(
                f"Unsupported challenge type for scenario {scenario_id}: {challenge_type}"
            )
        scenario_ids.add(scenario_id)

        qrels = row.get("qrels")
        if (
            not isinstance(qrels, dict)
            or set(qrels) != {"default_grade", "overrides"}
            or type(qrels.get("default_grade")) is not int
            or qrels.get("default_grade") != 0
            or not isinstance(qrels.get("overrides"), dict)
        ):
            raise ValueError(f"Scenario {scenario_id} must declare complete default-zero qrels")
        relevance_grades: dict[str, int] = {}
        for document_id, grade in qrels["overrides"].items():
            if document_id not in document_ids:
                raise ValueError(
                    f"Scenario {scenario_id} references unknown document {document_id}"
                )
            if type(grade) is not int or not 0 <= grade <= 3:
                raise ValueError(f"Invalid relevance grade for {scenario_id}/{document_id}")
            relevance_grades[document_id] = grade
        if not any(grade >= 2 for grade in relevance_grades.values()):
            raise ValueError(f"Scenario {scenario_id} must have a relevant qrel")

        hard_negatives = row.get("hard_negative_ids")
        if (
            not isinstance(hard_negatives, list)
            or not all(isinstance(item, str) and item in document_ids for item in hard_negatives)
            or len(set(hard_negatives)) != len(hard_negatives)
        ):
            raise ValueError(f"Invalid hard-negative IDs for scenario {scenario_id}")
        if challenge_type == "hard_negative" and not hard_negatives:
            raise ValueError(f"Hard-negative scenario {scenario_id} needs an in-corpus negative")
        if challenge_type != "hard_negative" and hard_negatives:
            raise ValueError(
                f"Non-contrastive scenario {scenario_id} cannot declare hard negatives"
            )
        if any(relevance_grades.get(item, 0) != 0 for item in hard_negatives):
            raise ValueError(f"Hard-negative document must have relevance grade 0 in {scenario_id}")

        task_challenge = (task, challenge_type)
        task_challenge_counts[task_challenge] = task_challenge_counts.get(task_challenge, 0) + 1
        for language in ("en", "ar"):
            query_id = f"{scenario_id}-{language}"
            if query_id in query_ids:
                raise ValueError(f"Duplicate query ID: {query_id}")
            query_text = _required_string(row, f"text_{language}")
            relevant_ids = {
                document_id for document_id, grade in relevance_grades.items() if grade >= 2
            }
            if challenge_type != "lexical" and any(
                _contains_title_phrase(
                    query_text, getattr(documents_by_id[item], f"title_{language}")
                )
                for item in relevant_ids
            ):
                raise ValueError(f"Non-lexical scenario {scenario_id} copies a target title")
            if challenge_type == "hard_negative" and any(
                _contains_title_phrase(
                    query_text, getattr(documents_by_id[item], f"title_{language}")
                )
                for item in hard_negatives
            ):
                raise ValueError(f"Hard-negative scenario {scenario_id} copies a distractor title")
            query_ids.add(query_id)
            queries.append(
                Query(
                    id=query_id,
                    scenario_id=scenario_id,
                    language=language,
                    text=query_text,
                    task=task,
                    challenge_type=challenge_type,
                    relevance_grades=relevance_grades,
                    hard_negative_ids=frozenset(hard_negatives),
                )
            )

    expected_counts = {
        ("product_grounding", "lexical"): 9,
        ("policy_grounding", "lexical"): 3,
        ("product_grounding", "paraphrase"): 13,
        ("policy_grounding", "paraphrase"): 5,
        ("product_grounding", "hard_negative"): 8,
        ("policy_grounding", "hard_negative"): 2,
    }
    if len(scenario_ids) != 40 or task_challenge_counts != expected_counts:
        raise ValueError("Dataset scenarios do not match the approved task/challenge quotas")

    return Dataset(version, review_status, corpus_sha256, tuple(documents), tuple(queries))


def _required_string(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Dataset field {key!r} must be a non-empty string")
    return value


def _normalized_words(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", text.casefold(), flags=re.UNICODE))


def _contains_title_phrase(text: str, title: str) -> bool:
    normalized_text = " ".join(_normalized_words(text))
    normalized_title = " ".join(_normalized_words(title))
    return bool(normalized_title and f" {normalized_title} " in f" {normalized_text} ")


def query_overlap_diagnostics(
    dataset: Dataset,
) -> dict[str, dict[str, dict[str, float | int]]]:
    documents_by_id = {document.id: document for document in dataset.documents}
    diagnostics: dict[str, dict[str, dict[str, float | int]]] = {}
    for language in ("en", "ar"):
        language_queries = [query for query in dataset.queries if query.language == language]
        diagnostics[language] = {}
        for challenge_type in ("lexical", "paraphrase", "hard_negative"):
            selected = [
                query for query in language_queries if query.challenge_type == challenge_type
            ]
            ratios: list[float] = []
            exact_title_hits = 0
            for query in selected:
                target_documents = [
                    documents_by_id[document_id] for document_id in query.relevant_ids
                ]
                query_words = set(_normalized_words(query.text))
                target_ratios = [
                    len(query_words & set(_normalized_words(getattr(document, f"text_{language}"))))
                    / max(1, len(set(_normalized_words(getattr(document, f"text_{language}")))))
                    for document in target_documents
                ]
                ratios.append(statistics.fmean(target_ratios))
                if any(
                    _contains_title_phrase(query.text, getattr(document, f"title_{language}"))
                    for document in target_documents
                ):
                    exact_title_hits += 1
            diagnostics[language][challenge_type] = {
                "query_count": len(selected),
                "mean_target_document_token_overlap": round(statistics.fmean(ratios), 4),
                "queries_containing_full_target_title": exact_title_hits,
            }
    return diagnostics


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]],
    *,
    k: int = RRF_K,
    limit: int = CANDIDATE_DEPTH,
    weights: Sequence[float] | None = None,
) -> list[str]:
    if k <= 0 or limit <= 0:
        raise ValueError("RRF k and result limit must be positive")
    if weights is None:
        weights = (1.0 / len(rankings),) * len(rankings) if rankings else ()
    if len(weights) != len(rankings):
        raise ValueError("RRF needs exactly one weight per ranking")
    if any(not math.isfinite(weight) or weight <= 0 for weight in weights):
        raise ValueError("RRF weights must be finite and positive")
    if not math.isclose(sum(weights), 1.0):
        raise ValueError("Weighted RRF weights must sum to 1")
    scores: dict[str, float] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        seen: set[str] = set()
        for zero_based_rank, identifier in enumerate(ranking):
            if identifier in seen:
                continue
            seen.add(identifier)
            scores[identifier] = scores.get(identifier, 0.0) + weight / (k + zero_based_rank + 1)
    return [
        identifier
        for identifier, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def recall_at_k(ranked_ids: Sequence[str], relevant_ids: frozenset[str], k: int) -> float:
    if not relevant_ids:
        raise ValueError("Recall requires at least one relevant document")
    if k <= 0:
        raise ValueError("Recall k must be positive")
    return len(set(ranked_ids[:k]) & relevant_ids) / len(relevant_ids)


def mrr_at_k(ranked_ids: Sequence[str], relevant_ids: frozenset[str], k: int) -> float:
    if not relevant_ids:
        raise ValueError("MRR requires at least one relevant document")
    if k <= 0:
        raise ValueError("MRR k must be positive")
    for rank, document_id in enumerate(ranked_ids[:k], start=1):
        if document_id in relevant_ids:
            return 1 / rank
    return 0.0


def ndcg_at_k(ranked_ids: Sequence[str], relevance_grades: dict[str, int], k: int) -> float:
    if not any(grade >= 2 for grade in relevance_grades.values()):
        raise ValueError("nDCG requires at least one relevant document")
    if k <= 0:
        raise ValueError("nDCG k must be positive")

    def gain(grade: int) -> int:
        return 2**grade - 1

    dcg = sum(
        gain(relevance_grades.get(document_id, 0)) / math.log2(rank + 1)
        for rank, document_id in enumerate(ranked_ids[:k], start=1)
    )
    ideal_grades = sorted(relevance_grades.values(), reverse=True)[:k]
    ideal_dcg = sum(gain(grade) / math.log2(rank + 1) for rank, grade in enumerate(ideal_grades, 1))
    return dcg / ideal_dcg


def hard_negative_outrank_count(
    ranked_ids: Sequence[str],
    relevant_ids: frozenset[str],
    hard_negative_ids: frozenset[str],
) -> tuple[int, int]:
    positions = {document_id: rank for rank, document_id in enumerate(ranked_ids)}
    relevant_positions = [positions[item] for item in relevant_ids if item in positions]
    first_relevant = min(relevant_positions, default=None)
    outranked = sum(
        1
        for item in hard_negative_ids
        if item in positions and (first_relevant is None or positions[item] < first_relevant)
    )
    return outranked, len(hard_negative_ids)


def percentile(values: Sequence[float], percent: int) -> float:
    if not values:
        raise ValueError("Cannot calculate a percentile from no measurements")
    if not 0 <= percent <= 100:
        raise ValueError("Percentile must be between 0 and 100")
    ordered = sorted(values)
    index = max(0, math.ceil(percent / 100 * len(ordered)) - 1)
    return ordered[index]


def evaluate_mode(
    queries: Sequence[Query],
    ranked_results: dict[str, list[str]],
    latency_ms: Sequence[float],
    end_to_end_latency_ms: Sequence[float],
    *,
    includes_query_embedding: bool,
) -> dict[str, object]:
    def quality_metrics(selected_queries: Sequence[Query]) -> dict[str, float | int]:
        return {
            "query_count": len(selected_queries),
            "recall_at_5": round(
                statistics.fmean(
                    recall_at_k(ranked_results[query.id], query.relevant_ids, RECALL_K)
                    for query in selected_queries
                ),
                4,
            ),
            "mrr_at_5": round(
                statistics.fmean(
                    mrr_at_k(ranked_results[query.id], query.relevant_ids, RECALL_K)
                    for query in selected_queries
                ),
                4,
            ),
            "ndcg_at_5": round(
                statistics.fmean(
                    ndcg_at_k(ranked_results[query.id], query.relevance_grades, RECALL_K)
                    for query in selected_queries
                ),
                4,
            ),
        }

    if not queries:
        raise ValueError("Cannot evaluate an empty query set")
    hard_negative_queries = [query for query in queries if query.hard_negative_ids]
    outranked_count = 0
    hard_negative_count = 0
    for query in hard_negative_queries:
        outranked, count = hard_negative_outrank_count(
            ranked_results[query.id], query.relevant_ids, query.hard_negative_ids
        )
        outranked_count += outranked
        hard_negative_count += count

    return {
        "query_count": len(queries),
        **quality_metrics(queries),
        "metrics_by_language": {
            language: quality_metrics([query for query in queries if query.language == language])
            for language in ("en", "ar")
        },
        "metrics_by_task": {
            task: quality_metrics(selected)
            for task in ("product_grounding", "policy_grounding")
            if (selected := [query for query in queries if query.task == task])
        },
        "metrics_by_challenge_type": {
            challenge_type: quality_metrics(selected)
            for challenge_type in ("lexical", "paraphrase", "hard_negative")
            if (selected := [query for query in queries if query.challenge_type == challenge_type])
        },
        "hard_negative_outrank_rate": (
            round(outranked_count / hard_negative_count, 4) if hard_negative_count else None
        ),
        "hard_negative_pairs": hard_negative_count,
        "hard_negative_queries": len(hard_negative_queries),
        "search_latency_ms": {
            "p50": round(percentile(latency_ms, 50), 3),
            "p95": round(percentile(latency_ms, 95), 3),
            "samples": len(latency_ms),
        },
        "end_to_end_latency_ms": {
            "p50": round(percentile(end_to_end_latency_ms, 50), 3),
            "p95": round(percentile(end_to_end_latency_ms, 95), 3),
            "samples": len(end_to_end_latency_ms),
            "includes_query_embedding": includes_query_embedding,
        },
    }


def parse_memory_mib(value: str) -> float:
    match = re.fullmatch(r"\s*([\d.]+)\s*(B|KiB|MiB|GiB|TiB)\s*", value)
    if match is None:
        raise ValueError(f"Unsupported Docker memory value: {value!r}")
    amount = float(match.group(1))
    scale = {"B": 1 / 1024**2, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "TiB": 1024**2}
    return amount * scale[match.group(2)]


def parse_docker_stats(output: str) -> dict[str, float]:
    usage: dict[str, float] = {}
    for line in output.splitlines():
        name, separator, memory = line.partition("\t")
        if not separator or "/" not in memory:
            raise ValueError(f"Invalid Docker stats line: {line!r}")
        used, _, _ = memory.partition("/")
        usage[name] = parse_memory_mib(used)
    if not usage:
        raise ValueError("Docker stats returned no containers")
    return usage


class PostgresRetriever:
    name = "pgvector"

    def __init__(self) -> None:
        import psycopg  # type: ignore[import-not-found]

        self.connection = psycopg.connect(POSTGRES_DSN, connect_timeout=5)
        row = self.connection.execute("SELECT current_database(), version()").fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL did not return its database version")
        database, version = row
        if database != "s3bench":
            raise RuntimeError(f"Refusing unexpected PostgreSQL database: {database}")
        self.version = version
        self.connection.commit()
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self.connection.transaction():
            self.connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS s3_benchmark_documents (
                    id text PRIMARY KEY,
                    text_en text NOT NULL,
                    text_ar text NOT NULL,
                    embedding_en vector(1024) NOT NULL,
                    embedding_ar vector(1024) NOT NULL,
                    search_en tsvector NOT NULL,
                    search_ar tsvector NOT NULL
                )
                """
            )
            self.connection.execute("DELETE FROM s3_benchmark_documents")
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS s3_benchmark_embedding_en_hnsw "
                "ON s3_benchmark_documents USING hnsw (embedding_en vector_cosine_ops)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS s3_benchmark_embedding_ar_hnsw "
                "ON s3_benchmark_documents USING hnsw (embedding_ar vector_cosine_ops)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS s3_benchmark_search_en_gin "
                "ON s3_benchmark_documents USING gin (search_en)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS s3_benchmark_search_ar_gin "
                "ON s3_benchmark_documents USING gin (search_ar)"
            )

    def load(
        self, documents: Sequence[Document], vectors: dict[str, dict[str, list[float]]]
    ) -> None:
        with self.connection.transaction():
            for document in documents:
                self.connection.execute(
                    """
                    INSERT INTO s3_benchmark_documents
                    (id, text_en, text_ar, embedding_en, embedding_ar, search_en, search_ar)
                    VALUES (%s, %s, %s, %s::vector, %s::vector,
                        to_tsvector('english', %s), to_tsvector('arabic', %s))
                    """,
                    (
                        document.id,
                        document.text_en,
                        document.text_ar,
                        _vector_literal(vectors[document.id]["en"]),
                        _vector_literal(vectors[document.id]["ar"]),
                        document.text_en,
                        document.text_ar,
                    ),
                )
            self.connection.execute("ANALYZE s3_benchmark_documents")

    def search_dense(self, vector: Sequence[float], language: str, limit: int) -> list[str]:
        columns = {"en": "embedding_en", "ar": "embedding_ar"}
        column = columns[language]
        rows = self.connection.execute(
            f"SELECT id FROM s3_benchmark_documents ORDER BY {column} <=> %s::vector, id LIMIT %s",
            (_vector_literal(vector), limit),
        ).fetchall()
        return [row[0] for row in rows]

    def search_lexical(
        self, text: str, language: str, limit: int, *, strict: bool = False
    ) -> list[str]:
        column = "search_en" if language == "en" else "search_ar"
        config = LANGUAGE_CONFIGS[language]
        query_expression = (
            "plainto_tsquery(%s, %s)"
            if strict
            else "replace(plainto_tsquery(%s, %s)::text, ' & ', ' | ')::tsquery"
        )
        rows = self.connection.execute(
            f"""
            WITH query_terms AS (
                SELECT {query_expression} AS value
            )
            SELECT id
            FROM s3_benchmark_documents, query_terms
            WHERE {column} @@ query_terms.value
            ORDER BY ts_rank_cd({column}, query_terms.value) DESC, id
            LIMIT %s
            """,
            (config, text, limit),
        ).fetchall()
        return [row[0] for row in rows]

    def search_hybrid_single_roundtrip(
        self,
        vector: Sequence[float],
        text: str,
        language: str,
        limit: int,
        mode: str,
    ) -> list[str]:
        if mode != "hybrid_sql_cte":
            raise ValueError(f"Unsupported PostgreSQL single-roundtrip mode: {mode}")
        embedding_column = "embedding_en" if language == "en" else "embedding_ar"
        search_column = "search_en" if language == "en" else "search_ar"
        config = LANGUAGE_CONFIGS[language]
        query_expression = "replace(plainto_tsquery(%s, %s)::text, ' & ', ' | ')::tsquery"
        rows = self.connection.execute(
            f"""
            WITH query_terms AS (
                SELECT {query_expression} AS value
            ),
            dense_candidates AS (
                SELECT id, row_number() OVER (ORDER BY distance, id) - 1 AS rank
                FROM (
                    SELECT id, {embedding_column} <=> %s::vector AS distance
                    FROM s3_benchmark_documents
                    ORDER BY distance, id
                    LIMIT %s
                ) dense_top
            ),
            lexical_candidates AS (
                SELECT id, row_number() OVER (ORDER BY score DESC, id) - 1 AS rank
                FROM (
                    SELECT id, ts_rank_cd({search_column}, query_terms.value) AS score
                    FROM s3_benchmark_documents, query_terms
                    WHERE {search_column} @@ query_terms.value
                    ORDER BY score DESC, id
                    LIMIT %s
                ) lexical_top
            ),
            fused_scores AS (
                SELECT id, SUM(weight / (%s + rank + 1)) AS score
                FROM (
                    SELECT id, rank, %s::double precision AS weight
                    FROM dense_candidates
                    UNION ALL
                    SELECT id, rank, %s::double precision AS weight
                    FROM lexical_candidates
                ) candidates
                GROUP BY id
            )
            SELECT id
            FROM fused_scores
            ORDER BY score DESC, id
            LIMIT %s
            """,
            (
                config,
                text,
                _vector_literal(vector),
                CANDIDATE_DEPTH,
                CANDIDATE_DEPTH,
                RRF_K,
                WEIGHTED_RRF_CONFIGS["50_50"][0],
                WEIGHTED_RRF_CONFIGS["50_50"][1],
                limit,
            ),
        ).fetchall()
        return [row[0] for row in rows]

    def close(self) -> None:
        self.connection.close()


class QdrantRetriever:
    name = "qdrant"

    def __init__(self) -> None:
        from qdrant_client import QdrantClient  # type: ignore[import-not-found]

        self.client = QdrantClient(url=QDRANT_URL, timeout=10)
        with urlopen(f"{QDRANT_URL}/", timeout=5) as response:
            self.version_info = json.load(response)

    def load(
        self, documents: Sequence[Document], vectors: dict[str, dict[str, list[float]]]
    ) -> None:
        from qdrant_client import models  # type: ignore[import-not-found]

        if self.client.collection_exists(COLLECTION_NAME):
            self.client.delete_collection(COLLECTION_NAME)
        self.client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config={
                "dense_en": models.VectorParams(
                    size=MODEL_DIMENSIONS, distance=models.Distance.COSINE
                ),
                "dense_ar": models.VectorParams(
                    size=MODEL_DIMENSIONS, distance=models.Distance.COSINE
                ),
            },
        )
        for language in ("en", "ar"):
            self.client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name=f"text_{language}",
                field_schema=models.TextIndexParams(
                    type=models.TextIndexType.TEXT,
                    tokenizer=models.TokenizerType.WORD,
                    lowercase=True,
                    scoring=True,
                ),
                wait=True,
            )
        self.client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                models.PointStruct(
                    id=index,
                    vector={
                        "dense_en": vectors[document.id]["en"],
                        "dense_ar": vectors[document.id]["ar"],
                    },
                    payload={
                        "doc_id": document.id,
                        "text_en": document.text_en,
                        "text_ar": document.text_ar,
                    },
                )
                for index, document in enumerate(documents, start=1)
            ],
            wait=True,
        )

    def search_dense(self, vector: Sequence[float], language: str, limit: int) -> list[str]:
        result = self.client.query_points(
            collection_name=COLLECTION_NAME,
            query=list(vector),
            using=f"dense_{language}",
            limit=limit,
            with_payload=True,
        )
        document_ids = []
        for point in result.points:
            payload = point.payload
            if payload is None or not isinstance(payload.get("doc_id"), str):
                raise RuntimeError("Qdrant returned a point without a document ID")
            document_ids.append(payload["doc_id"])
        return document_ids

    def search_lexical(
        self, text: str, language: str, limit: int, *, strict: bool = False
    ) -> list[str]:
        from qdrant_client import models  # type: ignore[import-not-found]

        query_filter = (
            models.Filter(
                must=[
                    models.FieldCondition(key=f"text_{language}", match=models.MatchText(text=text))
                ]
            )
            if strict
            else None
        )
        result = self.client.query_points(
            collection_name=COLLECTION_NAME,
            query=models.TextQuery(text=text),
            using=f"text_{language}",
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        document_ids = []
        for point in result.points:
            payload = point.payload
            if payload is None or not isinstance(payload.get("doc_id"), str):
                raise RuntimeError("Qdrant returned a point without a document ID")
            document_ids.append(payload["doc_id"])
        return document_ids

    def search_hybrid_single_roundtrip(
        self,
        vector: Sequence[float],
        text: str,
        language: str,
        limit: int,
        mode: str,
    ) -> list[str]:
        from qdrant_client import models  # type: ignore[import-not-found]

        prefetch = [
            models.Prefetch(query=list(vector), using=f"dense_{language}", limit=CANDIDATE_DEPTH),
            models.Prefetch(
                query=models.TextQuery(text=text),
                using=f"text_{language}",
                limit=CANDIDATE_DEPTH,
            ),
        ]
        if mode == "hybrid_server_fusion_default":
            fusion_query = models.FusionQuery(fusion=models.Fusion.RRF)
        elif mode == "hybrid_server_rrf_parity":
            fusion_query = models.RrfQuery(rrf=models.Rrf(k=RRF_K + 1, weights=[1.0, 1.0]))
        else:
            raise ValueError(f"Unsupported Qdrant single-roundtrip mode: {mode}")
        result = self.client.query_points(
            collection_name=COLLECTION_NAME,
            prefetch=prefetch,
            query=fusion_query,
            limit=limit,
            with_payload=True,
        )
        document_ids = []
        for point in result.points:
            payload = point.payload
            if payload is None or not isinstance(payload.get("doc_id"), str):
                raise RuntimeError("Qdrant returned a point without a document ID")
            document_ids.append(payload["doc_id"])
        return document_ids

    def close(self) -> None:
        self.client.close()


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(format(value, ".9g") for value in values) + "]"


def _query_results(
    retriever: Retriever,
    query: Query,
    vector: Sequence[float],
    mode: str,
) -> list[str]:
    if mode == "dense":
        return retriever.search_dense(vector, query.language, CANDIDATE_DEPTH)
    if mode == "lexical":
        return retriever.search_lexical(query.text, query.language, CANDIDATE_DEPTH)
    if mode in SINGLE_ROUNDTRIP_MODES[retriever.name]:
        return retriever.search_hybrid_single_roundtrip(
            vector, query.text, query.language, CANDIDATE_DEPTH, mode
        )
    weights_by_mode = {
        "hybrid": WEIGHTED_RRF_CONFIGS["50_50"],
        "hybrid_rrf_70_30": WEIGHTED_RRF_CONFIGS["70_30"],
        "hybrid_rrf_80_20": WEIGHTED_RRF_CONFIGS["80_20"],
    }
    if mode in weights_by_mode or mode == "hybrid_strict_lexical_gate":
        dense = retriever.search_dense(vector, query.language, CANDIDATE_DEPTH)
        lexical = retriever.search_lexical(
            query.text,
            query.language,
            CANDIDATE_DEPTH,
            strict=mode == "hybrid_strict_lexical_gate",
        )
        weights = weights_by_mode.get(mode, WEIGHTED_RRF_CONFIGS["50_50"])
        return reciprocal_rank_fusion(
            (dense, lexical), k=RRF_K, limit=CANDIDATE_DEPTH, weights=weights
        )
    raise ValueError(f"Unsupported retrieval mode: {mode}")


def _run_mode(
    retriever: Retriever,
    dataset: Dataset,
    query_vectors: dict[str, list[float]],
    encode_query: Callable[[str], list[float]],
    mode: str,
    on_query_complete: Callable[[], None] | None = None,
) -> tuple[dict[str, object], dict[str, list[str]]]:
    ranked_results: dict[str, list[str]] = {}
    latency_ms: list[float] = []
    end_to_end_latency_ms: list[float] = []
    for query in dataset.queries:
        for _ in range(WARMUP_ROUNDS):
            if mode != "lexical":
                encode_query(query.text)
            _query_results(retriever, query, query_vectors[query.id], mode)
        for repetition in range(MEASUREMENT_REPETITIONS):
            end_to_end_started = time.perf_counter()
            vector = query_vectors[query.id] if mode == "lexical" else encode_query(query.text)
            search_started = time.perf_counter()
            results = _query_results(retriever, query, vector, mode)
            finished = time.perf_counter()
            latency_ms.append((finished - search_started) * 1000)
            end_to_end_latency_ms.append((finished - end_to_end_started) * 1000)
            if repetition == 0:
                ranked_results[query.id] = results
        if on_query_complete is not None:
            on_query_complete()
    return (
        evaluate_mode(
            dataset.queries,
            ranked_results,
            latency_ms,
            end_to_end_latency_ms,
            includes_query_embedding=mode != "lexical",
        ),
        ranked_results,
    )


def _ranking_comparison(
    reference: dict[str, list[str]], candidate: dict[str, list[str]]
) -> dict[str, float | int]:
    query_ids = sorted(set(reference) & set(candidate))
    if not query_ids:
        raise ValueError("Cannot compare rankings without shared query IDs")
    exact_matches = sum(reference[query_id] == candidate[query_id] for query_id in query_ids)
    overlaps = [
        len(set(reference[query_id]) & set(candidate[query_id]))
        / max(len(reference[query_id]), len(candidate[query_id]), 1)
        for query_id in query_ids
    ]
    return {
        "exact_order_match_queries": exact_matches,
        "query_count": len(query_ids),
        "exact_order_match_rate": round(exact_matches / len(query_ids), 4),
        "mean_candidate_set_overlap": round(statistics.fmean(overlaps), 4),
    }


def _hardware_info() -> dict[str, object]:
    memory_mib: float | None = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                memory_mib = round(int(line.split()[1]) / 1024, 1)
                break
    cpu_model: str | None = None
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                cpu_model = line.partition(":")[2].strip()
                break
    return {
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "cpu_model": cpu_model or platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "host_memory_mib": memory_mib,
    }


def _benchmark_process_max_rss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / 1024, 1) if platform.system() == "Linux" else round(peak / 1024**2, 1)


def _wait_for_services() -> None:
    import psycopg  # type: ignore[import-not-found]

    last_error: Exception | None = None
    for _ in range(30):
        try:
            with psycopg.connect(POSTGRES_DSN, connect_timeout=2):
                pass
            with urlopen(f"{QDRANT_URL}/", timeout=2) as response:
                json.load(response)
            return
        except (OSError, psycopg.Error) as error:
            last_error = error
        time.sleep(1)
    raise RuntimeError(f"Benchmark services did not become ready: {last_error}") from last_error


def _embed(
    dataset: Dataset,
) -> tuple[
    dict[str, dict[str, list[float]]],
    dict[str, list[float]],
    dict[str, float],
    Callable[[str], list[float]],
]:
    from fastembed import TextEmbedding  # type: ignore[import-not-found]
    from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

    model_path = snapshot_download(
        repo_id=MODEL_SOURCE_REPO,
        revision=MODEL_REVISION,
        token=False,
        allow_patterns=[
            "config.json",
            "model.onnx",
            "model.onnx_data",
            "sentencepiece.bpe.model",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer_config.json",
        ],
    )
    embedding = TextEmbedding(
        model_name=MODEL_NAME,
        threads=1,
        specific_model_path=model_path,
        local_files_only=True,
    )

    def encode_query(text: str) -> list[float]:
        encoded = list(embedding.embed([f"{QUERY_PREFIX}{text}"]))
        if len(encoded) != 1:
            raise ValueError("Embedding model returned an invalid query result")
        vector = [float(value) for value in encoded[0]]
        if len(vector) != MODEL_DIMENSIONS:
            raise ValueError(f"Unexpected query embedding dimension for {MODEL_NAME}")
        return vector

    documents: dict[str, dict[str, list[float]]] = {}
    passage_items = [
        (document.id, language, f"{PASSAGE_PREFIX}{getattr(document, f'text_{language}')}")
        for document in dataset.documents
        for language in ("en", "ar")
    ]
    passage_vectors = list(embedding.embed([text for _, _, text in passage_items]))
    for (document_id, language, _), passage_vector in zip(
        passage_items, passage_vectors, strict=True
    ):
        values = [float(value) for value in passage_vector]
        if len(values) != MODEL_DIMENSIONS:
            raise ValueError(f"Unexpected embedding dimension {len(values)} for {MODEL_NAME}")
        documents.setdefault(document_id, {})[language] = values

    query_vectors: dict[str, list[float]] = {}
    embedding_latencies: dict[str, float] = {}
    for _ in range(WARMUP_ROUNDS):
        encode_query(dataset.queries[0].text)
    for query in dataset.queries:
        samples: list[float] = []
        for _ in range(MEASUREMENT_REPETITIONS):
            started = time.perf_counter()
            query_vector = encode_query(query.text)
            samples.append((time.perf_counter() - started) * 1000)
        query_vectors[query.id] = query_vector
        embedding_latencies[query.id] = statistics.fmean(samples)
    return documents, query_vectors, embedding_latencies, encode_query


def _embedding_latency_report(latencies: dict[str, float]) -> dict[str, float | int]:
    values = list(latencies.values())
    return {
        "p50_ms": round(percentile(values, 50), 3),
        "p95_ms": round(percentile(values, 95), 3),
        "query_count": len(values),
        "samples_per_query": MEASUREMENT_REPETITIONS,
    }


def markdown_summary_path(report_path: Path) -> Path:
    if report_path.suffix == ".json":
        return report_path.with_suffix(".md")
    return report_path.with_name(f"{report_path.name}.md")


def _report_mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Benchmark report field {field} must be an object")
    return value


def _report_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Benchmark report field {field} must be a finite number")
    return float(value)


def render_markdown_summary(report: dict[str, object]) -> str:
    dataset_version = report.get("dataset_version")
    created_at = report.get("created_at_utc")
    if not isinstance(dataset_version, str) or not isinstance(created_at, str):
        raise ValueError("Benchmark report is missing its dataset version or creation time")

    embedding = _report_mapping(report.get("embedding"), "embedding")
    backends = _report_mapping(report.get("backend_versions"), "backend_versions")
    methodology = _report_mapping(report.get("methodology"), "methodology")
    execution_strategy_value = methodology.get("execution_strategy")
    if isinstance(execution_strategy_value, dict):
        execution_strategy = _report_mapping(
            execution_strategy_value, "methodology.execution_strategy"
        )
        execution_mode = execution_strategy.get("mode")
        execution_rationale = execution_strategy.get("rationale")
        if not isinstance(execution_mode, str) or not isinstance(execution_rationale, str):
            raise ValueError("Benchmark report has incomplete execution strategy metadata")
    else:
        execution_mode = "not recorded"
        execution_rationale = "This historical report predates execution-mode metadata."
    qdrant_capabilities = _report_mapping(
        methodology.get("qdrant_capabilities"), "methodology.qdrant_capabilities"
    )
    results = _report_mapping(report.get("results"), "results")
    quality_modes = {
        "pgvector": (
            ("hybrid", "Client-side RRF 50:50"),
            ("hybrid_rrf_70_30", "Client-side RRF 70:30"),
            ("hybrid_rrf_80_20", "Client-side RRF 80:20"),
            ("hybrid_strict_lexical_gate", "Strict lexical gate"),
            ("hybrid_sql_cte", "Single-roundtrip SQL CTE"),
        ),
        "qdrant": (
            ("hybrid", "Client-side RRF 50:50"),
            ("hybrid_rrf_70_30", "Client-side RRF 70:30"),
            ("hybrid_rrf_80_20", "Client-side RRF 80:20"),
            ("hybrid_strict_lexical_gate", "Strict lexical gate"),
            ("hybrid_server_fusion_default", "Single-request native FusionQuery RRF"),
            ("hybrid_server_rrf_parity", "Single-request RrfQuery parity parameters"),
        ),
    }

    rows: list[str] = []
    for backend, modes in quality_modes.items():
        backend_results = _report_mapping(results.get(backend), f"results.{backend}")
        for mode, label in modes:
            metrics = _report_mapping(backend_results.get(mode), f"results.{backend}.{mode}")
            search_latency = _report_mapping(
                metrics.get("search_latency_ms"),
                f"results.{backend}.{mode}.search_latency_ms",
            )
            end_to_end_latency = _report_mapping(
                metrics.get("end_to_end_latency_ms"),
                f"results.{backend}.{mode}.end_to_end_latency_ms",
            )
            comparison = metrics.get("client_ranking_comparison")
            comparison_text = "n/a"
            if comparison is not None:
                comparison_metrics = _report_mapping(
                    comparison, f"results.{backend}.{mode}.client_ranking_comparison"
                )
                exact = int(
                    _report_number(
                        comparison_metrics.get("exact_order_match_queries"),
                        f"results.{backend}.{mode}.client_ranking_comparison.exact_order_match_queries",
                    )
                )
                compared = int(
                    _report_number(
                        comparison_metrics.get("query_count"),
                        f"results.{backend}.{mode}.client_ranking_comparison.query_count",
                    )
                )
                overlap = _report_number(
                    comparison_metrics.get("mean_candidate_set_overlap"),
                    f"results.{backend}.{mode}.client_ranking_comparison.mean_candidate_set_overlap",
                )
                comparison_text = f"{exact}/{compared} exact; {overlap:.1%} overlap"

            hard_negative_rate = _report_number(
                metrics.get("hard_negative_outrank_rate"),
                f"results.{backend}.{mode}.hard_negative_outrank_rate",
            )
            search_p95 = _report_number(
                search_latency.get("p95"),
                f"results.{backend}.{mode}.search_latency_ms.p95",
            )
            end_to_end_p95 = _report_number(
                end_to_end_latency.get("p95"),
                f"results.{backend}.{mode}.end_to_end_latency_ms.p95",
            )
            rows.append(
                "| "
                + " | ".join(
                    (
                        backend,
                        label,
                        f"{_report_number(metrics.get('recall_at_5'), 'recall_at_5'):.4f}",
                        f"{_report_number(metrics.get('mrr_at_5'), 'mrr_at_5'):.4f}",
                        f"{_report_number(metrics.get('ndcg_at_5'), 'ndcg_at_5'):.4f}",
                        f"{hard_negative_rate:.1%}",
                        f"{search_p95:.3f}",
                        f"{end_to_end_p95:.3f}",
                        comparison_text,
                    )
                )
                + " |"
            )

    postgresql_version = backends.get("postgresql")
    if not isinstance(postgresql_version, str):
        raise ValueError("Benchmark report is missing the PostgreSQL version")
    postgres_match = re.search(r"PostgreSQL\s+(\d+(?:\.\d+)?)", postgresql_version)
    if postgres_match is None:
        raise ValueError("Benchmark report has an unrecognized PostgreSQL version")
    qdrant_version = _report_mapping(backends.get("qdrant"), "backend_versions.qdrant")
    hardware = _report_mapping(report.get("hardware"), "hardware")
    platform_name = hardware.get("platform")
    if not isinstance(platform_name, str):
        raise ValueError("Benchmark report is missing the platform")
    if "WSL2" in platform_name:
        platform_name = "Linux/WSL2"
    container_memory = report.get("container_memory_max_mib")
    if container_memory is None:
        memory_text = "Not yet collected by the host-side monitor."
    else:
        memory_values = _report_mapping(container_memory, "container_memory_max_mib")
        memory_text = "; ".join(
            f"{name}: {_report_number(value, f'container_memory_max_mib.{name}'):.1f} MiB"
            for name, value in sorted(memory_values.items())
        )
    runner_rss = _report_number(
        report.get("benchmark_process_max_rss_mib"), "benchmark_process_max_rss_mib"
    )
    query_count = int(_report_number(report.get("dataset_queries"), "dataset_queries"))
    scenario_count = int(_report_number(report.get("dataset_scenarios"), "dataset_scenarios"))
    document_count = int(_report_number(report.get("dataset_documents"), "dataset_documents"))
    qdrant_client_version = qdrant_capabilities.get("qdrant_client_version")
    if not isinstance(qdrant_client_version, str):
        raise ValueError("Benchmark report is missing the qdrant-client version")

    return "\n".join(
        (
            "# S3 bilingual retrieval benchmark: review summary",
            "",
            f"- Dataset: `{dataset_version}`; {document_count} documents.",
            (
                f"- Queries: {query_count} language queries over {scenario_count} paired "
                "scenarios; language pairs are not independent samples."
            ),
            f"- Run: {created_at}; command: `make benchmark-s3`.",
            (
                f"- Embedding: `{embedding.get('model')}` at revision "
                f"`{embedding.get('source_revision')}`."
            ),
            (
                f"- Backends: PostgreSQL {postgres_match.group(1)} with pgvector "
                f"{backends.get('pgvector_extension')}; Qdrant {qdrant_version.get('version')} "
                f"with qdrant-client {qdrant_client_version}."
            ),
            "",
            "## Results",
            "",
            (
                "Recall@5, MRR@5, and nDCG@5 are macro-averaged over the query set. "
                "Search latency is client-observed and excludes query embedding; "
                "end-to-end latency includes query embedding."
            ),
            "",
            (
                "| Backend | Variant | Recall@5 | MRR@5 | nDCG@5 | "
                "Hard-negative outrank | Search p95 (ms) | End-to-end p95 (ms) | "
                "Ranking vs client RRF 50:50 |"
            ),
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            *rows,
            "",
            (
                "Single-roundtrip ranking comparisons use exact ordered-list equality and "
                "mean top-20 candidate-set overlap. Similar candidate sets do not imply "
                "identical ranking formulas."
            ),
            "",
            "## Runtime and verified capabilities",
            "",
            f"- Hardware: {hardware.get('cpu_model')}; {platform_name}.",
            f"- Container peak memory: {memory_text}",
            f"- Benchmark process peak RSS: {runner_rss:.1f} MiB.",
            f"- Execution mode: {execution_mode}; {execution_rationale}",
            f"- Qdrant text query: {qdrant_capabilities.get('text_query')}",
            f"- Qdrant score threshold: {qdrant_capabilities.get('score_threshold')}",
            (
                f"- Qdrant strict matching and phrase support: "
                f"{qdrant_capabilities.get('strict_lexical')}; "
                f"{qdrant_capabilities.get('phrase_matching')}."
            ),
            f"- Qdrant server fusion: {qdrant_capabilities.get('server_fusion')}",
            "",
            "## Interpretation and limits",
            "",
            (
                "- This is a small local benchmark, not production-performance evidence or "
                "a statistically significant store decision."
            ),
            (
                "- PostgreSQL and Qdrant use different lexical tokenization and scoring; "
                "strict gates are operationally analogous, not token-identical."
            ),
            (
                "- Qdrant native fusion and client-side RRF are distinct. Review the measured "
                "ranking comparison rather than assuming parity."
            ),
            (
                "- The owner selected PostgreSQL FTS + pgvector for POC retrieval in "
                "[ADR-0015](../../../docs/adr/0015-postgresql-retrieval-store.md); these "
                "benchmark results remain local evidence, not production or statistical proof."
            ),
            (
                "- Detailed language, task, challenge, and per-query results are in the "
                "machine-readable report."
            ),
            "",
            "## Detailed artifacts",
            "",
            "- Machine-readable measurements: [s3-benchmark.json](./s3-benchmark.json).",
            "- Dataset and qrels: [s3_dataset.json](../s3_dataset.json).",
            (
                "- Procedure and limitations: "
                "[runbook](../../../docs/runbooks/s3-retrieval-benchmark.md)."
            ),
            "- Verified Qdrant capabilities: [F-025](../../../docs/facts-register.md#f-025).",
            "",
        )
    )


def write_markdown_summary(report_path: Path, report: dict[str, object]) -> Path:
    summary_path = markdown_summary_path(report_path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(render_markdown_summary(report), encoding="utf-8")
    return summary_path


def publish_report_pair(staged_report_path: Path, final_report_path: Path) -> None:
    staged_summary_path = markdown_summary_path(staged_report_path)
    final_summary_path = markdown_summary_path(final_report_path)
    if staged_report_path.resolve() == final_report_path.resolve():
        raise ValueError("Staged and published report paths must be different")
    if staged_report_path.parent.resolve() != final_report_path.parent.resolve():
        raise ValueError("Staged and published reports must be on the same filesystem directory")
    for path in (staged_report_path, staged_summary_path):
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Staged report artifact is missing or empty: {path}")

    final_report_path.parent.mkdir(parents=True, exist_ok=True)
    backup_paths: dict[Path, Path | None] = {}
    try:
        for path in (final_report_path, final_summary_path):
            if not path.exists():
                backup_paths[path] = None
                continue
            descriptor, backup_name = tempfile.mkstemp(
                prefix=f".{path.name}.backup-", dir=path.parent
            )
            os.close(descriptor)
            created_backup_path = Path(backup_name)
            backup_paths[path] = created_backup_path
            shutil.copy2(path, created_backup_path)
    except OSError:
        for backup_value in backup_paths.values():
            if backup_value is not None:
                backup_value.unlink(missing_ok=True)
        raise

    published_paths: list[Path] = []
    try:
        os.replace(staged_summary_path, final_summary_path)
        published_paths.append(final_summary_path)
        os.replace(staged_report_path, final_report_path)
        published_paths.append(final_report_path)
    except OSError as error:
        rollback_errors: list[str] = []
        for path in reversed(published_paths):
            target_backup = backup_paths[path]
            try:
                if target_backup is None:
                    path.unlink(missing_ok=True)
                else:
                    os.replace(target_backup, path)
                    backup_paths[path] = None
            except OSError as rollback_error:
                rollback_errors.append(f"{path}: {rollback_error}")
        if rollback_errors:
            retained = ", ".join(
                str(backup_value)
                for backup_value in backup_paths.values()
                if backup_value is not None
            )
            raise RuntimeError(
                f"Report publication failed: {error}; rollback failed: "
                f"{'; '.join(rollback_errors)}; retained backups: {retained}"
            ) from error
        for backup_value in backup_paths.values():
            if backup_value is not None:
                backup_value.unlink(missing_ok=True)
        raise
    else:
        cleanup_errors = []
        for backup_value in backup_paths.values():
            if backup_value is not None:
                try:
                    backup_value.unlink()
                except OSError as error:
                    cleanup_errors.append(f"{backup_value}: {error}")
        if cleanup_errors:
            print(
                "Warning: report pair was published, but backup cleanup failed: "
                + "; ".join(cleanup_errors),
                file=sys.stderr,
                flush=True,
            )


def require_approved_dataset(dataset: Dataset) -> None:
    if dataset.review_status != "approved":
        raise ValueError("Dataset is pending owner review; no benchmark may run")


def _cache_path() -> Path:
    if "S3_BENCHMARK_HF_CACHE_DIR" in os.environ:
        return Path(os.environ["S3_BENCHMARK_HF_CACHE_DIR"]).expanduser()
    if "HF_HUB_CACHE" in os.environ:
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    return home.expanduser() / "hub"


def _check_writable_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".s3-preflight-", dir=path):
        pass


def run_preflight(dataset_path: Path, report_path: Path) -> Dataset:
    reporter = ProgressReporter()
    with reporter.stage("preflight"):
        dataset = load_dataset(dataset_path)
        require_approved_dataset(dataset)

        required_files = (
            Path("deploy/s3-benchmark-compose.yml"),
            Path("deploy/s3-benchmark-runner.Dockerfile"),
            Path("pyproject.toml"),
            Path("uv.lock"),
            Path("scripts/s3_retrieval_benchmark.py"),
            Path("scripts/monitor_s3_benchmark_memory.py"),
        )
        missing_files = [str(path) for path in required_files if not path.is_file()]
        if missing_files:
            missing = ", ".join(missing_files)
            raise FileNotFoundError(f"Required benchmark files are missing: {missing}")

        docker = shutil.which("docker")
        if docker is None:
            raise FileNotFoundError("Required tool is missing: docker")
        if shutil.which("uv") is None:
            raise FileNotFoundError("Required tool is missing: uv")

        os.environ.setdefault("S3_BENCHMARK_UID", str(os.getuid()))
        os.environ.setdefault("S3_BENCHMARK_GID", str(os.getgid()))
        report_directory = report_path.expanduser().resolve().parent
        os.environ.setdefault("S3_BENCHMARK_REPORT_DIR", str(report_directory))
        os.environ.setdefault("S3_BENCHMARK_HF_CACHE_DIR", str(_cache_path().resolve()))

        compose = [
            docker,
            "compose",
            "--project-name",
            "s3-retrieval-benchmark",
            "--file",
            "deploy/s3-benchmark-compose.yml",
        ]
        try:
            subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            subprocess.run(
                [*compose, "config", "--quiet"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            subprocess.run(
                [sys.executable, "scripts/s3_retrieval_benchmark.py", "--help"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            subprocess.run(
                [sys.executable, "-m", "scripts.monitor_s3_benchmark_memory", "--help"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            existing_containers = subprocess.run(
                [*compose, "ps", "--quiet", "--all"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()
            existing_networks = subprocess.run(
                [
                    docker,
                    "network",
                    "ls",
                    "--quiet",
                    "--filter",
                    "label=com.docker.compose.project=s3-retrieval-benchmark",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or str(error)).strip()
            raise RuntimeError(f"Docker preflight failed: {detail}") from error
        if existing_containers or existing_networks:
            raise RuntimeError(
                "The S3 Compose project already has containers or networks; "
                "inspect and remove them before starting a new run"
            )

        report_directory = report_path.expanduser().resolve().parent
        cache_directory = Path(os.environ["S3_BENCHMARK_HF_CACHE_DIR"]).expanduser().resolve()
        _check_writable_directory(report_directory)
        _check_writable_directory(cache_directory)

    print(
        "Preflight passed: approved dataset and hash, required files, Docker/Compose "
        "configuration, idle project namespace, and writable report/cache directories. "
        "It does not start services, build images, download the model, or assess retrieval "
        "quality.",
        file=sys.stderr,
        flush=True,
    )
    return dataset


def run_smoke_check() -> None:
    reporter = ProgressReporter()
    with reporter.stage("smoke_service_readiness"):
        _wait_for_services()

    document = Document(
        id="preflight-smoke-001",
        kind="policy",
        title_en="Synthetic smoke record",
        title_ar="سجل اختبار اصطناعي",
        text_en="Synthetic preflight smoke record",
        text_ar="سجل اختبار تمهيدي اصطناعي",
        origin="synthetic_preflight",
    )
    vector = [0.1] * MODEL_DIMENSIONS
    vectors = {document.id: {"en": vector, "ar": vector}}
    postgres: PostgresRetriever | None = None
    qdrant: QdrantRetriever | None = None
    try:
        with reporter.stage("smoke_connect_backends"):
            postgres = PostgresRetriever()
            qdrant = QdrantRetriever()
        assert postgres is not None and qdrant is not None
        with reporter.stage("smoke_load_synthetic_record", total=2, unit="backends") as advance:
            postgres.load((document,), vectors)
            advance(1)
            qdrant.load((document,), vectors)
            advance(1)
        with reporter.stage("smoke_dense_and_lexical_queries", total=8, unit="checks") as advance:
            for retriever in (postgres, qdrant):
                for language, text in (("en", "preflight"), ("ar", "اختبار")):
                    dense_ids = retriever.search_dense(vector, language, 1)
                    lexical_ids = retriever.search_lexical(text, language, 1)
                    if dense_ids != [document.id] or lexical_ids != [document.id]:
                        raise RuntimeError(
                            f"{retriever.name} {language} smoke retrieval did not return "
                            "the synthetic record"
                        )
                    advance(2)
    finally:
        with reporter.stage("smoke_cleanup"):
            try:
                if postgres is not None:
                    postgres.close()
            finally:
                if qdrant is not None:
                    qdrant.close()

    print(
        "Smoke check passed: both backends loaded one synthetic record and returned it "
        "for dense and lexical English/Arabic queries. No embedding model was loaded; "
        "this does not validate model downloads, full-run behavior, or retrieval quality.",
        file=sys.stderr,
        flush=True,
    )


def run_benchmark(dataset: Dataset, report_path: Path) -> dict[str, object]:
    reporter = ProgressReporter()
    require_approved_dataset(dataset)
    with reporter.stage("service_readiness"):
        _wait_for_services()

    postgres: PostgresRetriever | None = None
    qdrant: QdrantRetriever | None = None
    try:
        with reporter.stage("connect_backends"):
            postgres = PostgresRetriever()
            qdrant = QdrantRetriever()
        assert postgres is not None and qdrant is not None
        with reporter.stage("embedding"):
            document_vectors, query_vectors, embedding_latencies, encode_query = _embed(dataset)
        with reporter.stage("load_documents", total=2, unit="backends") as advance:
            postgres.load(dataset.documents, document_vectors)
            advance(1)
            qdrant.load(dataset.documents, document_vectors)

        results: dict[str, dict[str, object]] = {}
        for retriever in (postgres, qdrant):
            modes = (
                "dense",
                "lexical",
                "hybrid",
                "hybrid_rrf_70_30",
                "hybrid_rrf_80_20",
                "hybrid_strict_lexical_gate",
                *SINGLE_ROUNDTRIP_MODES[retriever.name],
            )
            backend_results: dict[str, object] = {}
            baseline_rankings: dict[str, list[str]] | None = None
            for mode in modes:
                with reporter.stage(
                    f"retrieval_{retriever.name}_{mode}",
                    total=len(dataset.queries),
                    unit="queries",
                ) as advance:
                    mode_result, ranked_results = _run_mode(
                        retriever,
                        dataset,
                        query_vectors,
                        encode_query,
                        mode,
                        on_query_complete=lambda: advance(1),
                    )
                    if mode == "hybrid":
                        baseline_rankings = ranked_results
                    elif mode in SINGLE_ROUNDTRIP_MODES[retriever.name]:
                        if baseline_rankings is None:
                            raise RuntimeError("Client-side hybrid baseline must run first")
                        mode_result["client_ranking_comparison"] = _ranking_comparison(
                            baseline_rankings, ranked_results
                        )
                    backend_results[mode] = mode_result
            results[retriever.name] = backend_results
        postgres_version = postgres.version
        qdrant_version = qdrant.version_info
    finally:
        with reporter.stage("cleanup_backends"):
            try:
                if postgres is not None:
                    postgres.close()
            finally:
                if qdrant is not None:
                    qdrant.close()

    report: dict[str, object] = {
        "benchmark": "S3 bilingual retrieval benchmark",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "dataset_version": dataset.version,
        "dataset_corpus_sha256": dataset.corpus_sha256,
        "dataset_documents": len(dataset.documents),
        "dataset_queries": len(dataset.queries),
        "dataset_scenarios": len({query.scenario_id for query in dataset.queries}),
        "dataset_query_pairs_are_independent": False,
        "query_overlap_diagnostics": query_overlap_diagnostics(dataset),
        "embedding": {
            "model": MODEL_NAME,
            "source_repository": MODEL_SOURCE_REPO,
            "source_revision": MODEL_REVISION,
            "model_weight_sha256": MODEL_WEIGHT_SHA256,
            "fastembed_version": _distribution_version("fastembed"),
            "dimensions": MODEL_DIMENSIONS,
            "query_prefix": QUERY_PREFIX,
            "passage_prefix": PASSAGE_PREFIX,
            "device": "CPU",
            "latency": _embedding_latency_report(embedding_latencies),
        },
        "backend_versions": {
            "postgresql": postgres_version,
            "pgvector_extension": _pgvector_version(),
            "qdrant": qdrant_version,
        },
        "methodology": {
            "execution_strategy": EXECUTION_STRATEGY,
            "warmup_rounds_per_query_and_mode": WARMUP_ROUNDS,
            "measurement_repetitions_per_query_and_mode": MEASUREMENT_REPETITIONS,
            "candidate_depth": CANDIDATE_DEPTH,
            "recall_k": RECALL_K,
            "relevance_grades": {
                "3": "exact",
                "2": "acceptable substitute or supporting answer when explicitly requested",
                "1": "related but insufficient",
                "0": "irrelevant",
                "relevant_grades_for_recall_and_mrr": [2, 3],
                "ndcg_gain": "2^grade - 1",
            },
            "hard_negative_outrank_rate": (
                "fraction of labeled hard-negative documents ranked above the first relevant "
                "document within candidate depth"
            ),
            "query_mix": {
                challenge_type: sum(
                    query.challenge_type == challenge_type for query in dataset.queries
                )
                // 2
                for challenge_type in ("lexical", "paraphrase", "hard_negative")
            },
            "query_tasks": {
                task: sum(query.task == task for query in dataset.queries) // 2
                for task in ("product_grounding", "policy_grounding")
            },
            "rrf": {
                "implementation": "shared client-side weighted reciprocal_rank_fusion",
                "k": RRF_K,
                "rank_positions": "zero-based; score = sum(weight / (k + rank + 1))",
                "weighted_sweep": {
                    "50:50": WEIGHTED_RRF_CONFIGS["50_50"],
                    "70:30": WEIGHTED_RRF_CONFIGS["70_30"],
                    "80:20": WEIGHTED_RRF_CONFIGS["80_20"],
                },
                "tie_break": "document ID ascending",
            },
            "strict_lexical_gate": {
                "pgvector": (
                    "conjunctive plainto_tsquery; every non-stopword query term must match"
                ),
                "qdrant": (
                    "BM25 TextQuery with a MatchText filter over the full query; every term "
                    "must match"
                ),
                "tokenization_caveat": (
                    "PostgreSQL language configurations and Qdrant's word tokenizer differ; "
                    "strict gates are operationally analogous, not token-identical"
                ),
            },
            "single_roundtrip": {
                "pgvector": (
                    "one SQL statement with dense and disjunctive lexical candidate CTEs, "
                    "then the shared 50:50 RRF formula"
                ),
                "qdrant_default": (
                    "one query_points call with dense and TextQuery Prefetch, using native "
                    "FusionQuery RRF"
                ),
                "qdrant_formula_parity": (
                    f"one query_points call with RrfQuery(k={RRF_K + 1}, weights=[1, 1]); "
                    "compared against client-side 50:50 rankings"
                ),
                "ranking_comparison": (
                    "single-roundtrip results include exact-order match rate and mean "
                    "candidate-set overlap against client-side 50:50 RRF"
                ),
            },
            "qdrant_capabilities": {
                "qdrant_client_version": _distribution_version("qdrant-client"),
                **QDRANT_CAPABILITY_SUMMARY,
            },
            "lexical_methods": {
                **LEXICAL_METHODS,
            },
            "search_latency": "client-observed retrieval latency; excludes query embedding",
            "end_to_end_latency": (
                "query embedding plus retrieval and RRF; lexical mode does not embed"
            ),
            "ram": ("maximum sampled container memory; benchmark process RSS reported separately"),
            "release_gate": RELEASE_GATE,
            "provisional_recall_at_5_target": 0.9,
        },
        "hardware": _hardware_info(),
        "benchmark_runtime": {
            "python": platform.python_version(),
            "runner_image": os.environ.get("S3_BENCHMARK_RUNNER_IMAGE", "local"),
            "network": "private Compose service network",
        },
        "benchmark_process_max_rss_mib": _benchmark_process_max_rss_mib(),
        "results": results,
    }
    with reporter.stage("write_reports"):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        write_markdown_summary(report_path, report)
    return report


def _distribution_version(name: str) -> str:
    from importlib.metadata import version

    return version(name)


def _pgvector_version() -> str:
    import psycopg  # type: ignore[import-not-found]

    with psycopg.connect(POSTGRES_DSN, connect_timeout=5) as connection:
        row = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        if row is None:
            raise RuntimeError("pgvector extension was not installed")
        return row[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the explicit local S3 retrieval benchmark")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("evals/retrieval/results/s3-benchmark.json"),
    )
    run_kind = parser.add_mutually_exclusive_group()
    run_kind.add_argument(
        "--preflight", action="store_true", help="Validate local inputs and Docker readiness only"
    )
    run_kind.add_argument(
        "--smoke", action="store_true", help="Exercise synthetic backend retrieval without a model"
    )
    run_kind.add_argument(
        "--publish-staged-report",
        type=Path,
        help="Publish a completed staged JSON/Markdown report pair to --report",
    )
    arguments = parser.parse_args()
    try:
        if arguments.preflight:
            run_preflight(arguments.dataset, arguments.report)
            return 0
        if arguments.smoke:
            run_smoke_check()
            return 0
        if arguments.publish_staged_report is not None:
            with ProgressReporter().stage("publish_reports"):
                publish_report_pair(arguments.publish_staged_report, arguments.report)
            return 0
        dataset = load_dataset(arguments.dataset)
        require_approved_dataset(dataset)
        with ProgressReporter().stage("benchmark_run"):
            run_benchmark(dataset, arguments.report)
    except StageFailure as error:
        print(f"S3 benchmark failed at stage '{error.stage}': {error.cause}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"S3 benchmark refused: {error}", file=sys.stderr)
        return 2
    print(
        "Benchmark completed. "
        f"JSON report: {arguments.report}; "
        f"Markdown summary: {markdown_summary_path(arguments.report)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
