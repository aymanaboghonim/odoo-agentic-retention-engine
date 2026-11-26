"""#139: the Grafana dashboard as code (deploy/terraform/grafana).

Hermetic: reads the dashboard JSON and the Terraform files as text.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "terraform" / "grafana"
TEXT = (ROOT / "dashboards" / "aorte-overview.json").read_text(encoding="utf-8")
DASH: dict[str, Any] = json.loads(TEXT)
PANELS = [p for p in DASH["panels"] if p["type"] != "row"]
PLACEHOLDER = "__LANGFUSE_TRACE_BASE__"


def by_title(fragment: str) -> dict[str, Any]:
    (panel,) = [p for p in PANELS if fragment in p["title"]]
    return panel


def queries(panel: dict[str, Any]) -> list[str]:
    return [t.get("expr") or t.get("query") or "" for t in panel["targets"]]


def test_dashboard_has_the_required_panels() -> None:
    assert DASH["uid"] == "aorte-overview" and DASH["editable"] is False
    for fragment, metric in (
        ("Runs by state", "aorte_runs"),
        ("Held effects", "aorte_execution_held"),
        ("Worker last scan age", "aorte_worker_last_scan_age_seconds"),
        ("Runtime state", "aorte_runtime_state_ratio"),
        ("LLM tokens", "aorte_llm_tokens"),
        ("Estimated LLM USD", "aorte_llm_cost_usd"),
        ("Effect outcomes", "aorte_execution_effects"),
        ("Dropped telemetry", "aorte_telemetry_dropped"),
        ("API requests per route", "http_server_request_duration"),  # #134
        ("API 5xx error ratio", "http_server_request_duration"),
        ("API P95 latency per route", "http_server_request_duration"),
        ("Host CPU busy", "node_cpu_seconds_total"),  # #165, ADR-0033
        ("Host memory available", "node_memory_MemAvailable_bytes"),
        ("Host root disk free", "node_filesystem_avail_bytes"),
        ("Container CPU", "aorte_container_cpu_time"),
        ("Container memory", "aorte_container_memory_usage"),
        ("Container network", "aorte_container_network_io"),
        ("Container restarts", "aorte_container_restarts"),
    ):
        panels = [p for p in PANELS if fragment in p["title"]]
        assert panels, fragment
        assert all(any(metric in q for q in queries(p)) for p in panels), fragment
    assert "aorte.llm.call" in queries(by_title("Run traces"))[0]


def test_queries_use_variables_and_bounded_labels() -> None:
    names = {v["name"] for v in DASH["templating"]["list"]}
    assert names == {"prom", "tempo", "env"}
    for panel in PANELS:
        assert panel["datasource"]["uid"] in {"${prom}", "${tempo}"}, panel["title"]
        for target in panel["targets"]:
            assert target["datasource"] == panel["datasource"]
        if panel["datasource"]["uid"] == "${prom}":
            for expr in queries(panel):
                assert 'deployment_environment_name="$env"' in expr, panel["title"]
                # Only closed-set labels (OBS-8); never a run or customer ID.
                for group in re.findall(r"by \(([^)]*)\)", expr):
                    for label in group.split(","):
                        assert label.strip() in {
                            "aorte_state",
                            "aorte_runtime_state",
                            "gen_ai_token_type",
                            "aorte_effect",
                            "aorte_outcome",
                            "aorte_signal",
                            "aorte_reason",
                            "http_route",
                            "le",
                            "aorte_container",
                            "aorte_direction",
                        }


def test_trace_panels_link_to_langfuse_by_full_trace_id() -> None:
    tables = [
        p
        for p in PANELS
        if p["datasource"]["uid"] == "${tempo}" and "aorte.trace_id" in queries(p)[0]
    ]
    assert len(tables) == 3
    for panel in tables:
        (query,) = queries(panel)
        assert "span.aorte.trace_id" in query  # padded ID; Tempo drops leading zeros
        (link,) = panel["fieldConfig"]["defaults"]["links"]
        assert link["url"] == PLACEHOLDER + '${__data.fields["aorte.trace_id"]}'
        assert link["targetBlank"] is True
    main = (ROOT / "main.tf").read_text(encoding="utf-8")
    assert f'"{PLACEHOLDER}"' in main
    assert "https://us.cloud.langfuse.com/project/${var.langfuse_project_id}/traces/" in main


def test_no_private_identifiers_in_the_public_files() -> None:
    # Only what git would publish: local backend.hcl, terraform.tfvars, and .tfsafe/ are
    # gitignored and must stay so.
    listed = subprocess.run(
        ["git", "ls-files", "--others", "--cached", "--exclude-standard", str(ROOT)],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    ).stdout.split()
    files = [ROOT / name for name in listed]  # git prints paths relative to cwd=ROOT
    assert files
    names = {p.name for p in files}
    assert "terraform.tfvars" not in names and "backend.hcl" not in names
    for path in files:
        if path.name == ".terraform.lock.hcl":
            continue
        text = path.read_text(encoding="utf-8")
        assert ".grafana.net" not in text, path
        assert not re.search(r"\b\d{12}\b", text), path  # AWS account ID
        assert not re.search(r"project/[a-z0-9]{20,}", text), path  # Langfuse project ID
        assert "glc_" not in text and "glsa_" not in text, path
    versions = (ROOT / "versions.tf").read_text(encoding="utf-8")
    assert re.search(r'^provider "grafana" \{\}$', versions, re.M)  # URL and token from env
    assert 'version = "= 4.55.0"' in versions


def test_slow_sql_panel_shows_statement_shape_only() -> None:
    """#134: APM spans are not in Langfuse, so the SQL table has no Langfuse link."""
    panel = by_title("Slow SQL")
    (query,) = queries(panel)
    assert "span.db.query.text" in query and "duration > 100ms" in query
    assert "parameters" not in query
    assert not panel["fieldConfig"]["defaults"].get("links")
