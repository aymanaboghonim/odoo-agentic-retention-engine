"""Live smoke for the deployed AWS MVP (#97; spec docs/specs/aws-mvp-deployment.md).

Checks (each maps to a #97 done-when item):
  SM-1 HTTPS through CloudFront serves the UI; plain HTTP redirects to HTTPS.
  SM-2 No public path to the host: no public IPv4; one ingress rule, API port from the
       CloudFront prefix list only.
  SM-3 Origin guard: the API refuses a request without X-Origin-Verify on the host itself
       (403), and an unauthenticated request through CloudFront gets 401.
  SM-4 Cognito RBAC: viewer may read, may not decide; approver may decide (#86).
  SM-5 Odoo readiness: `odoo_ro` is read-only and `website_sale_loyalty` is installed
       (the erp_minimal_fixtures preflight, run in the operator container, ADR-0027).

SM-3 (host command) and SM-5 (one-off task) start something, so they run only with
--apply. The end-to-end retention flow is run step by step (docs/runbooks/aws-mvp.md),
because `erp_cli execute` needs an exact human confirmation. Writes summary.md and
results.json to --out. Never prints tokens.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.aws_mvp import PREFIX, Aws, OpsError, Runner, _default_runner, cmd_exec, preflight  # noqa: E402


@dataclass
class Check:
    id: str
    name: str
    status: str  # pass | fail | not-run
    detail: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def http(
    method: str, url: str, token: str | None = None, body: dict[str, Any] | None = None
) -> tuple[int, dict[str, str], str]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=20) as response:
            return (
                response.status,
                dict(response.headers),
                response.read(2000).decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read(2000).decode("utf-8", "replace")


def sm1_edge(base: str) -> Check:
    code, _h, body = http("GET", f"{base}/")
    http_code, headers, _b = http("GET", base.replace("https://", "http://", 1) + "/")
    location = headers.get("Location", "")
    ok = (
        code == 200
        and "Operator UI" in body
        and http_code == 301
        and location.startswith("https://")
    )
    return Check(
        "SM-1",
        "HTTPS via CloudFront; HTTP redirects",
        "pass" if ok else "fail",
        f"https {code}; http {http_code} -> {location[:40]}",
    )


def sm2_no_public_path(aws: Aws) -> Check:
    data = aws.read(
        "ec2",
        "describe-instances",
        "--filters",
        f"Name=tag:Project,Values={PREFIX}",
        "Name=instance-state-name,Values=running,stopped",
    )
    instances = [i for r in data.get("Reservations", []) for i in r.get("Instances", [])]
    if len(instances) != 1:
        return Check(
            "SM-2",
            "no public path to the host",
            "fail",
            f"expected 1 instance, found {len(instances)}",
        )
    inst = instances[0]
    groups = [g["GroupId"] for g in inst.get("SecurityGroups", [])]
    rules = aws.read(
        "ec2",
        "describe-security-group-rules",
        "--filters",
        f"Name=group-id,Values={','.join(groups)}",
    ).get("SecurityGroupRules", [])
    ingress = [r for r in rules if not r.get("IsEgress")]
    only_prefix = (
        len(ingress) == 1
        and ingress[0].get("PrefixListId")
        and ingress[0].get("FromPort") == ingress[0].get("ToPort")
        and ingress[0].get("FromPort") != 22
    )
    ok = not inst.get("PublicIpAddress") and bool(only_prefix)
    sources = [
        (r.get("FromPort"), r.get("PrefixListId") or r.get("CidrIpv4") or r.get("CidrIpv6"))
        for r in ingress
    ]
    detail = f"public ipv4: {inst.get('PublicIpAddress') or 'none'}; ingress rules: {sources}"
    return Check("SM-2", "no public path to the host", "pass" if ok else "fail", detail)


def sm3_origin_guard(aws: Aws, base: str, instance_id: str) -> Check:
    code, _h, _b = http("GET", f"{base}/api/approvals")
    if not aws.apply:
        return Check(
            "SM-3",
            "origin guard and auth",
            "not-run",
            f"CloudFront without token: {code}; host check needs --apply",
        )
    cmd = aws.change(
        "run a local curl on the host (no X-Origin-Verify)",
        "ssm",
        "send-command",
        "--instance-ids",
        instance_id,
        "--document-name",
        "AWS-RunShellScript",
        "--parameters",
        'commands=["curl -s -o /dev/null -w %{http_code} http://127.0.0.1:8080/api/approvals"]',
    )
    command_id = (cmd or {}).get("Command", {}).get("CommandId", "")
    host_code = ""
    for _ in range(20):
        time.sleep(3)
        inv = (
            aws.try_read(
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
            )
            or {}
        )
        if inv.get("Status") in {"Success", "Failed"}:
            host_code = inv.get("StandardOutputContent", "").strip()
            break
    ok = code == 401 and host_code == "403"
    return Check(
        "SM-3",
        "origin guard and auth",
        "pass" if ok else "fail",
        f"CloudFront without token: {code}; host without X-Origin-Verify: {host_code or 'no result'}",
    )


def sm4_rbac(base: str, viewer: str | None, approver: str | None) -> Check:
    if not viewer or not approver:
        return Check(
            "SM-4",
            "Cognito RBAC",
            "not-run",
            "pass --viewer-token and --approver-token files (scripts/cognito_login.py)",
        )
    decision = {"snapshot_hash": "0" * 64, "decision": "reject"}
    run = uuid.uuid4()
    v_read, _h, _b = http("GET", f"{base}/api/approvals", viewer)
    v_decide, _h, _b = http("POST", f"{base}/api/approvals/{run}/decision", viewer, decision)
    a_decide, _h, _b = http("POST", f"{base}/api/approvals/{run}/decision", approver, decision)
    # An approver passes the role check and reaches the (empty) approval: 409, not 401/403.
    ok = v_read == 200 and v_decide == 403 and a_decide == 409
    return Check(
        "SM-4",
        "Cognito RBAC",
        "pass" if ok else "fail",
        f"viewer read {v_read}, viewer decide {v_decide}, approver decide {a_decide}",
    )


def sm5_odoo(aws: Aws) -> Check:
    if not aws.apply:
        return Check(
            "SM-5",
            "odoo_ro read-only and website_sale_loyalty",
            "not-run",
            "operator command needs --apply",
        )
    args = argparse.Namespace(
        argv=["python", "-m", "scripts.erp_minimal_fixtures", "--preflight-only"],
        env=[],
        timeout=1800,
    )
    code = cmd_exec(aws, args)
    return Check(
        "SM-5",
        "odoo_ro read-only and website_sale_loyalty",
        "pass" if code == 0 else "fail",
        f"erp_minimal_fixtures --preflight-only exit {code}",
    )


def write_report(out: Path, checks: list[Check], meta: dict[str, str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(
        json.dumps({"meta": meta, "checks": [asdict(c) for c in checks]}, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# AWS MVP smoke",
        "",
        f"Run: {meta['started']} (UTC), mode {meta['mode']}. Raw data: [results.json](results.json).",
        "",
        "| Check | What | Result | Detail |",
        "| :-- | :-- | :-- | :-- |",
        *[f"| {c.id} | {c.name} | {c.status} | {c.detail} |" for c in checks],
        "",
        "Limits: the end-to-end retention flow is run separately (runbook); "
        "this run does not measure cost or latency.",
        "",
    ]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None, runner: Runner = _default_runner) -> int:
    parser = argparse.ArgumentParser(description="AWS MVP live smoke (#97)")
    parser.add_argument("--base-url", required=True, help="terraform output cloudfront_url")
    parser.add_argument("--viewer-token", type=Path)
    parser.add_argument("--approver-token", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="report directory")
    parser.add_argument(
        "--apply", action="store_true", help="also run SM-3 host check and SM-5 task"
    )
    args = parser.parse_args(argv)
    aws = Aws(apply=args.apply, runner=runner)
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = args.base_url.rstrip("/")
    try:
        preflight(aws)
        if not base.startswith("https://") or not base.endswith(".cloudfront.net"):
            raise OpsError("--base-url must be the https://<id>.cloudfront.net output")
        checks = [sm1_edge(base), sm2_no_public_path(aws)]
        instance = aws.read(
            "ec2",
            "describe-instances",
            "--filters",
            f"Name=tag:Project,Values={PREFIX}",
            "Name=instance-state-name,Values=running",
        )
        ids = [
            i["InstanceId"]
            for r in instance.get("Reservations", [])
            for i in r.get("Instances", [])
        ]
        checks.append(sm3_origin_guard(aws, base, ids[0] if ids else ""))
        read = lambda p: p.read_text(encoding="utf-8").strip() if p else None  # noqa: E731
        checks.append(sm4_rbac(base, read(args.viewer_token), read(args.approver_token)))
        checks.append(sm5_odoo(aws))
    except OpsError as error:
        print(f"FAILED (smoke): {error}", file=sys.stderr)
        return 1
    write_report(
        args.out, checks, {"started": started, "mode": "apply" if args.apply else "read-only"}
    )
    for check in checks:
        print(f"{check.id} {check.status:<8} {check.name}: {check.detail}")
    print(f"report: {args.out / 'summary.md'}")
    return 0 if all(c.status == "pass" for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
