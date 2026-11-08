from __future__ import annotations

import hashlib


def coupon_code_for_run(run_id: str) -> str:
    """Derive a stable coupon code before the final message is fingerprinted."""
    if not run_id:
        raise ValueError("run ID is required for a coupon code")
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return f"RET-{digest[:32].upper()}"
