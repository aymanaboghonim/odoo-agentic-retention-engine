"""#163, OBS-19: Grafana alert rules stay quiet while the stack is stopped.

Hermetic: parses deploy/terraform/grafana/*.tf with python-hcl2.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import hcl2

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "terraform" / "grafana"
TEXT = (ROOT / "alerts.tf").read_text(encoding="utf-8")
OPTIONS = hcl2.SerializationOptions(strip_string_quotes=True, with_comments=False)


def parsed(name: str) -> dict[str, Any]:
    with (ROOT / name).open(encoding="utf-8") as handle:
        return hcl2.load(handle, serialization_options=OPTIONS)


def rules() -> dict[str, Any]:
    (locals_block,) = parsed("alerts.tf")["locals"]
    return locals_block["rules"]


def test_alert_rules_are_quiet_while_stopped() -> None:
    found = rules()
    assert set(found) == {
        "worker_scan_stale",
        "worker_missing",
        "held_effects",
        "runtime_not_active",
        "telemetry_cap",
    }
    # Every rule: no data is OK, so a stopped stack (no data at all) never fires.
    assert TEXT.count('no_data_state  = "OK"') == 1 and 'dynamic "rule"' in TEXT
    assert 'no_data_state  = "NoData"' not in TEXT and '"Alerting"' not in TEXT
    # Peer presence: worker missing needs the approval API or Alloy (ADR-0033) to be reporting.
    missing = found["worker_missing"]["expr"]
    assert "aorte_process_up_ratio" in missing and "aorte-approval-api" in missing
    assert 'node_load1{service_name="aorte-alloy"' in missing
    assert ") unless on() count(aorte_worker_last_scan_age_seconds" in missing
    # Every rule is scoped to the AWS environment, so local and eval runs never alert.
    for key, rule in found.items():
        assert 'deployment_environment_name=\\"aws-mvp\\"' in rule["expr"] or (
            "${local.env}" in rule["expr"]
        ), key
    assert re.search(r'env\s*=\s*"deployment_environment_name=\\"aws-mvp\\""', TEXT)


def test_alert_thresholds_and_test_mode_default_off() -> None:
    found = rules()
    assert found["worker_scan_stale"]["above"] == "${3 * var.scan_interval_seconds}"
    for key in ("held_effects", "telemetry_cap"):
        assert found[key]["above"] == "${local.test_floor}"
    variables = {k: v for block in parsed("variables.tf")["variable"] for k, v in block.items()}
    assert variables["alert_test_mode"]["default"] is False
    assert variables["scan_interval_seconds"]["default"] == 60
    assert "default" not in variables["alert_email"]  # never a literal address in the repo
    # One email contact point, attached on each rule; the shared notification policy is not
    # managed here (it belongs to the whole stack).
    assert "grafana_notification_policy" not in TEXT
    assert "contact_point = grafana_contact_point.email.name" in TEXT
    assert 'datasource_uid = "grafanacloud-prom"' in TEXT
