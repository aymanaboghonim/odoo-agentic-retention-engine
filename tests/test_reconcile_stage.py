"""#132: automatic reconciliation and the admin resolve API (REC-4..10, REC-12; ADR-0030).

Spec: docs/specs/execution-reconciliation.md. Held runs are synthetic rows in the throwaway
test database (local-test-db skill), removed afterwards. Odoo and Mailpit are fakes.
"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from engine import telemetry
from engine.approval_api import create_app
from engine.auth import TokenVerifier
from engine.config import normalize_database_url
from engine.coupon_code import coupon_code_for_run
from engine.db import (
    RECONCILER,
    ReconcileConflict,
    RunRepository,
    agent_proposals,
    create_engine,
    effect_message_id,
    execution_claims,
    execution_effects,
    reconcile_decisions,
    run_events,
    runs,
    upgrade_schema,
)
from engine.privacy import PrivacyRepository, PrivacySettings
from engine.reconcile import (
    ReconcileConfigError,
    ReconcileSettings,
    StageSettings,
    accept_decision,
    run_outcome,
)
from engine.reconcile_evidence import CouponLookup, MailpitLookup
from engine.reconcile_stage import ReconcileStage
from engine.runs import TERMINAL_STATES, RunState
from scripts.dev_oidc import DevIssuer

URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")

ISSUER = DevIssuer(Path(tempfile.mkdtemp(prefix="dev-oidc-reconcile-stage-")))
SETTINGS = ReconcileSettings(stale_started_minutes=30)
STAGE = StageSettings(interval_minutes=10, deadline_hours=24)
NOW = datetime.now(UTC)
HOUR_AGO = NOW - timedelta(hours=1)
DAY_AGO = NOW - timedelta(hours=25)


@pytest.fixture
def db() -> Iterator[sa.Engine]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def created(db: sa.Engine) -> Iterator[list[uuid.UUID]]:
    ids: list[uuid.UUID] = []
    yield ids
    with db.begin() as conn:
        for table in (
            "reconcile_decisions",
            "execution_effects",
            "execution_claims",
            "agent_proposals",
            "run_events",
        ):
            conn.execute(sa.text(f"DELETE FROM {table} WHERE run_id = ANY(:ids)"), {"ids": ids})
        conn.execute(runs.delete().where(runs.c.id.in_(ids)))


def held_run(
    engine: sa.Engine,
    created: list[uuid.UUID],
    *,
    channel: str,
    discount: str,
    effects: dict[str, tuple[str, datetime]],
) -> uuid.UUID:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                id=run_id,
                state=RunState.EXECUTING.value,
                trigger_type="synthetic",
                trigger_ref="9:1",
                idempotency_key=str(uuid.uuid4()),
                created_at=NOW,
                updated_at=NOW,
                retention_expires_at=NOW - timedelta(days=1),
            )
        )
        conn.execute(
            agent_proposals.insert().values(
                run_id=run_id,
                schema_version="test-v1",
                fingerprint="f" * 64,
                proposal={"execution_channel": channel, "proposed_discount_percent": discount},
                gate_result={"outcome": "allow"},
            )
        )
        conn.execute(
            execution_claims.insert().values(
                run_id=run_id, claim_id=claim_id, binding_hash="b" * 64, created_at=NOW
            )
        )
        for effect, (status, updated) in effects.items():
            conn.execute(
                execution_effects.insert().values(
                    run_id=run_id,
                    effect=effect,
                    status=status,
                    attempts=1,
                    max_attempts=1,
                    updated_at=updated,
                    reference="card-1" if status == "confirmed" else None,
                    message_id=effect_message_id(run_id, claim_id, 1)
                    if effect == "message" and channel == "mailpit"
                    else None,
                )
            )
    created.append(run_id)
    return run_id


class Odoo:
    """Fake JSON-2 calls: `cards` maps coupon codes to existence."""

    def __init__(self, *present: uuid.UUID, fail: bool = False) -> None:
        self.codes = {coupon_code_for_run(str(run)) for run in present}
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def __call__(self, model: str, method: str, params: dict[str, object]) -> object:
        self.calls.append((model, method))
        if self.fail:
            raise OSError("Odoo timed out")
        domain = params["domain"]
        assert isinstance(domain, list)
        code = domain[0][2]
        return [{"id": 1, "code": code}] if code in self.codes else []


class Mailpit:
    def __init__(self, *present: str, fail: bool = False) -> None:
        self.present = {mid.strip("<>") for mid in present}
        self.fail = fail

    def __call__(self, url: str) -> Any:
        if self.fail:
            raise OSError("Mailpit down")
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(url).query)["query"][0]
        hits = [{"MessageID": m} for m in self.present if m in query]
        return {"messages": hits, "messages_count": len(hits)}


class Deactivations:
    def __init__(self) -> None:
        self.runs: list[uuid.UUID] = []

    def __call__(self, run_id: uuid.UUID) -> None:
        self.runs.append(run_id)


def stage(db: sa.Engine, odoo: Odoo, mailpit: Mailpit, deactivate: Deactivations) -> ReconcileStage:
    return ReconcileStage(
        RunRepository(db),
        SETTINGS,
        STAGE,
        CouponLookup(odoo),
        MailpitLookup("http://mailpit:8025", fetch=mailpit),
        deactivate,
    )


def run_row(db: sa.Engine, run_id: uuid.UUID) -> Any:
    with db.connect() as conn:
        return conn.execute(sa.select(runs).where(runs.c.id == run_id)).one()


def stored_message_id(db: sa.Engine, run_id: uuid.UUID) -> str:
    with db.connect() as conn:
        return conn.execute(
            sa.select(execution_effects.c.message_id).where(
                execution_effects.c.run_id == run_id, execution_effects.c.effect == "message"
            )
        ).scalar_one()


def events(db: sa.Engine, run_id: uuid.UUID) -> list[Any]:
    with db.connect() as conn:
        return list(
            conn.execute(
                sa.select(run_events).where(run_events.c.run_id == run_id).order_by(run_events.c.id)
            ).all()
        )


def test_outcome_rules() -> None:
    assert run_outcome({"message": "confirmed"}, needs_coupon=False, deadline=False).state == (
        "completed"
    )
    assert run_outcome({"message": "unknown"}, needs_coupon=False, deadline=False).state == (
        "executing"
    )
    late = run_outcome(
        {"coupon": "confirmed", "message": "unknown"}, needs_coupon=True, deadline=True
    )
    assert (late.reason, late.orphan_coupon) == ("reconciled_unverified", False)
    lost = run_outcome(
        {"coupon": "confirmed", "message": "not_sent"}, needs_coupon=True, deadline=False
    )
    assert (lost.reason, lost.orphan_coupon) == ("reconciled_message_not_sent", True)
    never = run_outcome({"coupon": "confirmed"}, needs_coupon=True, deadline=False)
    assert (never.reason, never.orphan_coupon) == ("reconciled_message_not_sent", True)
    nocoupon = run_outcome({"coupon": "not_sent"}, needs_coupon=True, deadline=False)
    assert nocoupon.reason == "reconciled_coupon_not_created" and not nocoupon.orphan_coupon


def test_auto_resolve_only_on_definitive_evidence_or_deadline(
    db: sa.Engine, created: list[uuid.UUID]
) -> None:
    """REC-4: definitive evidence resolves; otherwise wait until the deadline, then fail."""
    sent = held_run(
        db, created, channel="mailpit", discount="0", effects={"message": ("unknown", HOUR_AGO)}
    )
    lost = held_run(
        db,
        created,
        channel="mailpit",
        discount="15",
        effects={"coupon": ("confirmed", HOUR_AGO), "message": ("unknown", HOUR_AGO)},
    )
    gram_young = held_run(
        db, created, channel="telegram", discount="0", effects={"message": ("unknown", HOUR_AGO)}
    )
    gram_old = held_run(
        db,
        created,
        channel="telegram",
        discount="15",
        effects={"coupon": ("confirmed", DAY_AGO), "message": ("unknown", DAY_AGO)},
    )
    crashed = held_run(
        db, created, channel="mailpit", discount="15", effects={"coupon": ("started", HOUR_AGO)}
    )
    mailpit = Mailpit(stored_message_id(db, sent))
    deactivated = Deactivations()
    report = stage(db, Odoo(crashed), mailpit, deactivated).run_once(NOW)

    assert (
        run_row(db, sent).state == "completed" and run_row(db, sent).outcome == "message_confirmed"
    )
    assert (run_row(db, lost).state, run_row(db, lost).failure_reason) == (
        "failed",
        "reconciled_message_not_sent",
    )
    assert run_row(db, gram_young).state == "executing"  # attestation required, before deadline
    assert (run_row(db, gram_old).state, run_row(db, gram_old).failure_reason) == (
        "failed",
        "reconciled_unverified",
    )
    # The coupon was created (present) but its message never started: proven not sent.
    assert run_row(db, crashed).failure_reason == "reconciled_message_not_sent"
    # REC-10: only proven not-sent messages lose their coupon; unverified keeps it active.
    assert sorted(map(str, deactivated.runs)) == sorted(map(str, [lost, crashed]))
    assert report.resolutions["coupon_deactivated"] == 2

    # A lookup error is unknown: nothing resolves before the deadline (REC-3, REC-4).
    flaky = held_run(
        db, created, channel="mailpit", discount="0", effects={"message": ("unknown", HOUR_AGO)}
    )
    stage(db, Odoo(fail=True), Mailpit(fail=True), Deactivations()).run_once(NOW)
    assert run_row(db, flaky).state == "executing"
    # The latest evidence is stored for the held-runs API.
    held = {
        e.run_id: e for e in RunRepository(db).held_effects(stale_before=SETTINGS.stale_before(NOW))
    }
    assert held[flaky].evidence == "unknown" and held[gram_young].evidence == "attestation_required"


def test_resolved_run_never_redispatches(db: sa.Engine, created: list[uuid.UUID]) -> None:
    """REC-5: a resolved run is never re-queued; repeated stages change nothing more."""
    lost = held_run(
        db,
        created,
        channel="mailpit",
        discount="15",
        effects={"coupon": ("confirmed", HOUR_AGO), "message": ("unknown", HOUR_AGO)},
    )
    deactivated = Deactivations()
    reconciler = stage(db, Odoo(), Mailpit(), deactivated)
    reconciler.run_once(NOW)
    with db.connect() as conn:
        before = conn.execute(
            sa.select(execution_effects).where(execution_effects.c.run_id == lost)
        ).all()
    first_events = len(events(db, lost))
    for _ in range(3):
        reconciler.run_once(NOW + timedelta(hours=30))
    with db.connect() as conn:
        after = conn.execute(
            sa.select(execution_effects).where(execution_effects.c.run_id == lost)
        ).all()
    assert after == before  # no new attempt, no status change, no new effect
    assert {row.attempts for row in after} == {1}
    assert len(events(db, lost)) == first_events
    assert deactivated.runs == [lost]  # deactivated once
    # The stage module cannot reach dispatch or execution (import rule REC-1, REC-5).
    source = Path(__file__).resolve().parents[1] / "src/engine/reconcile_stage.py"
    text = source.read_text(encoding="utf-8")
    assert "channels" not in text.split('"""', 2)[2] and "execution import" not in text


