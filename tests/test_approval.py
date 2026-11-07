from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from email.message import Message
from typing import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from engine.approval_api import Operator, create_app
from engine.approval_cli import main as cli
from engine.config import normalize_database_url
from engine.db import ApprovalConflict, RunRepository, create_engine, upgrade_schema
from engine.gate_ledger import InMemoryGateLedger
from engine.policy import Gate, GateConfig, GateRejected, OfferProposal, as_dict
from engine.runs import RunState

URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")

TOKEN = "synthetic-token-" + "x" * 32


@pytest.fixture
def db() -> Iterator[sa.Engine]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def gate() -> Gate:
    config = GateConfig.from_env(
        {
            "GATE_TIMEZONE": "Asia/Riyadh",
            "GATE_CURRENCY": "USD",
            "GATE_DAILY_BUDGET": "100",
            "GATE_CATEGORY_MIN_MARGINS": '{"decor":"0.2"}',
        }
    )
    return Gate(config, InMemoryGateLedger())


@pytest.fixture
def pending(db: sa.Engine, gate: Gate) -> Iterator[uuid.UUID]:
    repo = RunRepository(db)
    run, _ = repo.create_run(
        trigger_type="synthetic", trigger_ref="9:1", idempotency_key=str(uuid.uuid4())
    )
    for current, new in [
        (RunState.RECEIVED, RunState.VALIDATING_INPUT),
        (RunState.VALIDATING_INPUT, RunState.LOADING_CONTEXT),
        (RunState.LOADING_CONTEXT, RunState.GENERATING_PROPOSAL),
    ]:
        repo.transition(run.id, current, new, actor="system")
    proposal = OfferProposal(
        run_id=str(run.id),
        customer_id="9",
        cart_id="1",
        cart_owner_id="9",
        category="decor",
        currency="USD",
        eligible_total=Decimal("100"),
        eligible_unit_cost_total=Decimal("40"),
        proposed_discount_percent=Decimal("12"),
        customer_message="A synthetic offer.",
    )
    record = json.loads(json.dumps(asdict(proposal), default=str))
    record.update({"action": "draft_message", "policy_sources": [{"source_id": "consent"}]})
    result = as_dict(gate.evaluate(proposal, now=datetime.now(UTC)))
    repo.persist_proposal(
        run.id,
        schema_version="test-v1",
        fingerprint=proposal.fingerprint(),
        proposal=record,
        gate_result=result,
    )
    repo.request_approval(run.id, gate)
    yield run.id
    with db.begin() as conn:
        for table in ("approval_requests", "agent_proposals", "run_events"):
            conn.execute(sa.text(f"DELETE FROM {table} WHERE run_id=:run"), {"run": run.id})
        conn.execute(sa.text("DELETE FROM runs WHERE id=:run"), {"run": run.id})


def token_for(repo: RunRepository, run_id: uuid.UUID) -> str:
    return next(
        row["snapshot_hash"] for row in repo.pending_approvals() if row["run_id"] == str(run_id)
    )


def client(repo: RunRepository, *, host: str = "127.0.0.1") -> TestClient:
    return TestClient(create_app(repo, Operator("operator", TOKEN)), client=(host, 1234))


def test_approval_config_fails_closed() -> None:
    for env in (
        {},
        {"APPROVAL_OPERATOR": "alice", "APPROVAL_TOKEN": "short"},
        {"APPROVAL_OPERATOR": "bad name", "APPROVAL_TOKEN": TOKEN},
    ):
        with pytest.raises(ValueError):
            Operator.from_env(env)


