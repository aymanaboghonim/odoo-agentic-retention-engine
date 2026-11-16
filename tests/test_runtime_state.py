"""#87 runtime state invariants KS-1, KS-2, KS-6, KS-7, KS-8.

Spec: docs/specs/release-gates-and-kill-switch.md.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from engine.approval_api import create_app
from engine.approval_cli import main as cli
from engine.auth import TokenVerifier
from engine.config import normalize_database_url
from engine.db import RunRepository, create_engine, upgrade_schema
from engine.runtime_state import (
    EngineHalted,
    RuntimeGuard,
    RuntimeState,
    RuntimeStateConflict,
    RuntimeStateError,
    RuntimeStateRepository,
    validate_reason,
)
from scripts.dev_oidc import DevIssuer

URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")

ISSUER = DevIssuer(Path(tempfile.mkdtemp(prefix="dev-oidc-runtime-")))


def reset(engine: sa.Engine) -> None:
    """The state is a singleton shared by the whole database: always leave it `active`."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO engine_runtime_state "
                "(id, state, reason, changed_by, changed_at, version) "
                "VALUES (1, 'active', 'initial state', 'migration', now(), 1) "
                "ON CONFLICT (id) DO UPDATE SET state='active', reason='initial state', "
                "changed_by='migration', changed_at=now(), version=1"
            )
        )
        conn.execute(sa.text("DELETE FROM engine_runtime_events"))


@pytest.fixture
def db() -> Iterator[sa.Engine]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    reset(engine)
    yield engine
    reset(engine)
    engine.dispose()


@pytest.fixture
def runtime(db: sa.Engine) -> RuntimeStateRepository:
    return RuntimeStateRepository(db)


def api(db: sa.Engine, runtime: RuntimeStateRepository) -> TestClient:
    return TestClient(
        create_app(RunRepository(db), TokenVerifier(ISSUER.settings()), runtime=runtime),
        client=("127.0.0.1", 1234),
    )


def bearer(subject: str, role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ISSUER.token(subject, [role])}"}


def test_runtime_state_is_durable_and_audited(
    db: sa.Engine, runtime: RuntimeStateRepository
) -> None:
    """KS-1: default active, durable across connections, every change audited."""
    first = runtime.get()
    assert (first.state, first.version, first.changed_by) == (RuntimeState.ACTIVE, 1, "migration")
    changed = runtime.set_state(
        RuntimeState.HALTED, actor="admin-sub-1", reason="planned maintenance window"
    )
    assert (changed.state, changed.version) == (RuntimeState.HALTED, 2)
    restarted = RuntimeStateRepository(create_engine(normalize_database_url(URL)))
    persisted = restarted.get()
    assert persisted.state is RuntimeState.HALTED
    assert (persisted.changed_by, persisted.reason) == ("admin-sub-1", "planned maintenance window")
    runtime.set_state(RuntimeState.ACTIVE, actor="admin-sub-2", reason="maintenance finished")
    history = runtime.history()
    assert [(e["from_state"], e["to_state"], e["actor"]) for e in reversed(history)] == [
        ("active", "halted", "admin-sub-1"),
        ("halted", "active", "admin-sub-2"),
    ]
    assert history[0]["version"] == 3 and history[0]["at"]
    # Invalid changes are refused and leave no audit row.
    with pytest.raises(RuntimeStateConflict):
        runtime.set_state(RuntimeState.ACTIVE, actor="admin-sub-2", reason="already active")
    with pytest.raises(RuntimeStateError):
        runtime.set_state(RuntimeState.HALTED, actor=" ", reason="no actor given")
    with pytest.raises(RuntimeStateError):
        runtime.set_state("halted", actor="a", reason="not an enum")  # type: ignore[arg-type]
    assert len(runtime.history()) == 2
    # The database itself refuses any other state value or a second row.
    with pytest.raises(sa.exc.IntegrityError), db.begin() as conn:
        conn.execute(sa.text("UPDATE engine_runtime_state SET state='paused' WHERE id=1"))
    with pytest.raises(sa.exc.IntegrityError), db.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO engine_runtime_state VALUES (2, 'active', 'x', 'y', now(), 1)")
        )