def test_resolution_requires_matching_evidence(db: sa.Engine, created: list[uuid.UUID]) -> None:
    """REC-6: confirmed needs present or an attestation; not_sent needs absent or one; an
    attestation never overrides definitive evidence to the contrary."""
    assert accept_decision("confirmed", "evidence", "present")
    assert not accept_decision("confirmed", "evidence", "unknown")
    assert accept_decision("confirmed", "attestation", "attestation_required")
    assert accept_decision("confirmed", "attestation", "unknown")
    assert not accept_decision("confirmed", "attestation", "absent")
    assert accept_decision("not_sent", "evidence", "absent")
    assert not accept_decision("not_sent", "attestation", "present")

    repo = RunRepository(db)
    gram = held_run(
        db,
        created,
        channel="telegram",
        discount="15",
        effects={"coupon": ("confirmed", HOUR_AGO), "message": ("unknown", HOUR_AGO)},
    )
    wrong = held_run(
        db, created, channel="mailpit", discount="0", effects={"message": ("unknown", HOUR_AGO)}
    )
    stale = SETTINGS.stale_before(NOW)
    repo.record_decision(
        gram,
        effect="message",
        decision="confirmed",
        basis="attestation",
        reason="operator saw it in the chat",
        actor="admin-1",
        stale_before=stale,
    )
    repo.record_decision(
        wrong,
        effect="message",
        decision="confirmed",
        basis="evidence",
        reason="should be in mailpit",
        actor="admin-1",
        stale_before=stale,
    )
    report = stage(db, Odoo(), Mailpit(), Deactivations()).run_once(NOW)
    assert run_row(db, gram).state == "completed"
    final = events(db, gram)[-1]
    assert final.actor == "admin-1" and final.detail["basis"] == {"message": "attestation"}
    # Evidence basis without matching evidence: rejected and audited; Mailpit shows absent,
    # so the evidence itself then resolves the message as not sent.
    assert report.resolutions["decision_rejected"] == 1
    assert run_row(db, wrong).failure_reason == "reconciled_message_not_sent"
    with db.connect() as conn:
        outcomes = dict(
            conn.execute(
                sa.select(reconcile_decisions.c.run_id, reconcile_decisions.c.outcome).where(
                    reconcile_decisions.c.run_id.in_([gram, wrong])
                )
            ).all()
        )
    assert outcomes == {gram: "applied", wrong: "rejected_mismatch"}
    assert any(
        e.detail.get("event") == "reconcile_decision_rejected_mismatch" for e in events(db, wrong)
    )