def test_approval_authentication(db: sa.Engine, pending: uuid.UUID) -> None:
    c = client(RunRepository(db))
    assert c.get("/approvals").status_code == 401
    assert c.get("/approvals", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = c.get("/approvals", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert any(row["run_id"] == str(pending) for row in response.json())
    assert (
        client(RunRepository(db), host="192.0.2.1")
        .get("/approvals", headers={"Authorization": f"Bearer {TOKEN}"})
        .status_code
        == 403
    )


def test_approval_token_not_exposed(
    db: sa.Engine, pending: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    repo = RunRepository(db)
    c = client(repo)
    response = c.post(
        f"/approvals/{pending}/decision",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"snapshot_hash": token_for(repo, pending), "decision": "approve"},
    )
    assert response.status_code == 200
    assert TOKEN not in response.text and TOKEN not in caplog.text
    assert TOKEN not in str(repo.get_events(pending))


@pytest.mark.parametrize("field", ["proposal", "schema_version", "fingerprint", "gate_result"])
def test_changed_proposal_requires_fresh_policy_and_approval(
    db: sa.Engine, pending: uuid.UUID, field: str
) -> None:
    repo = RunRepository(db)
    token = token_for(repo, pending)
    with db.begin() as conn:
        row = (
            conn.execute(sa.text("SELECT * FROM agent_proposals WHERE run_id=:id"), {"id": pending})
            .mappings()
            .one()
        )
        if field in {"proposal", "gate_result"}:
            data = dict(row[field])
            data["changed"] = True
            conn.execute(
                sa.text(
                    f"UPDATE agent_proposals SET {field}=CAST(:value AS json) WHERE run_id=:id"
                ),
                {"value": json.dumps(data), "id": pending},
            )
        else:
            conn.execute(
                sa.text(f"UPDATE agent_proposals SET {field}=:value WHERE run_id=:id"),
                {"value": "changed", "id": pending},
            )
    with pytest.raises(ApprovalConflict):
        repo.decide_approval(pending, token=token, decision="approve", actor="operator")
    assert repo.get_run(pending).state is RunState.AWAITING_APPROVAL


def test_stale_approval_rejected(db: sa.Engine, pending: uuid.UUID) -> None:
    repo = RunRepository(db)
    token = token_for(repo, pending)
    with pytest.raises(ApprovalConflict):
        repo.decide_approval(pending, token="0" * 64, decision="approve", actor="operator")
    repo.decide_approval(pending, token=token, decision="reject", actor="operator")
    with pytest.raises(ApprovalConflict):
        repo.decide_approval(pending, token=token, decision="approve", actor="operator")
    assert repo.get_run(pending).state is RunState.REJECTED
    assert repo.current_human_approval(pending) is None


def test_approved_snapshot_cannot_be_reused_after_change(db: sa.Engine, pending: uuid.UUID) -> None:
    repo = RunRepository(db)
    token = token_for(repo, pending)
    repo.decide_approval(pending, token=token, decision="approve", actor="operator")
    assert repo.current_human_approval(pending) is not None
    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE agent_proposals SET schema_version='changed' WHERE run_id=:run"),
            {"run": pending},
        )
    with pytest.raises(ApprovalConflict, match="changed"):
        repo.current_human_approval(pending)


def test_approval_is_atomic_and_durable(db: sa.Engine, pending: uuid.UUID) -> None:
    repo = RunRepository(db)
    token = token_for(repo, pending)

    def fail_event(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.startswith("INSERT INTO run_events"):
            raise RuntimeError("injected audit failure")

    sa.event.listen(db, "before_cursor_execute", fail_event)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            repo.decide_approval(pending, token=token, decision="approve", actor="operator")
    finally:
        sa.event.remove(db, "before_cursor_execute", fail_event)
    assert repo.get_run(pending).state is RunState.AWAITING_APPROVAL
    assert token_for(repo, pending) == token
    repo.decide_approval(pending, token=token, decision="approve", actor="operator")
    db.dispose()
    restarted = RunRepository(create_engine(normalize_database_url(URL)))
    assert restarted.get_run(pending).state is RunState.EVALUATING_POLICY
    assert restarted.get_events(pending)[-1].actor == "operator"
    assert restarted.get_events(pending)[-1].detail["decision"] == "approve"  # type: ignore[index]


def test_concurrent_approval_decisions(db: sa.Engine, pending: uuid.UUID) -> None:
    repo = RunRepository(db)
    token = token_for(repo, pending)

    def decide(decision: str) -> str:
        try:
            repo.decide_approval(pending, token=token, decision=decision, actor="operator")
            return "ok"
        except ApprovalConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ["approve", "reject"]))
    assert sorted(results) == ["conflict", "ok"]


