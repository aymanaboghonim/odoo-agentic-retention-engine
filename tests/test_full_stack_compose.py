from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any


def compose_config() -> dict[str, Any]:
    compose_file = Path(__file__).resolve().parents[1] / "deploy/full-stack-compose.yml"
    env = {
        **os.environ,
        "FULL_STACK_ODOO_DB_PASSWORD": "test",
        "FULL_STACK_ODOO_RO_PASSWORD": "test",
        "FULL_STACK_ENGINE_DB_PASSWORD": "test",
        "FULL_STACK_ENGINE_DATABASE_URL": "postgresql://engine:test@engine-db:5432/engine",
        "FULL_STACK_ODOO_READONLY_DATABASE_URL": "postgresql://odoo_ro:test@odoo-db:5432/odoo",
        "FULL_STACK_INSTANCE_ID": str(uuid.uuid4()),
        "OIDC_ISSUER": "https://dev-oidc.example.test",
        "OIDC_CLIENT_ID": "dev-client",
        "OIDC_ROLES_CLAIM": "cognito:groups",
        "FULL_STACK_OIDC_DIR": "/tmp",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "poc68-config-test",
            "-f",
            str(compose_file),
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_approval_api_is_unpublished_and_private() -> None:
    config = compose_config()
    services = config["services"]
    assert not services["approval-api"].get("ports")
    assert set(services["approval-api"]["networks"]) == {"private"}
    assert config["networks"]["private"]["internal"] is True


def test_web_ui_ports_bind_only_to_loopback() -> None:
    services = compose_config()["services"]
    for service in ("odoo", "mailpit"):
        assert services[service]["ports"]
        assert all(port["host_ip"] == "127.0.0.1" for port in services[service]["ports"])