def test_resolve_api_requires_admin_and_only_records(
    db: sa.Engine, created: list[uuid.UUID]
) -> None:
    """REC-7: admin only, with a reason; the API records and changes no run or effect."""
    gram = held_run(
        db, created, channel="telegram", discount="0", effects={"message": ("unknown", HOUR_AGO)}
    )
    done = held_run(
        db, created, channel="telegram", discount="0", effects={"message": ("confirmed", HOUR_AGO)}
    )
    client = TestClient(
        create_app(RunRepository(db), TokenVerifier(ISSUER.settings()), reconcile=SETTINGS),
        client=("127.0.0.1", 1234),
    )

    def bearer(subject: str, role: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {ISSUER.token(subject, [role])}"}

    body = {
        "effect": "message",
        "decision": "confirmed",
        "basis": "attestation",
        "reason": "seen in chat",
    }
    path = f"/held-runs/{gram}/resolve"
    for role in ("viewer", "approver"):
        assert client.post(path, json=body, headers=bearer(f"{role}-1", role)).status_code == 403
    refusals = [e for e in events(db, gram) if e.detail.get("event") == "reconcile_resolve_refused"]
    assert [e.actor for e in refusals] == ["viewer-1", "approver-1"]
    assert (
        client.post(
            path,
            json={**body, "reason": "mail layla@example.test"},
            headers=bearer("admin-1", "admin"),
        ).status_code
        == 422
    )
    assert (
        client.post(path, json={**body, "extra": 1}, headers=bearer("admin-1", "admin")).status_code
        == 422
    )
    with db.connect() as conn:
        effect_before = conn.execute(
            sa.select(execution_effects).where(execution_effects.c.run_id == gram)
        ).one()
    ok = client.post(path, json=body, headers=bearer("admin-1", "admin"))
    assert ok.status_code == 200 and ok.json()["status"] == "recorded"
    assert (
        client.post(path, json=body, headers=bearer("admin-1", "admin")).status_code == 200
    )  # idempotent
    other = {**body, "decision": "not_sent"}
    assert client.post(path, json=other, headers=bearer("admin-1", "admin")).status_code == 409
    assert (
        client.post(
            f"/held-runs/{done}/resolve", json=body, headers=bearer("admin-1", "admin")
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/held-runs/{uuid.uuid4()}/resolve", json=body, headers=bearer("admin-1", "admin")
        ).status_code
        == 404
    )
    # Only recorded: the run and its effect are unchanged.
    assert run_row(db, gram).state == "executing"
    with db.connect() as conn:
        effect_after = conn.execute(
            sa.select(execution_effects).where(execution_effects.c.run_id == gram)
        ).one()
        open_decisions = conn.execute(
            sa.select(sa.func.count())
            .select_from(reconcile_decisions)
            .where(reconcile_decisions.c.run_id == gram)
        ).scalar_one()
    assert effect_after == effect_before and open_decisions == 1
    recorded = [
        e for e in events(db, gram) if e.detail.get("event") == "reconcile_decision_recorded"
    ]
    assert len(recorded) == 1 and recorded[0].actor == "admin-1"


def test_concurrent_resolve_is_safe(db: sa.Engine, created: list[uuid.UUID]) -> None:
    """REC-8: the run row is locked; one resolver wins and the other changes nothing."""
    run_id = held_run(
        db, created, channel="mailpit", discount="0", effects={"message": ("unknown", HOUR_AGO)}
    )
    repo = RunRepository(db)
    results: list[Any] = []
    barrier = threading.Barrier(2)

    def resolve() -> None:
        barrier.wait()
        results.append(
            repo.resolve_held(
                run_id,
                {"message": "confirmed"},
                actor=RECONCILER,
                basis={"message": "evidence"},
                evidence={"message": "present"},
                stale_before=SETTINGS.stale_before(NOW),
                deadline=False,
            )
        )

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.applied for r in results) == [False, True]
    transitions = [e for e in events(db, run_id) if e.to_state == "completed"]
    assert len(transitions) == 1
    # Not executing any more: a decision is refused; a second resolve is a no-op.
    with pytest.raises(ReconcileConflict):
        repo.record_decision(
            run_id,
            effect="message",
            decision="confirmed",
            basis="attestation",
            reason="late attestation",
            actor="admin-1",
            stale_before=SETTINGS.stale_before(NOW),
        )


