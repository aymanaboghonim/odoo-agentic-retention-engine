from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.poc3_local_demo import (
    DEFAULT_HTML_PATH,
    DISCLAIMER,
    build_demo_bundle,
    main,
    render_cli,
    render_html,
)


def test_poc3_demo_scenarios_cover_all_gate_outcomes_and_coupon_rules() -> None:
    scenarios = build_demo_bundle()
    assert len(scenarios) == 3

    by_id = {s.scenario_id: s for s in scenarios}
    auto_case = by_id["en-auto-reminder"]
    assert auto_case.gate_outcome == "auto_approve"
    assert auto_case.coupon_code is None
    assert auto_case.channel == "mailpit"
    assert auto_case.direction == "ltr"

    approval_case = by_id["ar-human-approval-12pct"]
    assert approval_case.gate_outcome == "needs_approval"
    assert approval_case.gate_reasons == ("discount_in_approval_band",)
    assert approval_case.approval_status == "approved"
    assert approval_case.coupon_code is not None
    assert approval_case.coupon_code.startswith("RET-")
    assert approval_case.language == "ar"
    assert approval_case.direction == "rtl"
    assert approval_case.channel == "telegram"

    reject_case = by_id["en-hard-reject-90pct"]
    assert reject_case.gate_outcome == "reject"
    assert reject_case.gate_reasons == ("discount_above_hard_cap",)
    assert reject_case.coupon_code is None
    assert reject_case.channel == "none"
    assert reject_case.execution_status == "blocked_by_safety_gate"

    for s in scenarios:
        assert s.customer_email.endswith("@example.test")
        assert len(s.gate_fingerprint) == 64
        assert len(s.snapshot_hash) == 64


def test_poc3_demo_html_is_offline_self_contained_and_matches_committed_file() -> None:
    scenarios = build_demo_bundle()
    rendered = render_html(scenarios)

    assert DISCLAIMER in rendered
    assert 'dir="rtl"' in rendered
    assert "Abandoned Cart Scan" in rendered
    assert "Policy Retrieval &amp; Proposal" in rendered
    assert "Deterministic Safety Gate" in rendered
    assert "Operator Approval Binding" in rendered
    assert "Worker Execution &amp; Channel Dispatch Record" in rendered

    # Strictly offline and static: no external URLs, CDNs, or network fetches.
    assert "http://" not in rendered.lower()
    assert "https://" not in rendered.lower()
    assert not re.search(r"<script[^>]+src=", rendered, re.IGNORECASE)
    assert not re.search(r"<link[^>]+href=", rendered, re.IGNORECASE)
    assert "fetch(" not in rendered

    assert Path(DEFAULT_HTML_PATH).exists()
    assert main(["--check-html", str(DEFAULT_HTML_PATH)]) == 0


def test_poc3_demo_cli_and_json_modes(capsys: pytest.CaptureFixture[str]) -> None:
    scenarios = build_demo_bundle()
    text = render_cli(scenarios)
    assert DISCLAIMER in text
    assert "1. Scan / Cart" in text
    assert "5. Execution" in text

    assert main([]) == 0
    capsys.readouterr()
    assert main(["--json"]) == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["disclaimer"] == DISCLAIMER
    assert len(parsed["scenarios"]) == 3
    assert main(["--scenario", "unknown-id"]) == 2