def test_runtime_endpoints_enforce_roles(db: sa.Engine, runtime: RuntimeStateRepository) -> None:
    """KS-2: viewer and above can read; only admin can change; the actor is the token sub."""
    client = api(db, runtime)
    body = {"state": "halted", "reason": "provider incident under review"}
    for role in ("viewer", "approver", "admin"):
        read = client.get("/runtime", headers=bearer(f"{role}-sub", role))
        assert read.status_code == 200 and read.json()["state"] == "active", role
    assert client.get("/runtime").status_code == 401
    for role in ("viewer", "approver"):
        denied = client.post("/runtime/state", headers=bearer(f"{role}-sub", role), json=body)
        assert denied.status_code == 403, role
    assert runtime.get().state is RuntimeState.ACTIVE
    ok = client.post("/runtime/state", headers=bearer("admin-sub-9", "admin"), json=body)
    assert ok.status_code == 200
    assert ok.json()["state"] == "halted" and ok.json()["changed_by"] == "admin-sub-9"
    assert runtime.history()[0]["actor"] == "admin-sub-9"
    admin = bearer("admin-sub-9", "admin")
    stale = client.post(
        "/runtime/state",
        headers=admin,
        json={"state": "active", "reason": "back to normal", "expected_version": 1},
    )
    assert stale.status_code == 409
    same = client.post(
        "/runtime/state", headers=admin, json={"state": "halted", "reason": "no change at all"}
    )
    assert same.status_code == 409
    for bad in (
        {"state": "paused", "reason": "unknown state value"},
        {"state": "active", "reason": "ok reason", "extra": 1},
        {"state": "active"},
        {"state": "active", "reason": "ab"},
        {"state": "active", "reason": "mail ops@example.test about it"},
    ):
        assert client.post("/runtime/state", headers=admin, json=bad).status_code == 422, bad
    assert runtime.get().state is RuntimeState.HALTED
    # Without a runtime repository the endpoints do not exist.
    bare = TestClient(
        create_app(RunRepository(db), TokenVerifier(ISSUER.settings())), client=("127.0.0.1", 1)
    )
    assert bare.get("/runtime", headers=admin).status_code == 404


def test_unreadable_state_fails_closed(db: sa.Engine, runtime: RuntimeStateRepository) -> None:
    """KS-6: any failure to read the state is treated as halted."""
    assert RuntimeGuard(runtime).state() is RuntimeState.ACTIVE

    def broken() -> None:
        raise RuntimeError("database is down")

    guard = RuntimeGuard(broken)
    assert guard.state() is RuntimeState.HALTED
    with pytest.raises(EngineHalted):
        guard.require_not_halted()
    unreachable = create_engine("postgresql+psycopg://x:y@127.0.0.1:1/none?connect_timeout=1")
    assert RuntimeGuard(RuntimeStateRepository(unreachable)).state() is RuntimeState.HALTED
    with db.begin() as conn:
        conn.execute(sa.text("DELETE FROM engine_runtime_state"))
    assert RuntimeGuard(runtime).state() is RuntimeState.HALTED
    with pytest.raises(RuntimeStateError):
        runtime.get()


def test_state_reason_rejects_contact_like_text(runtime: RuntimeStateRepository) -> None:
    """KS-7: a short, single-line, non-PII reason."""
    assert validate_reason("  provider incident #87  ") == "provider incident #87"
    for bad in (
        "",
        "ab",
        "x" * 201,
        "write to ops@example.test",
        "call +966 50 123 4567 now",
        "call 0501234567 now",
        "line one\nline two",
        "tab\tseparated",
    ):
        with pytest.raises(RuntimeStateError):
            validate_reason(bad)
        with pytest.raises(RuntimeStateError):
            runtime.set_state(RuntimeState.HALTED, actor="admin", reason=bad)
    assert runtime.get().state is RuntimeState.ACTIVE and runtime.history() == []


def test_concurrent_state_changes_one_wins(runtime: RuntimeStateRepository) -> None:
    """KS-8: two changes from the same version cannot both win."""
    version = runtime.get().version
    barrier = threading.Barrier(2)
    results: list[object] = []

    def change(state: RuntimeState, actor: str) -> None:
        barrier.wait()
        try:
            results.append(
                runtime.set_state(
                    state, actor=actor, reason="racing admins", expected_version=version
                )
            )
        except RuntimeStateConflict as error:
            results.append(error)

    threads = [
        threading.Thread(target=change, args=(RuntimeState.HALTED, "admin-a")),
        threading.Thread(target=change, args=(RuntimeState.FALLBACK, "admin-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(isinstance(r, RuntimeStateConflict) for r in results) == 1
    assert runtime.get().version == version + 1
    assert len(runtime.history()) == 1


class Response(io.BytesIO):
    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_cli_reads_and_sets_runtime_through_the_api(
    db: sa.Engine,
    runtime: RuntimeStateRepository,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = api(db, runtime)

    class Opener:
        def open(self, request, timeout):  # type: ignore[no-untyped-def]
            response = client.request(
                request.get_method(),
                request.full_url,
                headers=dict(request.header_items()),
                content=request.data,
            )
            if response.status_code != 200:
                raise HTTPError(request.full_url, response.status_code, "error", Message(), None)
            return Response(response.content)

    monkeypatch.setattr("engine.approval_cli.build_opener", lambda *_: Opener())
    viewer = {"APPROVAL_ACCESS_TOKEN": ISSUER.token("viewer-1", ["viewer"])}
    admin = {"APPROVAL_ACCESS_TOKEN": ISSUER.token("admin-1", ["admin"])}
    assert cli(["runtime"], viewer) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "active"
    args = ["set-runtime", "--state", "fallback_fixed_rule", "--reason", "provider incident"]
    assert cli(args, viewer) == 1 and "HTTP 403" in capsys.readouterr().err
    assert cli(args, admin) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "fallback_fixed_rule"
    for bad in (
        ["set-runtime"],
        ["set-runtime", "--state", "halted"],
        ["runtime", "--state", "halted"],
    ):
        with pytest.raises(SystemExit):
            cli(bad, admin)
    assert runtime.get().changed_by == "admin-1"