def test_resolved_run_is_terminal_and_sweepable(db: sa.Engine, created: list[uuid.UUID]) -> None:
    """REC-9: resolved runs are terminal, and the retention sweep deletes them."""
    run_id = held_run(
        db, created, channel="telegram", discount="0", effects={"message": ("unknown", DAY_AGO)}
    )
    stage(db, Odoo(), Mailpit(), Deactivations()).run_once(NOW)
    assert RunState(run_row(db, run_id).state) in TERMINAL_STATES
    privacy = PrivacyRepository(db, PrivacySettings(30, "q12-test-pepper-" + "x" * 32))
    privacy.sweep_expired(now=NOW, limit=10_000)
    with db.connect() as conn:
        assert conn.execute(sa.select(runs.c.id).where(runs.c.id == run_id)).first() is None


def test_orphan_coupon_deactivated_only_when_message_not_sent(
    db: sa.Engine, created: list[uuid.UUID]
) -> None:
    """REC-10: deactivate only on a proven not-sent message; a failed call retries later."""
    lost = held_run(
        db,
        created,
        channel="mailpit",
        discount="15",
        effects={"coupon": ("confirmed", HOUR_AGO), "message": ("unknown", HOUR_AGO)},
    )
    unverified = held_run(
        db,
        created,
        channel="telegram",
        discount="15",
        effects={"coupon": ("confirmed", DAY_AGO), "message": ("unknown", DAY_AGO)},
    )

    class Flaky(Deactivations):
        def __call__(self, run_id: uuid.UUID) -> None:
            super().__call__(run_id)
            if len(self.runs) == 1:
                raise OSError("Odoo timed out")

    flaky = Flaky()
    reconciler = stage(db, Odoo(), Mailpit(), flaky)
    reconciler.run_once(NOW)
    assert RunRepository(db).orphan_coupons() == [lost]  # the first call failed: retried
    reconciler.run_once(NOW)
    assert RunRepository(db).orphan_coupons() == [] and flaky.runs == [lost, lost]
    assert run_row(db, unverified).failure_reason == "reconciled_unverified"
    assert unverified not in flaky.runs
    assert any(e.detail.get("event") == "orphan_coupon_deactivated" for e in events(db, lost))


