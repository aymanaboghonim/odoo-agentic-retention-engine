"""The worker's automatic reconciliation stage (#132; ADR-0030 decisions 2 to 6).

Every `RECONCILE_INTERVAL_MINUTES` the worker:

1. looks up evidence for each held effect and stores it for the held-runs API;
2. applies an open admin decision when it matches the evidence (REC-6), else closes it;
3. without a decision, resolves on definitive evidence only (REC-4);
4. closes a run still unverified after `RECONCILE_DEADLINE_HOURS` as `failed`
   (`reconciled_unverified`); its coupon stays active;
5. deactivates a confirmed coupon whose message is proven `not_sent` (REC-10).

It never sends, re-queues, or re-runs an effect (REC-5): it imports no channel or execution
module (import rule REC-1). Every resolution is audited with the actor (`reconciler`, or the
admin whose decision was applied) and counted (REC-12).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from engine import telemetry
from engine.db import RECONCILER, Decision, RunRepository
from engine.reconcile import (
    Evidence,
    HeldEffect,
    ReconcileSettings,
    StageSettings,
    accept_decision,
    evidence_resolution,
)
from engine.reconcile_evidence import CouponLookup, MailpitLookup, evidence_for

LOG = logging.getLogger(__name__)


@dataclass
class StageReport:
    held: int = 0
    resolutions: dict[str, int] = field(default_factory=dict)

    def count(self, resolution: str) -> None:
        self.resolutions[resolution] = self.resolutions.get(resolution, 0) + 1
        telemetry.count("aorte.reconcile.resolutions", 1, {"aorte.resolution": resolution})


class ReconcileStage:
    def __init__(
        self,
        repo: RunRepository,
        settings: ReconcileSettings,
        stage: StageSettings,
        coupons: CouponLookup,
        mailpit: MailpitLookup,
        deactivate: Callable[[uuid.UUID], None],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._stage = stage
        self._coupons = coupons
        self._mailpit = mailpit
        self._deactivate = deactivate
        self._clock = clock
        self._last: float | None = None

    def maybe_run(self, now: datetime) -> StageReport | None:
        """Run when the interval has passed (the first call runs). Never raises."""
        tick = self._clock()
        if self._last is not None and tick - self._last < self._stage.interval_minutes * 60:
            return None
        self._last = tick
        try:
            return self.run_once(now)
        except Exception as error:  # noqa: BLE001 - the scan loop must keep running
            LOG.warning("stage=reconcile status=failed reason=%s", type(error).__name__)
            return None

    def run_once(self, now: datetime) -> StageReport:
        report = StageReport()
        stale_before = self._settings.stale_before(now)
        held = self._repo.held_effects(stale_before=stale_before, limit=500)
        report.held = len(held)
        by_run: dict[uuid.UUID, list[HeldEffect]] = {}
        for effect in held:
            by_run.setdefault(effect.run_id, []).append(effect)
        decisions = {(d.run_id, d.effect): d for d in self._repo.open_decisions()}
        for (run_id, effect_name), decision in list(decisions.items()):
            if not any(e.effect == effect_name for e in by_run.get(run_id, [])):
                self._repo.close_decision(decision, outcome="not_held", now=now)
                decisions.pop((run_id, effect_name))
        for run_id, effects in by_run.items():
            self._reconcile_run(run_id, effects, decisions, now, stale_before, report)
        for run_id in self._repo.orphan_coupons():
            try:
                self._deactivate(run_id)
            except Exception as error:  # noqa: BLE001 - retried at the next interval
                LOG.warning(
                    "stage=reconcile run_id=%s coupon_deactivation=failed reason=%s",
                    run_id,
                    type(error).__name__,
                )
                continue
            self._repo.mark_coupon_deactivated(run_id, now=now)
            report.count("coupon_deactivated")
        LOG.info(
            "stage=reconcile status=completed held=%d resolutions=%s",
            report.held,
            ",".join(f"{k}:{v}" for k, v in sorted(report.resolutions.items())) or "none",
        )
        return report

    def _reconcile_run(
        self,
        run_id: uuid.UUID,
        effects: list[HeldEffect],
        decisions: dict[tuple[uuid.UUID, str], Decision],
        now: datetime,
        stale_before: datetime,
        report: StageReport,
    ) -> None:
        resolutions: dict[str, str] = {}
        basis: dict[str, str] = {}
        evidence: dict[str, Evidence] = {}
        applied: list[Decision] = []
        actor = RECONCILER
        for held in effects:
            found = evidence_for(held, self._coupons, self._mailpit)
            evidence[held.effect] = found
            self._repo.record_evidence(run_id, held.effect, found, now=now)
            decision = decisions.get((run_id, held.effect))
            if decision is not None:
                if accept_decision(decision.decision, decision.basis, found):  # type: ignore[arg-type]
                    resolutions[held.effect] = decision.decision
                    basis[held.effect] = decision.basis
                    applied.append(decision)
                    actor = decision.actor  # the admin's identity is the audit actor
                    continue
                self._repo.close_decision(decision, outcome="rejected_mismatch", now=now)
                report.count("decision_rejected")
            value = evidence_resolution(found)
            if value is not None:
                resolutions[held.effect] = value
                basis[held.effect] = "evidence"
        deadline_before = self._stage.deadline_before(now)
        deadline = any(
            held.updated_at < deadline_before for held in effects if held.effect not in resolutions
        )
        if not resolutions and not deadline:
            return
        result = self._repo.resolve_held(
            run_id,
            resolutions,
            actor=actor,
            basis=basis,
            evidence=dict(evidence),
            stale_before=stale_before,
            deadline=deadline,
            now=now,
        )
        for decision in applied:
            outcome = "applied" if result.applied else "not_held"
            self._repo.close_decision(decision, outcome=outcome, now=now)
        if result.applied and result.resolution is not None:
            report.count(result.resolution)
