"""#131: reconciliation evidence and the held-runs API (REC-1, REC-3, REC-11; ADR-0030).

Spec: docs/specs/execution-reconciliation.md. Held runs are synthetic rows written straight
into the throwaway test database (local-test-db skill) and removed afterwards; Odoo and
Mailpit are fakes, so nothing live is called.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from engine.approval_api import create_app
from engine.auth import TokenVerifier
from engine.config import normalize_database_url
from engine.coupon_code import coupon_code_for_run
from engine.db import (
    RunRepository,
    agent_proposals,
    create_engine,
    effect_message_id,
    execution_claims,
    execution_effects,
    runs,
    upgrade_schema,
)
from engine.reconcile import ReconcileConfigError, ReconcileSettings
from engine.reconcile_cli import CouponLookup, MailpitLookup, check, mailpit_api_url
from engine.runs import RunState
from scripts.dev_oidc import DevIssuer

URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")

ISSUER = DevIssuer(Path(tempfile.mkdtemp(prefix="dev-oidc-reconcile-")))
SETTINGS = ReconcileSettings(stale_started_minutes=30)
NOW = datetime.now(UTC)
EMAIL = "layla.haddad@example.test"
CHAT = "123456789"


@pytest.fixture
def db() -> Iterator[sa.Engine]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    yield engine
    engine.dispose()


def _held_run(
    engine: sa.Engine,
    *,
    channel: str,
    effects: dict[str, tuple[str, datetime]],
    state: RunState = RunState.EXECUTING,
    store_message_id: bool = True,
) -> uuid.UUID:
    """A synthetic executing run with journal rows; customer fields hold PII to prove filtering."""
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                id=run_id,
                state=state.value,
                trigger_type="synthetic",
                trigger_ref=f"cart-for-{EMAIL}",
                idempotency_key=str(uuid.uuid4()),
                customer_id=EMAIL,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        conn.execute(
            agent_proposals.insert().values(
                run_id=run_id,
                schema_version="test-v1",
                fingerprint="f" * 64,
                proposal={
                    "execution_channel": channel,
                    "customer_message": f"Hello {EMAIL}, chat {CHAT}",
                    "customer_id": EMAIL,
                },
                gate_result={"outcome": "allow"},
            )
        )
        conn.execute(
            execution_claims.insert().values(
                run_id=run_id, claim_id=claim_id, binding_hash="b" * 64, created_at=NOW
            )
        )
        for effect, (status, updated) in effects.items():
            message_id = (
                effect_message_id(run_id, claim_id, 1)
                if effect == "message" and channel == "mailpit" and store_message_id
                else None
            )
            conn.execute(
                execution_effects.insert().values(
                    run_id=run_id,
                    effect=effect,
                    status=status,
                    attempts=1,
                    max_attempts=1,
                    updated_at=updated,
                    message_id=message_id,
                )
            )
    return run_id


def _clean(engine: sa.Engine, run_ids: list[uuid.UUID]) -> None:
    with engine.begin() as conn:
        for table in ("execution_effects", "execution_claims", "agent_proposals", "run_events"):
            conn.execute(sa.text(f"DELETE FROM {table} WHERE run_id = ANY(:ids)"), {"ids": run_ids})
        conn.execute(runs.delete().where(runs.c.id.in_(run_ids)))


def _snapshot(engine: sa.Engine) -> dict[str, object]:
    with engine.connect() as conn:
        return {
            table: conn.execute(sa.text(f"SELECT * FROM {table} ORDER BY 1, 2")).all()
            for table in ("runs", "run_events", "execution_effects", "execution_claims")
        }


class FakeOdoo:
    def __init__(self, cards: dict[str, int], *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.cards = cards
        self.fail = fail

    def __call__(self, model: str, method: str, params: dict[str, object]) -> object:
        self.calls.append((model, method))
        if self.fail:
            raise OSError("Odoo timed out")
        domain = params["domain"]
        assert isinstance(domain, list)
        code = domain[0][2]
        return [{"id": i, "code": code} for i in range(self.cards.get(code, 0))]


class FakeMailpit:
    def __init__(self, stored: set[str], *, fail: bool = False) -> None:
        self.urls: list[str] = []
        self.stored = stored
        self.fail = fail

    def __call__(self, url: str) -> Any:
        self.urls.append(url)
        if self.fail:
            raise OSError("Mailpit down")
        query = url.split("query=", 1)[1]
        hits = [{"ID": "x", "MessageID": m} for m in self.stored if m in _unquote(query)]
        return {"messages": hits, "messages_count": len(hits), "total": 9}


def _unquote(value: str) -> str:
    from urllib.parse import unquote_plus

    return unquote_plus(value.split("&", 1)[0])


def test_reconcile_check_is_read_only(db: sa.Engine) -> None:
    """REC-1: lookups only; no Odoo write, no dispatch, no engine DB change."""
    old = NOW - timedelta(hours=2)
    mail = _held_run(
        db, channel="mailpit", effects={"coupon": ("confirmed", old), "message": ("unknown", old)}
    )
    gram = _held_run(db, channel="telegram", effects={"message": ("unknown", old)})
    crashed = _held_run(db, channel="mailpit", effects={"coupon": ("started", old)})
    fresh = _held_run(db, channel="mailpit", effects={"coupon": ("started", NOW)})
    done = _held_run(
        db, channel="mailpit", effects={"message": ("unknown", old)}, state=RunState.COMPLETED
    )
    created = [mail, gram, crashed, fresh, done]
    try:
        repo = RunRepository(db)
        with db.connect() as conn:
            stored = conn.execute(
                sa.select(execution_effects.c.message_id).where(
                    execution_effects.c.run_id == mail, execution_effects.c.effect == "message"
                )
            ).scalar_one()
        odoo = FakeOdoo({coupon_code_for_run(str(crashed)): 1})
        mailpit = FakeMailpit({stored.strip("<>")})
        before = _snapshot(db)
        rows = check(
            repo,
            SETTINGS,
            CouponLookup(odoo),
            MailpitLookup("http://mailpit:8025", fetch=mailpit),
            limit=100,
            now=NOW,
        )
        assert _snapshot(db) == before  # no engine DB change
        assert set(odoo.calls) == {("loyalty.card", "search_read")}  # read only
        assert all(url.startswith("http://mailpit:8025/api/v1/search?") for url in mailpit.urls)
        found = {
            (row["run_id"], row["effect"]): row
            for row in rows
            if row["run_id"] in {str(run) for run in created}
        }
        # Held: unknown, or started past the stale limit, on executing runs only.
        assert set(found) == {
            (str(mail), "message"),
            (str(gram), "message"),
            (str(crashed), "coupon"),
        }
        assert found[(str(mail), "message")]["evidence"] == "present"
        assert found[(str(gram), "message")]["evidence"] == "attestation_required"
        assert found[(str(crashed), "coupon")]["evidence"] == "present"
        assert repo.held_effect_count(stale_before=SETTINGS.stale_before(NOW)) >= 3
        assert repo.held_effect_count(stale_before=SETTINGS.stale_before(NOW)) == (
            repo.held_effect_count() + 1  # the crashed `started` coupon
        )
    finally:
        _clean(db, created)


def test_lookup_errors_are_unknown_not_absent() -> None:
    """REC-3: an error, a malformed answer, or a partial page is `unknown`, never `absent`."""
    run_id = uuid.uuid4()
    code = coupon_code_for_run(str(run_id))
    assert CouponLookup(FakeOdoo({code: 1})).evidence(run_id) == "present"
    assert CouponLookup(FakeOdoo({})).evidence(run_id) == "absent"
    assert CouponLookup(FakeOdoo({}, fail=True)).evidence(run_id) == "unknown"
    assert CouponLookup(FakeOdoo({code: 2})).evidence(run_id) == "unknown"  # not unique
    assert CouponLookup(lambda *a: {"error": "x"}).evidence(run_id) == "unknown"
    assert CouponLookup(lambda *a: [{"id": 1, "code": "OTHER"}]).evidence(run_id) == "unknown"

    mid = "<aorte-1111-a@aorte.invalid>"
    base = "http://mailpit:8025"
    assert (
        MailpitLookup(base, fetch=FakeMailpit({"aorte-1111-a@aorte.invalid"})).evidence(mid)
        == "present"
    )
    assert MailpitLookup(base, fetch=FakeMailpit(set())).evidence(mid) == "absent"
    assert MailpitLookup(base, fetch=FakeMailpit(set(), fail=True)).evidence(mid) == "unknown"
    assert MailpitLookup(base, fetch=lambda url: "<html>").evidence(mid) == "unknown"
    assert MailpitLookup(base, fetch=FakeMailpit(set())).evidence(None) == "unknown"  # pre-#131
    # Substring matches are not the message (measured: Mailpit matches substrings).
    near = {"messages": [{"MessageID": "aorte-1111-ab@aorte.invalid"}], "messages_count": 1}
    assert MailpitLookup(base, fetch=lambda url: near).evidence(mid) == "absent"
    partial = {"messages": [{"MessageID": "aorte-1111-ab@aorte.invalid"}], "messages_count": 60}
    assert MailpitLookup(base, fetch=lambda url: partial).evidence(mid) == "unknown"
    bad_count = {"messages": [], "messages_count": "0"}
    assert MailpitLookup(base, fetch=lambda url: bad_count).evidence(mid) == "unknown"


def test_held_runs_api_returns_no_pii(db: sa.Engine) -> None:
    """REC-11: `GET /held-runs` (viewer) returns codes and ages only."""
    old = NOW - timedelta(hours=1)
    held = _held_run(
        db, channel="mailpit", effects={"coupon": ("confirmed", old), "message": ("unknown", old)}
    )
    try:
        client = TestClient(
            create_app(RunRepository(db), TokenVerifier(ISSUER.settings()), reconcile=SETTINGS),
            client=("127.0.0.1", 1234),
        )
        viewer = {"Authorization": f"Bearer {ISSUER.token('viewer-1', ['viewer'])}"}
        assert client.get("/held-runs").status_code == 401
        response = client.get("/held-runs", headers=viewer)
        assert response.status_code == 200
        (run,) = [item for item in response.json() if item["run_id"] == str(held)]
        assert run == {
            "run_id": str(held),
            "state": "executing",
            "held_effects": [
                {
                    "effect": "message",
                    "status": "unknown",
                    "attempts": 1,
                    "age_minutes": run["held_effects"][0]["age_minutes"],
                    "channel": "mailpit",
                    "message_id_stored": True,
                }
            ],
        }
        assert 59 <= run["held_effects"][0]["age_minutes"] <= 61
        body = response.text
        for leaked in (
            EMAIL,
            CHAT,
            "Hello",
            "cart-for",
            "aorte.invalid",
            coupon_code_for_run(str(held)),
        ):
            assert leaked not in body, leaked
        # Without the setting the endpoint does not exist (main() requires it, fail closed).
        bare = TestClient(
            create_app(RunRepository(db), TokenVerifier(ISSUER.settings())), client=("127.0.0.1", 1)
        )
        assert bare.get("/held-runs", headers=viewer).status_code == 404
    finally:
        _clean(db, [held])


def test_reconcile_settings_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        ReconcileSettings.from_env({"RECONCILE_STALE_STARTED_MINUTES": "30"}).stale_started_minutes
        == 30
    )
    for bad in ("", "0", "-5", "abc", "1.5", str(7 * 24 * 60 + 1)):
        with pytest.raises(ReconcileConfigError):
            ReconcileSettings.from_env({"RECONCILE_STALE_STARTED_MINUTES": bad})
    assert (
        mailpit_api_url({"RECONCILE_MAILPIT_API_URL": "http://127.0.0.1:8025/"})
        == "http://127.0.0.1:8025"
    )
    for bad in (
        "",
        "https://127.0.0.1:8025",
        "http://8.8.8.8:8025",
        "http://127.0.0.1:8025/api",
        "ftp://x",
    ):
        with pytest.raises(ValueError):
            mailpit_api_url({"RECONCILE_MAILPIT_API_URL": bad})


def test_erp_list_shows_no_customer_data(db: sa.Engine) -> None:
    """#131 `erp_cli list`: ID, state, trigger type, created time only."""
    run_id = _held_run(db, channel="telegram", effects={})
    try:
        rows = RunRepository(db).recent_runs(limit=200, state=RunState.EXECUTING)
        (row,) = [r for r in rows if r["run_id"] == str(run_id)]
        assert set(row) == {"run_id", "state", "trigger_type", "created_at"}
        assert row["state"] == "executing" and row["trigger_type"] == "synthetic"
        assert EMAIL not in json.dumps(rows)
        with pytest.raises(ValueError):
            RunRepository(db).recent_runs(limit=0)
    finally:
        _clean(db, [run_id])