def test_resolution_is_audited_and_counted(db: sa.Engine, created: list[uuid.UUID]) -> None:
    """REC-12: every resolution has an audit event with the actor and is counted."""
    reader = InMemoryMetricReader()
    handle = telemetry.configure(
        telemetry.TelemetrySettings(
            enabled=True,
            environment="local",
            grafana_endpoint="https://otlp.example.test/otlp",
            grafana_token="QUJD" + "x" * 40,
            daily_span_cap=1000,
        ),
        service_name="aorte-test",
        span_exporter=InMemorySpanExporter(),
        metric_reader=reader,
        log_exporter=InMemoryLogRecordExporter(),
    )
    try:
        lost = held_run(
            db,
            created,
            channel="mailpit",
            discount="15",
            effects={"coupon": ("confirmed", HOUR_AGO), "message": ("unknown", HOUR_AGO)},
        )
        stage(db, Odoo(), Mailpit(), Deactivations()).run_once(NOW)
        audit = events(db, lost)
        assert [e.actor for e in audit] == [RECONCILER] * len(audit)
        reconciled = [e for e in audit if e.detail.get("event") == "effect_reconciled"]
        assert reconciled[0].detail == {
            "event": "effect_reconciled",
            "effect": "message",
            "status": "not_sent",
            "basis": "evidence",
            "evidence": "absent",
        }
        final = [e for e in audit if e.to_state == "failed"][0]
        assert final.detail["reason"] == "reconciled_message_not_sent"
        counted: dict[str, float] = {}
        data = reader.get_metrics_data()
        for rm in data.resource_metrics if data else ():
            for scope in rm.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == "aorte.reconcile.resolutions":
                        for point in metric.data.data_points:
                            assert isinstance(point, NumberDataPoint)
                            counted[str(dict(point.attributes or {})["aorte.resolution"])] = (
                                point.value
                            )
        assert counted == {"message_not_sent": 1, "coupon_deactivated": 1}
    finally:
        handle.shutdown()


