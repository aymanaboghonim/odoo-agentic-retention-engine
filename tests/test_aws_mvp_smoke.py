"""#97 smoke script: check logic with faked HTTP and AWS (hermetic)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from scripts import aws_mvp_smoke as smoke

SSO = "arn:aws:sts::000000000000:assumed-role/AWSReservedSSO_Admin/aws-mvp"


def fake_aws(public_ip: str | None = None, extra_rule: dict[str, Any] | None = None):
    rules = [
        {"IsEgress": False, "FromPort": 8080, "ToPort": 8080, "PrefixListId": "pl-3b927c52"},
        {"IsEgress": True},
    ]
    if extra_rule:
        rules.append(extra_rule)
    instance = {"InstanceId": "i-1", "SecurityGroups": [{"GroupId": "sg-1"}]}
    if public_ip:
        instance["PublicIpAddress"] = public_ip

    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        key = " ".join(argv[1:3])
        table = {
            "sts get-caller-identity": {"Account": "000000000000", "Arn": SSO},
            "ec2 describe-instances": {"Reservations": [{"Instances": [instance]}]},
            "ec2 describe-security-group-rules": {"SecurityGroupRules": rules},
        }
        return subprocess.CompletedProcess(list(argv), 0, json.dumps(table.get(key, {})), "")

    return runner


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str, str | None], int]:
    responses: dict[tuple[str, str, str | None], int] = {}

    def http(
        method: str, url: str, token: str | None = None, body: Any = None
    ) -> tuple[int, dict[str, str], str]:
        if url.startswith("http://"):
            return 301, {"Location": url.replace("http://", "https://")}, ""
        path = url.split(".cloudfront.net", 1)[1]
        if path == "/":
            return 200, {}, "<p>Operator UI is planned for MVP Tier 3.</p>"
        if path == "/api/approvals":
            return {None: 401, "viewer": 200, "approver": 200}[token], {}, ""
        if path.endswith("/decision"):
            return {"viewer": 403, "approver": 409}[token or "viewer"], {}, ""
        return 404, {}, ""

    monkeypatch.setenv("AWS_PROFILE", "aws-mvp")
    monkeypatch.setattr(smoke, "http", http)
    return responses


def tokens(tmp_path: Path) -> list[str]:
    (tmp_path / "v").write_text("viewer")
    (tmp_path / "a").write_text("approver")
    return ["--viewer-token", str(tmp_path / "v"), "--approver-token", str(tmp_path / "a")]


def test_read_only_smoke_passes_checks_it_can_run(tmp_path: Path, web: Any) -> None:
    out = tmp_path / "report"
    code = smoke.main(
        ["--base-url", "https://d1.cloudfront.net", "--out", str(out), *tokens(tmp_path)],
        runner=fake_aws(),
    )
    results = json.loads((out / "results.json").read_text())
    status = {c["id"]: c["status"] for c in results["checks"]}
    assert status == {
        "SM-1": "pass",
        "SM-2": "pass",
        "SM-3": "not-run",
        "SM-4": "pass",
        "SM-5": "not-run",
    }
    assert code == 1  # not-run is not a pass
    summary = (out / "summary.md").read_text()
    assert "| SM-4 | Cognito RBAC | pass |" in summary and "viewer" not in summary.split("|")[-1]


def test_smoke_fails_on_public_ip_or_extra_ingress(tmp_path: Path, web: Any) -> None:
    for runner in (
        fake_aws(public_ip="203.0.113.10"),
        fake_aws(
            extra_rule={"IsEgress": False, "FromPort": 22, "ToPort": 22, "CidrIpv4": "0.0.0.0/0"}
        ),
    ):
        out = tmp_path / "r"
        smoke.main(["--base-url", "https://d1.cloudfront.net", "--out", str(out)], runner=runner)
        status = {
            c["id"]: c["status"] for c in json.loads((out / "results.json").read_text())["checks"]
        }
        assert status["SM-2"] == "fail"


def test_smoke_rejects_non_cloudfront_url(tmp_path: Path, web: Any) -> None:
    assert (
        smoke.main(["--base-url", "http://example.com", "--out", str(tmp_path)], runner=fake_aws())
        == 1
    )
