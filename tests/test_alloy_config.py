"""#165, OBS-14 and OBS-21: Alloy sends only allowlisted host metrics; restart policy (ADR-0033).

Hermetic: reads deploy/alloy/config.alloy, the Compose file, and deploy/terraform/mvp/ecs.tf
(python-hcl2). The live behavior (metrics from the host, memory there, a restart in place) is
checked in #133.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import hcl2

from engine import telemetry

REPO = Path(__file__).resolve().parents[1]
CONFIG = (REPO / "deploy" / "alloy" / "config.alloy").read_text(encoding="utf-8")
MVP = REPO / "deploy" / "terraform" / "mvp"
OPTIONS = hcl2.SerializationOptions(strip_string_quotes=True, with_comments=False)

ALLOWED = {
    "node_cpu_seconds_total",
    "node_memory_MemTotal_bytes",
    "node_memory_MemAvailable_bytes",
    "node_load1",
    "node_load5",
    "node_filesystem_size_bytes",
    "node_filesystem_avail_bytes",
}
# Names the unix exporter emits with these collectors (measured locally, F-098), plus the
# synthetic scrape series; only ALLOWED may pass the keep rule.
EMITTED = ALLOWED | {
    "node_cpu_guest_seconds_total",
    "node_memory_Cached_bytes",
    "node_memory_SwapFree_bytes",
    "node_load15",
    "node_filesystem_free_bytes",
    "node_filesystem_files",
    "node_filesystem_readonly",
    "node_scrape_collector_success",
    "node_scrape_collector_duration_seconds",
    "up",
    "scrape_duration_seconds",
    "scrape_samples_scraped",
}
LONG_RUNNING = set(telemetry.CONTAINERS)
ONE_SHOT = {"initialize", "model-sync"}


def _rules(component: str = "allowlist") -> list[tuple[str, list[str], str]]:
    """(action, source_labels, regex) of each keep or drop rule in one relabel component."""
    block = CONFIG[CONFIG.index(f'prometheus.relabel "{component}"') :]
    block = block[: block.index("forward_to")]
    out = []
    for body in re.findall(r"rule \{(.*?)\n  \}", block, re.S):
        action = re.search(r'action\s*=\s*"([^"]*)"', body)
        if action is None or action.group(1) not in {"keep", "drop"}:
            continue
        sources = re.search(r"source_labels = \[(.*?)\]", body)
        regex = re.search(r'regex\s*=\s*"([^"]*)"', body)
        assert sources and regex, body
        out.append((action.group(1), re.findall(r'"([^"]+)"', sources.group(1)), regex.group(1)))
    return out


def _passes(name: str, labels: dict[str, str], component: str = "allowlist") -> bool:
    """Prometheus relabel semantics for keep and drop: anchored regex over joined labels.

    Replace rules that set a temporary label are applied first, as in the config.
    """
    values = {"__name__": name, **labels}
    if component == "postgres" and re.fullmatch(
        r"pg_(database_size_bytes|stat_database_numbackends);(?:engine|odoo)",
        f"{name};{labels.get('datname', '')}",
    ):
        values["__tmp_keep"] = "yes"
    for action, sources, regex in _rules(component):
        hit = re.fullmatch(regex, ";".join(values.get(s, "") for s in sources)) is not None
        if hit != (action == "keep"):
            return False
    return True


def _ecs() -> tuple[dict[str, Any], dict[str, Any]]:
    with (MVP / "ecs.tf").open(encoding="utf-8") as handle:
        parsed = hcl2.load(handle, serialization_options=OPTIONS)
    local = {k: v for block in parsed["locals"] for k, v in block.items()}
    containers = {c["name"]: c for c in local["core_containers"]}
    (task,) = [
        r["aws_ecs_task_definition"]["core"]
        for r in parsed["resource"]
        if "aws_ecs_task_definition" in r
    ]
    return local, {"containers": containers, "task": task}


def test_alloy_config_allowlist_and_memory_limit() -> None:
    """OBS-14: only allowlisted metrics, no logs, no socket or host root, a hard memory limit."""
    # Collectors and the allowlist.
    assert 'set_collectors = ["cpu", "meminfo", "loadavg", "filesystem"]' in CONFIG
    assert {n for n in EMITTED if _passes(n, {"mode": "user", "mountpoint": "/"})} == ALLOWED
    assert not _passes("node_cpu_seconds_total", {"mode": "softirq"})
    assert not _passes("node_filesystem_size_bytes", {"mountpoint": "/var/lib/docker"})
    assert not _passes("node_filesystem_size_bytes", {"mountpoint": ""})
    assert _passes("node_filesystem_avail_bytes", {"mountpoint": "/"})
    # No logs and nothing but metrics leaves; one exporter, to the Grafana OTLP endpoint.
    for banned in ("loki.", "logging", "write_to", "docker", "pyroscope", "remote_write", "traces"):
        assert banned not in CONFIG, banned
    assert re.findall(r"^(otelcol\.exporter\.\w+|prometheus\.\w+(?:\.\w+)?) ", CONFIG, re.M) == [
        "prometheus.exporter.unix",
        "prometheus.scrape",
        "prometheus.relabel",
        "prometheus.exporter.postgres",
        "prometheus.exporter.postgres",
        "prometheus.scrape",
        "prometheus.relabel",
        "otelcol.exporter.otlphttp",
    ]
    assert 'endpoint = sys.env("GRAFANA_OTLP_ENDPOINT")' in CONFIG
    assert 'keep_keys(attributes, ["deployment.environment.name", "service.name"])' in CONFIG

    # The ECS container: hard limit, read-only mounts of /proc and /sys only, no reporting.
    local, ecs = _ecs()
    alloy = ecs["containers"]["alloy"]
    assert alloy["memory"] == "${var.alloy_memory_mib}" and "memoryReservation" not in alloy
    assert alloy["essential"] is False and alloy["readonlyRootFilesystem"] is True
    assert "privileged" not in alloy and "user" not in alloy
    assert {
        (m["sourceVolume"], m["containerPath"], m["readOnly"]) for m in alloy["mountPoints"]
    } == {
        ("host-proc", "/host/proc", True),
        ("host-sys", "/host/sys", True),
    }
    assert "--disable-reporting" in alloy["command"][0]
    assert alloy["secrets"] == [
        {"name": "GRAFANA_OTLP_TOKEN", "valueFrom": "${local.param_arn}/grafana-otlp-token"},
        {
            "name": "ENGINE_MONITOR_PASSWORD",
            "valueFrom": "${local.param_arn}/engine-monitor-password",
        },
        {"name": "ODOO_MONITOR_PASSWORD", "valueFrom": "${local.param_arn}/odoo-monitor-password"},
    ]
    assert alloy["links"] == ["engine-db:engine-db", "odoo-db:odoo-db"] and "dependsOn" not in alloy
    host_volumes = {v["name"]: v["host_path"] for v in ecs["task"]["volume"] if "host_path" in v}
    assert host_volumes == {"host-proc": "/proc", "host-sys": "/sys"}
    tf = "".join(p.read_text(encoding="utf-8") for p in MVP.glob("*.tf"))
    assert "docker.sock" not in tf and "privileged" not in tf
    variables = (MVP / "variables.tf").read_text(encoding="utf-8")
    assert re.search(r'variable "alloy_memory_mib" \{[^}]*default\s*=\s*128', variables, re.S)

    # Compose mirrors it: same image, read-only, limit, no socket, off unless asked for.
    compose = (REPO / "deploy" / "full-stack-compose.yml").read_text(encoding="utf-8")
    service = compose[compose.index("\n  alloy:\n") : compose.index("\n  proposal:\n")]
    for line in (
        "profiles: [observability]",
        "read_only: true",
        "mem_limit: 128m",
        "--disable-reporting",
        "- /proc:/host/proc:ro",
        "- /sys:/host/sys:ro",
        "- ./alloy/config.alloy:/etc/alloy/config.alloy:ro",
    ):
        assert line in service, line
    assert "docker.sock" not in compose and service.count("\n      - ") == 3


def test_restart_policy_on_long_running_containers() -> None:
    """OBS-21 (ADR-0033 decision 4): restart in place for long-running containers only."""
    local, ecs = _ecs()
    assert local["restart_policy"] == {"enabled": True, "restartAttemptPeriod": 300}
    containers = ecs["containers"]
    assert set(containers) == LONG_RUNNING | ONE_SHOT
    for name in LONG_RUNNING:
        assert containers[name].get("restartPolicy") == "${local.restart_policy}", name
    for name in ONE_SHOT:
        assert "restartPolicy" not in containers[name], name


PG_ALLOWED = {"pg_up", "pg_database_size_bytes", "pg_stat_database_numbackends"}
# Names the bundled exporter emitted locally with these collectors (F-100); only PG_ALLOWED pass.
PG_EMITTED = PG_ALLOWED | {
    "pg_stat_activity_count",
    "pg_stat_activity_max_tx_duration",
    "pg_stat_database_tup_fetched",
    "pg_database_connection_limit",
    "pg_static",
    "pg_exporter_scrapes_total",
    "pg_scrape_collector_success",
}


def test_alloy_postgres_allowlist_and_no_query_text() -> None:
    """OBS-22: up, connections, and size of the two application databases; no query text."""
    code = "\n".join(line for line in CONFIG.splitlines() if not line.strip().startswith("//"))
    for server, database in (("engine", "engine-db:5432/engine"), ("odoo", "odoo-db:5432/odoo")):
        block = CONFIG[CONFIG.index(f'prometheus.exporter.postgres "{server}"') :]
        block = block[: block.index("\n}")]
        assert "aorte_monitor:" in block and f"@{database}?sslmode=disable" in block
        assert 'enabled_collectors       = ["database", "stat_database"]' in block
        assert "disable_settings_metrics = true" in block
    for banned in (
        "stat_statements",
        "include_query",
        "custom_queries",
        "pg_monitor",
        "autodiscovery",
    ):
        assert banned not in code, banned
    labels = {"datname": "engine"}
    assert {n for n in PG_EMITTED if _passes(n, labels, "postgres")} == PG_ALLOWED
    assert _passes("pg_up", {}, "postgres")  # pg_up has no database label
    for other in ("postgres", "template0", "template1"):
        assert not _passes("pg_database_size_bytes", {"datname": other}, "postgres"), other
    assert 'regex  = "datid|datname|server|__tmp_keep"' in CONFIG  # labels never exported

    # The init container creates the role with both passwords from SSM (INF-5).
    _, ecs = _ecs()
    init = {s["name"]: s["valueFrom"] for s in ecs["containers"]["initialize"]["secrets"]}
    assert init["FULL_STACK_ENGINE_MONITOR_PASSWORD"].endswith("/engine-monitor-password")
    assert init["FULL_STACK_ODOO_MONITOR_PASSWORD"].endswith("/odoo-monitor-password")
    ssm = (MVP / "ssm.tf").read_text(encoding="utf-8")
    for name in ("engine-monitor-password", "odoo-monitor-password"):
        assert re.search(rf'"{name}"\s*= ephemeral\.random_password\.', ssm), name