def test_stage_settings_fail_closed() -> None:
    assert StageSettings.from_env({"RECONCILE_STAGE": "NO"}) is None
    on = {
        "RECONCILE_STAGE": "YES",
        "RECONCILE_INTERVAL_MINUTES": "10",
        "RECONCILE_DEADLINE_HOURS": "24",
    }
    assert StageSettings.from_env(on) == StageSettings(10, 24)
    for broken in (
        {},
        {"RECONCILE_STAGE": "yes"},
        {**on, "RECONCILE_INTERVAL_MINUTES": "0"},
        {**on, "RECONCILE_DEADLINE_HOURS": ""},
        {**on, "RECONCILE_DEADLINE_HOURS": "721"},
    ):
        with pytest.raises(ReconcileConfigError):
            StageSettings.from_env(broken)


def test_stage_runs_on_its_interval_and_never_raises(db: sa.Engine) -> None:
    ticks = iter([0.0, 30.0, 600.0])

    class Broken(RunRepository):
        def held_effects(self, **kwargs: Any) -> Any:
            raise RuntimeError("database gone")

    reconciler = ReconcileStage(
        Broken(db),
        SETTINGS,
        STAGE,
        CouponLookup(Odoo()),
        MailpitLookup("http://mailpit:8025", fetch=Mailpit()),
        Deactivations(),
        clock=lambda: next(ticks),
    )
    assert reconciler.maybe_run(NOW) is None  # ran, failed, logged; no exception
    assert reconciler.maybe_run(NOW) is None  # 30 s later: not due
    assert reconciler.maybe_run(NOW) is None  # due again, fails again, still no exception
