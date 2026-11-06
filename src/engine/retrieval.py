from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy import Engine

MODEL_NAME = "intfloat/multilingual-e5-large"
MODEL_REPOSITORY = "Qdrant/multilingual-e5-large-onnx"
MODEL_REVISION = "ac6781cd1cf88b8306a536d7c9d18a5bd57cc14b"
MODEL_ID = f"{MODEL_REPOSITORY}@{MODEL_REVISION}"
DIMENSIONS = 1024
LANGUAGES = {"en": "english", "ar": "arabic"}
MODEL_FILES = [
    "config.json",
    "model.onnx",
    "model.onnx_data",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
]


@dataclass(frozen=True)
class Passage:
    source_id: str
    version: str
    language: str
    text: str
    content_hash: str


@dataclass(frozen=True)
class SearchResult:
    source_id: str
    version: str
    language: str
    text: str
    content_hash: str
    score: float


class Embedder(Protocol):
    model_id: str

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class Retriever(Protocol):
    def search(self, query: str, language: str) -> list[SearchResult]: ...


def load_policies(path: Path) -> list[Passage]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"policies"}:
        raise ValueError("Policy source must contain only a policies array")
    policies = data["policies"]
    if not isinstance(policies, list) or not policies:
        raise ValueError("Policy source must contain at least one policy")
    passages: list[Passage] = []
    seen: set[str] = set()
    for policy in policies:
        if not isinstance(policy, dict) or set(policy) != {"id", "version", "en", "ar"}:
            raise ValueError("Each policy must have an ID, version, and EN/AR passages")
        source_id, version = policy["id"], policy["version"]
        if not isinstance(source_id, str) or not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*", source_id
        ):
            raise ValueError("Policy ID must be a lowercase slug")
        if source_id in seen:
            raise ValueError(f"Duplicate policy ID: {source_id}")
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"Policy {source_id} needs a version")
        seen.add(source_id)
        for language in LANGUAGES:
            value = policy[language]
            if not isinstance(value, dict) or set(value) != {"text", "sha256"}:
                raise ValueError(f"Policy {source_id} needs text and hash for {language}")
            text, digest = value["text"], value["sha256"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Policy {source_id} has empty {language} text")
            if (
                not isinstance(digest, str)
                or digest != hashlib.sha256(text.encode("utf-8")).hexdigest()
            ):
                raise ValueError(f"Policy {source_id} has invalid {language} hash")
            passages.append(Passage(source_id, version, language, text, digest))
    return passages


def _vectors(embedder: Embedder, texts: Sequence[str]) -> list[str]:
    if embedder.model_id != MODEL_ID:
        raise ValueError("Embedding model does not match the policy index")
    vectors = list(embedder.embed(texts))
    if len(vectors) != len(texts):
        raise ValueError("Embedder returned the wrong number of vectors")
    result = []
    for vector in vectors:
        values = [float(value) for value in vector]
        if len(values) != DIMENSIONS or not all(math.isfinite(value) for value in values):
            raise ValueError("Embedding must have 1024 finite dimensions")
        if not any(values):
            raise ValueError("Embedding must not be a zero vector")
        result.append("[" + ",".join(str(value) for value in values) + "]")
    return result


class LocalE5Embedder:
    """Load only the pinned local Hugging Face snapshot; never fetch from the network."""

    model_id = MODEL_ID

    def __init__(self) -> None:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

        model_path = snapshot_download(
            repo_id=MODEL_REPOSITORY,
            revision=MODEL_REVISION,
            token=False,
            local_files_only=True,
            allow_patterns=MODEL_FILES,
        )
        self._model = TextEmbedding(
            model_name=MODEL_NAME, threads=1, specific_model_path=model_path, local_files_only=True
        )

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[float(value) for value in vector] for vector in self._model.embed(list(texts))]


def ingest(engine: Engine, embedder: Embedder, source: Path) -> int:
    passages = load_policies(source)
    vectors = _vectors(embedder, [f"passage: {passage.text}" for passage in passages])
    with engine.begin() as conn:
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(640001)"))
        conn.execute(sa.text("DELETE FROM policy_passages"))
        statement = sa.text(
            """
            INSERT INTO policy_passages
                (source_id, version, language, content, content_hash, model_id,
                 embedding, search_vector)
            VALUES
                (:source_id, :version, :language, :content, :content_hash, :model_id,
                 CAST(:embedding AS vector), to_tsvector(CAST(:config AS regconfig), :content))
            """
        )
        for passage, vector in zip(passages, vectors, strict=True):
            conn.execute(
                statement,
                {
                    "source_id": passage.source_id,
                    "version": passage.version,
                    "language": passage.language,
                    "content": passage.text,
                    "content_hash": passage.content_hash,
                    "model_id": MODEL_ID,
                    "embedding": vector,
                    "config": LANGUAGES[passage.language],
                },
            )
    return len(passages)


class PostgresRetriever:
    def __init__(self, engine: Engine, embedder: Embedder) -> None:
        self._engine = engine
        self._embedder = embedder

    def search(self, query: str, language: str) -> list[SearchResult]:
        if language not in LANGUAGES:
            raise ValueError("Language must be en or ar")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Query must not be empty")
        vector = _vectors(self._embedder, [f"query: {query}"])[0]
        with self._engine.connect() as conn:
            mismatched = conn.execute(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM policy_passages WHERE model_id <> :model_id)"
                ),
                {"model_id": MODEL_ID},
            ).scalar_one()
            if mismatched:
                raise ValueError("Policy index uses an incompatible embedding model; rebuild it")
            rows = conn.execute(
                sa.text(
                    """
                    WITH terms AS (
                        SELECT replace(plainto_tsquery(CAST(:config AS regconfig), :query)::text,
                                       ' & ', ' | ')::tsquery AS value
                    ),
                    dense AS (
                        SELECT source_id, row_number() OVER (ORDER BY distance, source_id) AS rank
                        FROM (
                            SELECT source_id, embedding <=> CAST(:vector AS vector) AS distance
                            FROM policy_passages WHERE language = :language
                            ORDER BY distance, source_id LIMIT 20
                        ) candidates
                    ),
                    lexical AS (
                        SELECT source_id,
                               row_number() OVER (ORDER BY relevance DESC, source_id) AS rank
                        FROM (
                            SELECT source_id, ts_rank_cd(search_vector, terms.value) AS relevance
                            FROM policy_passages, terms
                            WHERE language = :language AND search_vector @@ terms.value
                            ORDER BY relevance DESC, source_id LIMIT 20
                        ) candidates
                    ),
                    scores AS (
                        SELECT source_id, sum(weight / (60 + rank)) AS score FROM (
                            SELECT source_id, rank, 0.8::double precision AS weight FROM dense
                            UNION ALL
                            SELECT source_id, rank, 0.2::double precision AS weight FROM lexical
                        ) ranked GROUP BY source_id
                    )
                    SELECT p.source_id, p.version, p.language, p.content,
                           p.content_hash, scores.score
                    FROM scores JOIN policy_passages p USING (source_id)
                    WHERE p.language = :language
                    ORDER BY scores.score DESC, p.source_id LIMIT 5
                    """
                ),
                {
                    "config": LANGUAGES[language],
                    "query": query,
                    "vector": vector,
                    "language": language,
                },
            ).all()
        return [
            SearchResult(
                row.source_id, row.version, row.language, row.content, row.content_hash, row.score
            )
            for row in rows
        ]
