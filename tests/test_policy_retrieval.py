from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
import sqlalchemy as sa

from engine.config import normalize_database_url
from engine.db import create_engine, upgrade_schema
from engine.retrieval import (
    DIMENSIONS,
    MODEL_ID,
    PostgresRetriever,
    _vectors,
    ingest,
    load_policies,
)

SOURCE = Path("policies/synthetic.json")
URL = os.environ.get("TEST_DATABASE_URL", "")

if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")


class FakeEmbedder:
    model_id = MODEL_ID

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.calls.append(list(texts))
        return [[1.0, 0.0] + [0.0] * (DIMENSIONS - 2) for _ in texts]


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    db = create_engine(normalize_database_url(URL))
    upgrade_schema(db)
    with db.begin() as conn:
        conn.execute(sa.text("DELETE FROM policy_passages"))
    yield db
    with db.begin() as conn:
        conn.execute(sa.text("DELETE FROM policy_passages"))
    db.dispose()


def test_source_validation(tmp_path: Path) -> None:
    passages = load_policies(SOURCE)
    assert len(passages) == 8
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    path = tmp_path / "source.json"
    data["policies"][0]["en"]["text"] = "tampered"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        load_policies(path)
    data["policies"][0] = data["policies"][1]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        load_policies(path)


def test_embedding_contract() -> None:
    fake = FakeEmbedder()
    assert len(_vectors(fake, ["query: test"])) == 1
    fake.model_id = "another model"
    with pytest.raises(ValueError, match="model"):
        _vectors(fake, ["query: test"])

    class BadEmbedder(FakeEmbedder):
        def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
            return [[0.0] * DIMENSIONS for _ in texts]

    with pytest.raises(ValueError, match="zero"):
        _vectors(BadEmbedder(), ["query: test"])


def test_ingestion_idempotent_and_atomic(engine: sa.Engine, tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    assert ingest(engine, embedder, SOURCE) == 8
    assert all(text.startswith("passage: ") for text in embedder.calls[0])
    assert ingest(engine, embedder, SOURCE) == 8
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM policy_passages")).scalar_one() == 8
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    data["policies"].pop()
    data["policies"][0]["version"] = "2"
    data["policies"][0]["en"]["text"] = "Updated consent text"
    data["policies"][0]["en"]["sha256"] = hashlib.sha256(b"Updated consent text").hexdigest()
    path = tmp_path / "policies.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert ingest(engine, embedder, path) == 6
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM policy_passages")).scalar_one() == 6
    data["policies"][0]["en"]["sha256"] = hashlib.sha256(b"Updated consent text").hexdigest()
    path.write_text(json.dumps(data), encoding="utf-8")

    def fail_insert(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "INSERT INTO policy_passages" in statement:
            raise RuntimeError("injected insert failure")

    sa.event.listen(engine, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            ingest(engine, embedder, path)
    finally:
        sa.event.remove(engine, "before_cursor_execute", fail_insert)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM policy_passages")).scalar_one() == 6
        assert (
            conn.execute(
                sa.text(
                    "SELECT content FROM policy_passages "
                    "WHERE source_id='consent' AND language='en'"
                )
            ).scalar_one()
            == "Updated consent text"
        )
    data["policies"][0]["en"]["sha256"] = "bad"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        ingest(engine, embedder, path)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM policy_passages")).scalar_one() == 6


def test_bilingual_retrieval_and_invalid_input(engine: sa.Engine) -> None:
    fake = FakeEmbedder()
    ingest(engine, fake, SOURCE)
    retriever = PostgresRetriever(engine, fake)
    english = retriever.search("consent customers", "en")
    arabic = retriever.search("العملاء", "ar")
    assert english[0].source_id == "consent"
    assert arabic[0].source_id == "consent"
    assert english[0].version == "1" and english[0].text
    assert all(result.language == "ar" for result in arabic)
    assert fake.calls[-1] == ["query: العملاء"]
    assert len(english) <= 5
    for query, language in [("", "en"), ("test", "fr")]:
        with pytest.raises(ValueError):
            retriever.search(query, language)
    retriever.search("'; DROP TABLE policy_passages; --", "en")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM policy_passages")).scalar_one() == 8
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE policy_passages SET model_id = 'wrong'"))
    with pytest.raises(ValueError, match="incompatible"):
        retriever.search("consent", "en")


def test_hybrid_rrf_matches_reference(engine: sa.Engine) -> None:
    fake = FakeEmbedder()
    ingest(engine, fake, SOURCE)
    hits = PostgresRetriever(engine, fake).search("consent customers", "en")
    with engine.connect() as conn:
        dense = (
            conn.execute(
                sa.text(
                    "SELECT source_id FROM policy_passages WHERE language='en' ORDER BY source_id"
                )
            )
            .scalars()
            .all()
        )
        lexical = (
            conn.execute(
                sa.text("""
                WITH q AS (
                    SELECT replace(plainto_tsquery('english', :query)::text,
                                   ' & ', ' | ')::tsquery AS v
                )
                SELECT source_id FROM policy_passages, q
                WHERE language='en' AND search_vector @@ q.v
                ORDER BY ts_rank_cd(search_vector, q.v) DESC, source_id LIMIT 20
            """),
                {"query": "consent customers"},
            )
            .scalars()
            .all()
        )
    scores = {
        source_id: 0.8 / (60 + dense.index(source_id) + 1)
        + (0.2 / (60 + lexical.index(source_id) + 1) if source_id in lexical else 0)
        for source_id in dense
    }
    expected = sorted(scores, key=lambda source_id: (-scores[source_id], source_id))
    assert [hit.source_id for hit in hits] == expected[:5]
    assert [hit.score for hit in hits] == pytest.approx([scores[key] for key in expected[:5]])


def test_candidate_depth_and_result_limit(engine: sa.Engine, tmp_path: Path) -> None:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    sample = data["policies"][0]
    data["policies"] = [{**sample, "id": f"policy-{number:02d}"} for number in range(25)]
    source = tmp_path / "policies.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    fake = FakeEmbedder()
    ingest(engine, fake, source)
    hits = PostgresRetriever(engine, fake).search("zzzznomatch", "en")
    assert [hit.source_id for hit in hits] == [f"policy-{number:02d}" for number in range(5)]
    assert [hit.score for hit in hits] == pytest.approx([0.8 / (61 + n) for n in range(5)])
