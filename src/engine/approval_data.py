from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from engine.policy import OfferProposal


class InvalidProposalSnapshot(Exception):
    """Stored proposal cannot be safely reconstructed."""


def offer_from_record(record: dict[str, Any]) -> OfferProposal:
    try:
        string_fields = (
            "run_id",
            "customer_id",
            "cart_id",
            "cart_owner_id",
            "category",
            "currency",
        )
        if any(not isinstance(record[name], str) for name in string_fields):
            raise ValueError("invalid string")
        if not isinstance(record["is_bulk"], bool):
            raise ValueError("invalid bulk flag")
        language = record.get("language", "en")
        if language not in {"en", "ar"}:
            raise ValueError("invalid language")
        message = record["customer_message"]
        if message is not None and not isinstance(message, str):
            raise ValueError("invalid message")

        def decimal_field(name: str, *, optional: bool) -> Decimal | None:
            value = record[name]
            if value is None and optional:
                return None
            if not isinstance(value, str):
                raise ValueError("invalid decimal")
            result = Decimal(value)
            if not result.is_finite():
                raise ValueError("nonfinite decimal")
            return result

        total = decimal_field("eligible_total", optional=False)
        if total is None:
            raise ValueError("missing total")
        return OfferProposal(
            run_id=record["run_id"],
            customer_id=record["customer_id"],
            cart_id=record["cart_id"],
            cart_owner_id=record["cart_owner_id"],
            category=record["category"],
            currency=record["currency"],
            eligible_total=total,
            eligible_unit_cost_total=decimal_field("eligible_unit_cost_total", optional=True),
            proposed_discount_percent=decimal_field("proposed_discount_percent", optional=True),
            is_bulk=record["is_bulk"],
            customer_message=message,
            language=language,
        )
    except (KeyError, ValueError, TypeError, InvalidOperation) as error:
        raise InvalidProposalSnapshot("invalid stored proposal") from error


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