def test_pending_approval_has_no_external_side_effects(
    db: sa.Engine, pending: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("approval must not execute a side effect")

    monkeypatch.setattr("engine.ports.GatedExecutor.create_coupon", forbidden)
    monkeypatch.setattr("engine.ports.GatedExecutor.send_message", forbidden)
    repo = RunRepository(db)
    assert repo.get_run(pending).state is RunState.AWAITING_APPROVAL
    response = client(repo).post(
        f"/approvals/{pending}/decision",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"snapshot_hash": token_for(repo, pending), "decision": "approve"},
    )
    assert response.json()["state"] == "evaluating_policy"


def test_request_creation_rolls_back_with_audit_failure(
    db: sa.Engine, pending: uuid.UUID, gate: Gate
) -> None:
    repo = RunRepository(db)
    repo.transition(pending, RunState.AWAITING_APPROVAL, RunState.EVALUATING_POLICY, actor="system")
    with db.begin() as conn:
        conn.execute(sa.text("DELETE FROM approval_requests WHERE run_id=:run"), {"run": pending})

    def fail_event(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.startswith("INSERT INTO run_events"):
            raise RuntimeError("injected audit failure")

    sa.event.listen(db, "before_cursor_execute", fail_event)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            repo.request_approval(pending, gate)
    finally:
        sa.event.remove(db, "before_cursor_execute", fail_event)
    assert repo.get_run(pending).state is RunState.EVALUATING_POLICY
    assert all(row["run_id"] != str(pending) for row in repo.pending_approvals())


def test_execution_rechecks_safety_gate(gate: Gate) -> None:
    proposal = OfferProposal(
        run_id="run",
        customer_id="9",
        cart_id="1",
        cart_owner_id="9",
        category="decor",
        currency="USD",
        eligible_total=Decimal("100"),
        eligible_unit_cost_total=Decimal("40"),
        proposed_discount_percent=Decimal("12"),
    )
    now = datetime.now(UTC)
    approval = gate.issue_approval(proposal, now=now, human_approved=True)
    with pytest.raises(GateRejected):
        gate.verify(approval, replace(proposal, proposed_discount_percent=Decimal("16")), now=now)
    with pytest.raises(GateRejected):
        gate.issue_approval(
            replace(proposal, proposed_discount_percent=Decimal("16")), now=now, human_approved=True
        )
    with pytest.raises(GateRejected):
        gate.issue_approval(
            replace(proposal, eligible_unit_cost_total=Decimal("80")), now=now, human_approved=True
        )
    with pytest.raises(GateRejected):
        gate.issue_approval(replace(proposal, run_id="other"), now=now, human_approved=True)
    budget_gate = Gate(
        GateConfig.from_env(
            {
                "GATE_TIMEZONE": "Asia/Riyadh",
                "GATE_CURRENCY": "USD",
                "GATE_DAILY_BUDGET": "1",
                "GATE_CATEGORY_MIN_MARGINS": '{"decor":"0.2"}',
            }
        ),
        InMemoryGateLedger(),
    )
    with pytest.raises(GateRejected):
        budget_gate.issue_approval(proposal, now=now, human_approved=True)


def test_api_conflicts_and_bad_requests(db: sa.Engine, pending: uuid.UUID) -> None:
    repo = RunRepository(db)
    c = client(repo)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    path = f"/approvals/{pending}/decision"
    assert (
        c.post(
            path, headers=headers, json={"snapshot_hash": "bad", "decision": "approve"}
        ).status_code
        == 422
    )
    assert (
        c.post(
            path, headers=headers, json={"snapshot_hash": "0" * 64, "decision": "approve"}
        ).status_code
        == 409
    )
    token = token_for(repo, pending)
    assert (
        c.post(
            path, headers=headers, json={"snapshot_hash": token, "decision": "reject"}
        ).status_code
        == 200
    )
    assert (
        c.post(
            path, headers=headers, json={"snapshot_hash": token, "decision": "approve"}
        ).status_code
        == 409
    )


def test_approval_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class Response:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        def read(self):  # type: ignore[no-untyped-def]
            return b'{"ok": true}'

    class Opener:
        def open(self, request, timeout):  # type: ignore[no-untyped-def]
            assert request.full_url == "http://127.0.0.1:8080/approvals"
            assert request.get_header("Authorization") == f"Bearer {TOKEN}"
            return Response()

    monkeypatch.setattr("engine.approval_cli.build_opener", lambda *_: Opener())
    assert cli(["list"], {"APPROVAL_OPERATOR": "operator", "APPROVAL_TOKEN": TOKEN}) == 0
    output = capsys.readouterr()
    assert '"ok": true' in output.out and TOKEN not in output.out + output.err


def test_approval_cli_to_api(
    db: sa.Engine,
    pending: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = RunRepository(db)
    test_client = client(repo)

    class Opener:
        def open(self, request, timeout):  # type: ignore[no-untyped-def]
            response = test_client.request(
                request.get_method(),
                request.full_url,
                headers=dict(request.header_items()),
                content=request.data,
            )
            if response.status_code != 200:
                from urllib.error import HTTPError

                raise HTTPError(request.full_url, response.status_code, "error", Message(), None)
            return io.BytesIO(response.content)

    monkeypatch.setattr("engine.approval_cli.build_opener", lambda *_: Opener())
    env = {"APPROVAL_OPERATOR": "operator", "APPROVAL_TOKEN": TOKEN}
    assert cli(["list"], env) == 0
    displayed = json.loads(capsys.readouterr().out)
    item = next(item for item in displayed if item["run_id"] == str(pending))
    args = ["approve", "--run-id", str(pending), "--snapshot-hash", item["snapshot_hash"]]
    assert cli(args, env) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "evaluating_policy"
    assert cli(args, env) == 1
    captured = capsys.readouterr()
    assert "HTTP 409" in captured.err and TOKEN not in captured.err + captured.out


def test_local_api_and_cli_smoke(db: sa.Engine, pending: uuid.UUID) -> None:
    from urllib.error import HTTPError, URLError
    from urllib.request import urlopen

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "ENGINE_DATABASE_URL": URL,
        "APPROVAL_OPERATOR": "operator",
        "APPROVAL_TOKEN": TOKEN,
        "APPROVAL_API_PORT": str(port),
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "engine.approval_api"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            if server.poll() is not None:
                pytest.fail("local approval API exited before readiness")
            try:
                urlopen(f"http://127.0.0.1:{port}/approvals", timeout=1)
            except HTTPError as error:
                if error.code == 401:
                    break
                raise
            except URLError:
                if time.monotonic() >= deadline:
                    pytest.fail("local approval API did not become ready")
                time.sleep(0.05)
            else:
                pytest.fail("approval endpoint accepted an unauthenticated request")
        command = [sys.executable, "-m", "engine.approval_cli"]
        listed = subprocess.run(
            [*command, "list"], env=env, capture_output=True, text=True, timeout=10, check=True
        )
        item = next(row for row in json.loads(listed.stdout) if row["run_id"] == str(pending))
        result = subprocess.run(
            [
                *command,
                "approve",
                "--run-id",
                str(pending),
                "--snapshot-hash",
                item["snapshot_hash"],
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert json.loads(result.stdout)["state"] == "evaluating_policy"
        assert TOKEN not in listed.stdout + listed.stderr + result.stdout + result.stderr
    finally:
        server.terminate()
        server.wait(timeout=10)
