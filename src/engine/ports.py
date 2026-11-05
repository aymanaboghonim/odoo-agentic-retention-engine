from __future__ import annotations

from datetime import datetime
from typing import Protocol

from engine.policy import Gate, GateApproval, OfferProposal


class CouponCreator(Protocol):
    def create(self, approval: GateApproval, proposal: OfferProposal) -> str: ...


class MessageDispatcher(Protocol):
    def send(self, approval: GateApproval, proposal: OfferProposal) -> str: ...


class GatedExecutor:
    """The only path to coupon creation and customer dispatch; each call re-checks the gate."""

    def __init__(self, gate: Gate, coupons: CouponCreator, dispatcher: MessageDispatcher) -> None:
        self._gate = gate
        self._coupons = coupons
        self._dispatcher = dispatcher

    def create_coupon(
        self, approval: GateApproval, proposal: OfferProposal, *, now: datetime
    ) -> str:
        self._gate.verify(approval, proposal, now=now)
        return self._coupons.create(approval, proposal)

    def send_message(
        self, approval: GateApproval, proposal: OfferProposal, *, now: datetime
    ) -> str:
        self._gate.verify(approval, proposal, now=now)
        return self._dispatcher.send(approval, proposal)
