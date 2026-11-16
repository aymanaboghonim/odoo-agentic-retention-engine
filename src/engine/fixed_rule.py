"""Zero-LLM deterministic fixed-rule proposal (#87, ADR-0022, spec EV-5 and KS-4).

The fallback is a plain reminder with a 0% discount. It is built as an ordinary
`OfferProposal` and goes through the same `Gate`, consent, allowlist, PII tokens, and
approval path as a model proposal. A 0% offer cannot exceed any gate limit.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Literal

from engine.odoo import AbandonedCart, cart_to_proposal_inputs
from engine.policy import OfferProposal

FIXED_SCHEMA = "fixed-rule-v1"
Language = Literal["en", "ar"]

# Name-free on purpose: a cart without a customer name must still get a valid reminder.
FIXED_REMINDERS: Mapping[str, str] = {
    "en": "Your cart is still waiting for you. Come back whenever you are ready.",
    "ar": "سلتك ما زالت بانتظارك. عد إليها متى كنت جاهزا.",
}


def build_fixed_offer(
    run_id: uuid.UUID, cart: AbandonedCart, language: Language, *, message: str | None = None
) -> OfferProposal:
    """Build the fixed 0% reminder for a cart. `message` lets a caller use its own template."""
    if language not in FIXED_REMINDERS:
        raise ValueError("fixed reminders exist only for en and ar")
    if not cart.lines:
        raise ValueError("a fixed reminder needs a cart with lines")
    total, cost = cart_to_proposal_inputs(cart)
    return OfferProposal(
        run_id=str(run_id),
        customer_id=str(cart.partner_id),
        cart_id=str(cart.order_id),
        cart_owner_id=str(cart.partner_id),
        category=cart.lines[0].category,
        currency=cart.currency,
        eligible_total=total,
        eligible_unit_cost_total=cost,
        proposed_discount_percent=Decimal(0),
        customer_message=FIXED_REMINDERS[language] if message is None else message,
        language=language,
    )
