"""AWS MVP operations: status, start, shutdown, cleanup, secrets (#96; spec INF-8).

Every AWS call goes through `Aws`, which separates reads from changes. Without `--apply`
a change is printed as `WOULD ...` and never run (dry-run), so `cleanup` is safe by
default. Every command refuses the root identity and requires AWS_PROFILE. Output never
prints secret values. Rates come from the facts register (F-050, F-052, F-067); prices
that are not verified are reported as unknown.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PREFIX = os.environ.get("AORTE_PREFIX", "aorte-mvp")
REGION = os.environ.get("AWS_REGION", "us-east-1")
REPO = Path(__file__).resolve().parents[1]
TF_SAFE = os.environ.get("TF_SAFE", str(REPO / "scripts" / "tf_safe.sh"))
TF_ROOTS = {
    "mvp": REPO / "deploy" / "terraform" / "mvp",
    "bootstrap": REPO / "deploy" / "terraform" / "bootstrap",
}
HOURS_PER_MONTH = 730

# Verified rates (us-east-1). Keys name the fact row that holds the price.
RATES = {
    "t3a.medium_hour": 0.0376,  # F-050
    "public_ipv4_hour": 0.005,  # F-050
    "gp3_gb_month": 0.08,  # F-050
    "snapshot_gb_month": 0.05,  # F-050
    "ecr_gb_month": 0.10,  # F-052
    "s3_gb_month": 0.023,  # F-052
    "logs_gb_month": 0.03,  # F-067
}
GB = 1024**3
EXTERNAL_SECRETS = ("openai-api-key", "telegram-bot-token", "telegram-chat-id", "odoo-api-key")
MUTATING_VERBS = (
    "create-",
    "delete-",
    "deregister-",
    "detach-",
    "put-",
    "remove-",
    "start-",
    "stop-",
    "terminate-",
    "update-",
)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

# INF-8 (100% cleanup): every type on the INF-1 allowlist maps to the inventory kind that
# finds and deletes it; child resources go with their parent. Terraform can create only
# allowlisted types, so tests/test_aws_mvp_tooling.py::test_cleanup_covers_every_allowed_type
# makes any new resource type add cleanup support first. None: not an AWS resource.
CLEANUP_COVERAGE: dict[str, str | None] = {
    "aws_budgets_budget": "budget",
    "aws_cloudfront_distribution": "cloudfront-distribution",
    "aws_cloudfront_origin_access_control": "cloudfront-oac",
    "aws_cloudfront_vpc_origin": "cloudfront-vpc-origin",
    "aws_cloudwatch_log_group": "log-group",
    "aws_cognito_user_group": "cognito-user-pool",
    "aws_cognito_user_pool": "cognito-user-pool",
    "aws_cognito_user_pool_client": "cognito-user-pool",
    "aws_cognito_user_pool_domain": "cognito-user-pool",
    "aws_default_security_group": "vpc",
    "aws_ecr_lifecycle_policy": "ecr-repository",
    "aws_ecr_repository": "ecr-repository",
    "aws_ecs_cluster": "ecs-cluster",
    "aws_ecs_service": "ecs-service",
    "aws_ecs_task_definition": "ecs-task-definition",
    "aws_egress_only_internet_gateway": "vpc",
    "aws_iam_instance_profile": "iam-instance-profile",
    "aws_iam_openid_connect_provider": "iam-oidc-provider",
    "aws_iam_policy": "iam-role",
    "aws_iam_role": "iam-role",
    "aws_iam_role_policy": "iam-role",
    "aws_iam_role_policy_attachment": "iam-role",
    "aws_instance": "ec2-instance",  # its root gp3 volume has delete_on_termination
    "aws_internet_gateway": "vpc",
    "aws_route": "vpc",
    "aws_route_table": "vpc",
    "aws_route_table_association": "vpc",
    "aws_s3_bucket": "s3-bucket",
    "aws_s3_bucket_lifecycle_configuration": "s3-bucket",
    "aws_s3_bucket_ownership_controls": "s3-bucket",
    "aws_s3_bucket_policy": "s3-bucket",
    "aws_s3_bucket_public_access_block": "s3-bucket",
    "aws_s3_bucket_server_side_encryption_configuration": "s3-bucket",
    "aws_s3_bucket_versioning": "s3-bucket",
    "aws_s3_object": "s3-bucket",
    "aws_scheduler_schedule": "schedule",
    "aws_security_group": "vpc",
    "aws_ssm_parameter": "ssm-parameter",
    "aws_subnet": "vpc",
    "aws_vpc": "vpc",
    "aws_vpc_security_group_egress_rule": "vpc",
    "aws_vpc_security_group_ingress_rule": "vpc",
    "random_id": None,
    "random_password": None,
    "terraform_data": None,
}


class OpsError(Exception):
    """A named, expected failure: printed without a traceback, exit 1."""


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # A 2.2 GB `s3 cp` needs far longer than an API call (#97: 300 s cut an upload).
    timeout = 3600 if list(argv[1:3]) == ["s3", "cp"] else 300
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=timeout)


def log(message: str) -> None:
    print(message, flush=True)


class Aws:
    """Thin AWS CLI client. Reads always run; changes run only when apply=True."""

    def __init__(self, *, apply: bool, runner: Runner = _default_runner, region: str = REGION):
        self.apply = apply
        self.runner = runner
        self.region = region
        self.would: list[str] = []
        self.account = ""

    def _argv(self, args: Sequence[str]) -> list[str]:
        return ["aws", *args, "--region", self.region, "--output", "json"]

    def _run(self, args: Sequence[str]) -> Any:
        result = self.runner(self._argv(args))
        if result.returncode != 0:
            err = (result.stderr or "").strip()
            if any(
                s in err for s in ("Token has expired", "SSO session", "sso login", "ExpiredToken")
            ):
                profile = os.environ.get("AWS_PROFILE", "<profile>")
                raise OpsError(f"SSO session expired: run 'aws sso login --profile {profile}'")
            raise OpsError(
                f"aws {' '.join(args[:2])} failed: {err.splitlines()[-1] if err else result.returncode}"
            )
        out = (result.stdout or "").strip()
        return json.loads(out) if out else {}

    def read(self, *args: str) -> Any:
        if args[1].startswith(MUTATING_VERBS):
            raise AssertionError(f"read() called with a mutating command: {args[:2]}")
        return self._run(args)

    def try_read(self, *args: str) -> Any:
        try:
            return self.read(*args)
        except OpsError:
            return None

    def change(self, description: str, *args: str) -> Any:
        if not self.apply:
            self.would.append(description)
            log(f"  WOULD {description}")
            return None
        log(f"  DO    {description}")
        return self._run(args)


# ---------------------------------------------------------------- identity preflight


def preflight(aws: Aws) -> dict[str, str]:
    """INF-8: require a named profile and refuse the root identity before any other call."""
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    if not os.environ.get("AWS_PROFILE") and not in_ci:
        raise OpsError("set AWS_PROFILE (for example aws-mvp); refusing default credentials")
    ident = aws.read("sts", "get-caller-identity")
    arn = str(ident.get("Arn", ""))
    if arn.endswith(":root"):
        raise OpsError("refusing the root identity; use the Identity Center profile")
    if not os.environ.get("AWS_PROFILE") and f":assumed-role/{PREFIX}-gha-deploy/" not in arn:
        raise OpsError("without AWS_PROFILE only the GitHub OIDC deploy role is accepted")
    aws.account = str(ident.get("Account", ""))
    log(f"identity: {arn.rsplit('/', 1)[-1]} region {aws.region} account {ident.get('Account')}")
    return {"account": str(ident.get("Account", "")), "arn": arn}


# ---------------------------------------------------------------- inventory


@dataclass
class Item:
    kind: str
    id: str
    state: str
    scope: str  # mvp | bootstrap | account (found by an account-wide sweep)
    monthly_usd: float | None  # None: billable but price not verified, or usage-based
    note: str = ""

    @property
    def present(self) -> bool:
        return self.state not in {"terminated", "INACTIVE", "deleted"}


def _tags(tag_list: Any) -> dict[str, str]:
    return {t.get("Key", ""): t.get("Value", "") for t in (tag_list or []) if isinstance(t, dict)}


def _project(tag_list: Any) -> bool:
    return _tags(tag_list).get("Project") == PREFIX


def inventory(aws: Aws) -> list[Item]:
    items: list[Item] = []
    items += _instances(aws)
    items += _volumes_and_snapshots(aws)
    items += _network_billables(aws)
    items += _ecr(aws)
    items += _buckets(aws)
    items += _edge(aws)
    items += _ecs(aws)
    items += _logs_and_small(aws)
    return items


def _instances(aws: Aws) -> list[Item]:
    data = aws.read("ec2", "describe-instances", "--filters", f"Name=tag:Project,Values={PREFIX}")
    out = []
    for res in data.get("Reservations", []):
        for inst in res.get("Instances", []):
            state = inst.get("State", {}).get("Name", "?")
            cost: float | None = 0.0
            note = inst.get("InstanceType", "")
            if state in {"running", "pending"}:
                hourly = (
                    RATES["t3a.medium_hour"] if inst.get("InstanceType") == "t3a.medium" else None
                )
                cost = None if hourly is None else hourly * HOURS_PER_MONTH
                if inst.get("PublicIpAddress"):
                    cost = (
                        None if cost is None else cost + RATES["public_ipv4_hour"] * HOURS_PER_MONTH
                    )
                    note += " public-ipv4"
            out.append(Item("ec2-instance", inst["InstanceId"], state, "mvp", cost, note.strip()))
    return out


def _volumes_and_snapshots(aws: Aws) -> list[Item]:
    out = []
    for vol in aws.read("ec2", "describe-volumes").get("Volumes", []):
        ours = _project(vol.get("Tags"))
        if not ours and vol.get("State") != "available":
            continue  # attached volumes of other workloads are not ours to report
        out.append(
            Item(
                "ebs-volume",
                vol["VolumeId"],
                vol.get("State", "?"),
                "mvp" if ours else "account",
                vol.get("Size", 0) * RATES["gp3_gb_month"],
                f"{vol.get('Size', 0)} GiB {vol.get('VolumeType', '')}".strip(),
            )
        )
    for snap in aws.read("ec2", "describe-snapshots", "--owner-ids", "self").get("Snapshots", []):
        out.append(
            Item(
                "ebs-snapshot",
                snap["SnapshotId"],
                snap.get("State", "?"),
                "mvp" if _project(snap.get("Tags")) else "account",
                snap.get("VolumeSize", 0) * RATES["snapshot_gb_month"],
                f"<= {snap.get('VolumeSize', 0)} GiB",
            )
        )
    return out


def _network_billables(aws: Aws) -> list[Item]:
    out = []
    for addr in aws.read("ec2", "describe-addresses").get("Addresses", []):
        ident = addr.get("AllocationId") or addr.get("PublicIp", "?")
        out.append(
            Item(
                "elastic-ip",
                ident,
                "allocated",
                "account",
                RATES["public_ipv4_hour"] * HOURS_PER_MONTH,
            )
        )
    nat = aws.read(
        "ec2", "describe-nat-gateways", "--filter", "Name=state,Values=pending,available"
    )
    for gw in nat.get("NatGateways", []):
        out.append(
            Item(
                "nat-gateway",
                gw["NatGatewayId"],
                gw.get("State", "?"),
                "account",
                None,
                "unexpected (ADR-0019)",
            )
        )
    lbs = aws.read("elbv2", "describe-load-balancers") or {}
    for lb in lbs.get("LoadBalancers", []):
        out.append(
            Item(
                "load-balancer",
                lb.get("LoadBalancerName", "?"),
                "active",
                "account",
                None,
                "unexpected",
            )
        )
    for vpc in aws.read(
        "ec2", "describe-vpcs", "--filters", f"Name=tag:Project,Values={PREFIX}"
    ).get("Vpcs", []):
        out.append(Item("vpc", vpc["VpcId"], vpc.get("State", "?"), "mvp", 0.0))
    return out


def _ecr(aws: Aws) -> list[Item]:
    out = []
    for repo in aws.read("ecr", "describe-repositories").get("repositories", []):
        name = repo.get("repositoryName", "")
        if not name.startswith(f"{PREFIX}/"):
            continue
        images = aws.read("ecr", "describe-images", "--repository-name", name).get(
            "imageDetails", []
        )
        size = sum(i.get("imageSizeInBytes", 0) for i in images)
        out.append(
            Item(
                "ecr-repository",
                name,
                "present",
                "mvp",
                size / GB * RATES["ecr_gb_month"],
                f"{len(images)} images",
            )
        )
    return out


def _bucket_bytes(aws: Aws, bucket: str) -> int:
    data = aws.try_read("s3api", "list-object-versions", "--bucket", bucket) or {}
    return sum(v.get("Size", 0) for v in data.get("Versions", []) or [])


def _buckets(aws: Aws) -> list[Item]:
    out = []
    for bucket in aws.read("s3api", "list-buckets").get("Buckets", []):
        name = bucket.get("Name", "")
        if not name.startswith(f"{PREFIX}-"):
            continue
        scope = "bootstrap" if name.startswith(f"{PREFIX}-tfstate-") else "mvp"
        size = _bucket_bytes(aws, name)
        out.append(
            Item(
                "s3-bucket",
                name,
                "present",
                scope,
                size / GB * RATES["s3_gb_month"],
                f"{size} bytes",
            )
        )
    return out


def _edge(aws: Aws) -> list[Item]:
    out = []
    dist_list = aws.read("cloudfront", "list-distributions").get("DistributionList") or {}
    for dist in dist_list.get("Items") or []:
        if dist.get("Comment") != PREFIX:
            continue
        state = "enabled" if dist.get("Enabled") else "disabled"
        note = "pay-as-you-go; Always Free covers MVP traffic (F-065, ADR-0025)"
        out.append(Item("cloudfront-distribution", dist["Id"], state, "mvp", 0.0, note))
    origins = (aws.read("cloudfront", "list-vpc-origins") or {}).get("VpcOriginList") or {}
    for origin in origins.get("Items") or []:
        if str(origin.get("Name", "")).startswith(PREFIX):
            status = origin.get("Status", "?")
            out.append(Item("cloudfront-vpc-origin", origin["Id"], status, "mvp", 0.0))
    oac = (aws.read("cloudfront", "list-origin-access-controls") or {}).get(
        "OriginAccessControlList"
    ) or {}
    for control in oac.get("Items") or []:
        if str(control.get("Name", "")).startswith(PREFIX):
            out.append(Item("cloudfront-oac", control["Id"], "present", "mvp", 0.0))
    return out


def _ecs(aws: Aws) -> list[Item]:
    out = []
    clusters = aws.read("ecs", "list-clusters").get("clusterArns", [])
    ours = [c for c in clusters if c.endswith(f"/{PREFIX}")]
    if ours:
        for cluster in aws.read("ecs", "describe-clusters", "--clusters", *ours).get(
            "clusters", []
        ):
            status = cluster.get("status", "?")
            out.append(Item("ecs-cluster", cluster["clusterName"], status, "mvp", 0.0))
            if status != "ACTIVE":
                continue
            services = aws.read("ecs", "list-services", "--cluster", PREFIX).get("serviceArns", [])
            for arn in services:
                out.append(Item("ecs-service", arn.rsplit("/", 1)[-1], "present", "mvp", 0.0))
    for status in ("ACTIVE", "INACTIVE"):
        arns = aws.read(
            "ecs", "list-task-definitions", "--family-prefix", PREFIX, "--status", status
        ).get("taskDefinitionArns", [])
        for arn in arns:
            # INACTIVE revisions are free but stay listed until deleted (100% cleanup).
            out.append(
                Item("ecs-task-definition", arn.rsplit("/", 1)[-1], status.lower(), "mvp", 0.0)
            )
    return out


def _logs_and_small(aws: Aws) -> list[Item]:
    out = []
    groups = aws.read("logs", "describe-log-groups", "--log-group-name-prefix", f"/{PREFIX}")
    for group in groups.get("logGroups", []):
        stored = group.get("storedBytes", 0)
        cost = stored / GB * RATES["logs_gb_month"]
        out.append(
            Item("log-group", group["logGroupName"], "present", "mvp", cost, f"{stored} bytes")
        )
    pools = aws.read("cognito-idp", "list-user-pools", "--max-results", "60").get("UserPools", [])
    for pool in pools:
        if str(pool.get("Name", "")).startswith(PREFIX):
            note = "free tier (F-066)"
            out.append(Item("cognito-user-pool", pool["Id"], "present", "mvp", 0.0, note))
    schedules = aws.read("scheduler", "list-schedules", "--name-prefix", PREFIX) or {}
    for sched in schedules.get("Schedules", []):
        out.append(Item("schedule", sched["Name"], sched.get("State", "?"), "mvp", 0.0))
    budgets = aws.read("budgets", "describe-budgets", "--account-id", aws.account) or {}
    for budget in budgets.get("Budgets", []):
        if str(budget.get("BudgetName", "")).startswith(PREFIX):
            out.append(Item("budget", budget["BudgetName"], "present", "mvp", 0.0, "free (F-067)"))
    params = aws.read(
        "ssm",
        "describe-parameters",
        "--parameter-filters",
        f"Key=Name,Option=BeginsWith,Values=/{PREFIX}/",
    ).get("Parameters", [])
    for param in params:
        out.append(Item("ssm-parameter", param["Name"], "present", "mvp", 0.0, "standard tier"))
    for role in aws.read("iam", "list-roles").get("Roles", []):
        name = role.get("RoleName", "")
        if name.startswith(f"{PREFIX}-"):
            scope = "bootstrap" if name == f"{PREFIX}-gha-deploy" else "mvp"
            out.append(Item("iam-role", name, "present", scope, 0.0))
    for profile in aws.read("iam", "list-instance-profiles").get("InstanceProfiles", []):
        name = profile.get("InstanceProfileName", "")
        if name.startswith(f"{PREFIX}-"):
            out.append(Item("iam-instance-profile", name, "present", "mvp", 0.0))
    providers = aws.read("iam", "list-open-id-connect-providers").get(
        "OpenIDConnectProviderList", []
    )
    for provider in providers:
        arn = provider.get("Arn", "")
        if "token.actions.githubusercontent.com" not in arn:
            continue
        tags = (
            aws.try_read(
                "iam", "list-open-id-connect-provider-tags", "--open-id-connect-provider-arn", arn
            )
            or {}
        )
        if _project(tags.get("Tags")):  # never touch a GitHub provider another project shares
            out.append(Item("iam-oidc-provider", arn, "present", "bootstrap", 0.0))
    return out


def summarize(items: list[Item], include_bootstrap: bool) -> dict[str, Any]:
    """leftovers: project resources still present. account_review: untagged billables found
    by the account-wide sweep (never deleted by these scripts, but they still cost money)."""
    present = [i for i in items if i.present]
    leftovers = [
        i for i in present if i.scope == "mvp" or (include_bootstrap and i.scope == "bootstrap")
    ]
    account_review = [i for i in present if i.scope == "account"]
    known = sum(i.monthly_usd or 0.0 for i in present)
    unknown = [f"{i.kind}:{i.id}" for i in present if i.monthly_usd is None]
    return {
        "present": present,
        "leftovers": leftovers,
        "account_review": account_review,
        "monthly_known_usd": round(known, 2),
        "unknown_price": unknown,
    }


def print_status(items: list[Item], include_bootstrap: bool) -> dict[str, Any]:
    summary = summarize(items, include_bootstrap)
    if not summary["present"]:
        log("status: nothing found")
    for item in summary["present"]:
        price = "unknown" if item.monthly_usd is None else f"${item.monthly_usd:.2f}/mo"
        log(
            f"  {item.scope:<9} {item.kind:<24} {item.id:<44} {item.state:<10} {price:<12} {item.note}"
        )
    gone = [i for i in items if not i.present]
    if gone:
        log(f"  ({len(gone)} terminated/inactive records not billed)")
    log(
        f"estimated monthly at this state: ${summary['monthly_known_usd']:.2f} known"
        + (
            f" + unknown for {len(summary['unknown_price'])} item(s)"
            if summary["unknown_price"]
            else ""
        )
    )
    return summary


# ---------------------------------------------------------------- start / shutdown


def _instance(aws: Aws) -> dict[str, Any] | None:
    data = aws.read("ec2", "describe-instances", "--filters", f"Name=tag:Project,Values={PREFIX}")
    live = [
        inst
        for res in data.get("Reservations", [])
        for inst in res.get("Instances", [])
        if inst.get("State", {}).get("Name") not in {"terminated", "shutting-down"}
    ]
    if len(live) > 1:
        raise OpsError(f"expected one {PREFIX} instance, found {len(live)}")
    return live[0] if live else None


def _wait(
    what: str,
    check: Callable[[], bool],
    timeout_s: int,
    sleep: Callable[[float], None] | None = None,
) -> None:
    pause = sleep or time.sleep
    start = time.monotonic()
    while True:
        if check():
            log(f"  ready: {what} (+{int(time.monotonic() - start)}s)")
            return
        elapsed = int(time.monotonic() - start)
        if elapsed >= timeout_s:
            raise OpsError(f"timed out after {elapsed}s waiting for {what}")
        log(f"  waiting: {what} (+{elapsed}s)")
        pause(20)


def cmd_shutdown(
    aws: Aws, args: argparse.Namespace, sleep: Callable[[float], None] | None = None
) -> int:
    preflight(aws)
    inst = _instance(aws)
    if inst is None:
        log("shutdown: no instance; nothing to stop")
    elif inst["State"]["Name"] in {"stopped", "stopping"}:
        log(f"shutdown: {inst['InstanceId']} is already {inst['State']['Name']}")
    else:
        aws.change(
            f"stop instance {inst['InstanceId']}",
            "ec2",
            "stop-instances",
            "--instance-ids",
            inst["InstanceId"],
        )
        if aws.apply:
            _wait(
                "instance stopped",
                lambda: (_instance(aws) or {}).get("State", {}).get("Name") == "stopped",
                600,
                sleep,
            )
    if args.disable_edge:
        _disable_distributions(aws)
        log("note: Terraform will re-enable the distribution on the next apply (drift by design)")
    print_status(inventory(aws), include_bootstrap=True)
    return 0


def cmd_start(
    aws: Aws, args: argparse.Namespace, sleep: Callable[[float], None] | None = None
) -> int:
    preflight(aws)
    inst = _instance(aws)
    if inst is None:
        raise OpsError("no instance; apply the mvp Terraform root first")
    if inst["State"]["Name"] != "running":
        aws.change(
            f"start instance {inst['InstanceId']}",
            "ec2",
            "start-instances",
            "--instance-ids",
            inst["InstanceId"],
        )
    if not aws.apply:
        return 0
    _wait(
        "instance running",
        lambda: (_instance(aws) or {}).get("State", {}).get("Name") == "running",
        600,
        sleep,
    )

    def agent_connected() -> bool:
        arns = aws.read(
            "ecs", "list-container-instances", "--cluster", PREFIX, "--status", "ACTIVE"
        ).get("containerInstanceArns", [])
        if not arns:
            return False
        described = aws.read(
            "ecs",
            "describe-container-instances",
            "--cluster",
            PREFIX,
            "--container-instances",
            *arns,
        )
        return any(ci.get("agentConnected") for ci in described.get("containerInstances", []))

    _wait("ECS agent connected", agent_connected, 600, sleep)

    def service_steady() -> bool:
        svc = aws.read("ecs", "describe-services", "--cluster", PREFIX, "--services", "core").get(
            "services", [{}]
        )[0]
        return svc.get("desiredCount", 0) == svc.get("runningCount", -1)

    _wait("core service running == desired", service_steady, 900, sleep)
    if args.url:
        _wait(
            "approval API answers through CloudFront (401 without a token)",
            lambda: _api_answers(args.url),
            600,
            sleep,
        )
        log(f"start: ready at {args.url}")
    return 0


def _api_answers(base_url: str) -> bool:
    try:
        urllib.request.urlopen(f"{base_url.rstrip('/')}/approvals", timeout=10)  # noqa: S310 - fixed https URL
    except urllib.error.HTTPError as error:
        return error.code == 401
    except urllib.error.URLError, TimeoutError:
        return False
    return False


def _disable_distributions(aws: Aws) -> None:
    for item in _edge(aws):
        if item.kind != "cloudfront-distribution" or item.state != "enabled":
            continue
        current = aws.read("cloudfront", "get-distribution-config", "--id", item.id)
        config = current["DistributionConfig"]
        config["Enabled"] = False
        with _secret_file(json.dumps(config)) as path:
            aws.change(
                f"disable CloudFront distribution {item.id}",
                "cloudfront",
                "update-distribution",
                "--id",
                item.id,
                "--if-match",
                current["ETag"],
                "--distribution-config",
                f"file://{path}",
            )


# ---------------------------------------------------------------- cleanup


class _secret_file:
    """A 0600 temp file in a private temp dir, removed on exit (values never go in argv)."""

    def __init__(self, content: str):
        self.content = content
        self.dir: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> str:
        self.dir = tempfile.TemporaryDirectory(prefix="aorte-")
        path = Path(self.dir.name) / "value"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self.content)
        return str(path)

    def __exit__(self, *exc: object) -> None:
        if self.dir is not None:
            self.dir.cleanup()


def _empty_bucket(aws: Aws, bucket: str) -> None:
    data = aws.try_read("s3api", "list-object-versions", "--bucket", bucket) or {}
    keys = [
        {"Key": v["Key"], "VersionId": v["VersionId"]}
        for v in (data.get("Versions") or []) + (data.get("DeleteMarkers") or [])
    ]
    if not keys:
        log(f"  bucket {bucket} is empty")
        return
    for start in range(0, len(keys), 1000):
        batch = json.dumps({"Objects": keys[start : start + 1000], "Quiet": True})
        with _secret_file(batch) as path:
            aws.change(
                f"delete {len(keys[start : start + 1000])} object versions in {bucket}",
                "s3api",
                "delete-objects",
                "--bucket",
                bucket,
                "--delete",
                f"file://{path}",
            )


def _run_tf_safe(root: str, *args: str) -> None:
    result = subprocess.run(["bash", TF_SAFE, str(TF_ROOTS[root]), *args], check=False, text=True)
    if result.returncode != 0:
        raise OpsError(f"tf_safe.sh {root} {args[0]} failed (exit {result.returncode})")


def _sweep(
    aws: Aws,
    items: list[Item],
    include_bootstrap: bool,
    sleep: Callable[[float], None] | None = None,
) -> list[str]:
    """Delete every project resource left after `terraform destroy`, in dependency order.

    Only resources tagged or named for this project are deleted. Items found by the
    account-wide sweep (scope `account`) are reported for a human, never deleted.
    """
    todo = [i for i in items if i.present and (include_bootstrap or i.scope != "bootstrap")]
    by_kind: dict[str, list[Item]] = {}
    for item in todo:
        by_kind.setdefault(item.kind, []).append(item)
    ids = lambda kind: [i.id for i in by_kind.get(kind, []) if i.scope != "account"]  # noqa: E731

    # 1. Compute: ECS service and cluster first, then the instance.
    for service in ids("ecs-service"):
        aws.change(
            f"scale ECS service {service} to 0",
            "ecs",
            "update-service",
            "--cluster",
            PREFIX,
            "--service",
            service,
            "--desired-count",
            "0",
        )
        aws.change(
            f"delete ECS service {service}",
            "ecs",
            "delete-service",
            "--cluster",
            PREFIX,
            "--service",
            service,
            "--force",
        )
    for instance in ids("ec2-instance"):
        aws.change(
            f"terminate instance {instance}",
            "ec2",
            "terminate-instances",
            "--instance-ids",
            instance,
        )
        if aws.apply:
            _wait(f"instance {instance} terminated", lambda: _instance(aws) is None, 900, sleep)
    for cluster in [i.id for i in by_kind.get("ecs-cluster", []) if i.state == "ACTIVE"]:
        aws.change(f"delete ECS cluster {cluster}", "ecs", "delete-cluster", "--cluster", cluster)
    task_defs = ids("ecs-task-definition")
    for task_def in [i.id for i in by_kind.get("ecs-task-definition", []) if i.state == "active"]:
        aws.change(
            f"deregister task definition {task_def}",
            "ecs",
            "deregister-task-definition",
            "--task-definition",
            task_def,
        )
    for start in range(0, len(task_defs), 10):
        batch = task_defs[start : start + 10]
        aws.change(
            f"delete {len(batch)} task definition revision(s)",
            "ecs",
            "delete-task-definitions",
            "--task-definitions",
            *batch,
        )

    # 2. Edge: disable and delete the distribution, then the VPC origin and OAC (F-070 order).
    for dist in ids("cloudfront-distribution"):
        _delete_distribution(aws, dist, sleep)
    for origin in ids("cloudfront-vpc-origin"):
        etag = (aws.read("cloudfront", "get-vpc-origin", "--id", origin) or {}).get("ETag", "")
        aws.change(
            f"delete VPC origin {origin}",
            "cloudfront",
            "delete-vpc-origin",
            "--id",
            origin,
            "--if-match",
            etag,
        )
        if aws.apply:
            _wait(
                f"VPC origin {origin} deleted",
                lambda: aws.try_read("cloudfront", "get-vpc-origin", "--id", origin) is None,
                1200,
                sleep,
            )
    for control in ids("cloudfront-oac"):
        etag = (aws.read("cloudfront", "get-origin-access-control", "--id", control) or {}).get(
            "ETag", ""
        )
        aws.change(
            f"delete origin access control {control}",
            "cloudfront",
            "delete-origin-access-control",
            "--id",
            control,
            "--if-match",
            etag,
        )

    # 3. Network: VPC dependents, then the VPC (the CloudFront service SG goes with the VPC origin).
    for vpc in ids("vpc"):
        _delete_vpc(aws, vpc)

    # 4. Storage, images, and logs.
    for volume in [
        i.id for i in by_kind.get("ebs-volume", []) if i.scope == "mvp" and i.state == "available"
    ]:
        aws.change(f"delete volume {volume}", "ec2", "delete-volume", "--volume-id", volume)
    for snapshot in [i.id for i in by_kind.get("ebs-snapshot", []) if i.scope == "mvp"]:
        aws.change(
            f"delete snapshot {snapshot}", "ec2", "delete-snapshot", "--snapshot-id", snapshot
        )
    for repo in ids("ecr-repository"):
        aws.change(
            f"delete ECR repository {repo} and its images",
            "ecr",
            "delete-repository",
            "--repository-name",
            repo,
            "--force",
        )
    for group in ids("log-group"):
        aws.change(
            f"delete log group {group}", "logs", "delete-log-group", "--log-group-name", group
        )
    for bucket in ids("s3-bucket"):
        _empty_bucket(aws, bucket)
        aws.change(f"delete bucket {bucket}", "s3api", "delete-bucket", "--bucket", bucket)

    # 5. Identity, schedule, budget, parameters.
    for pool in ids("cognito-user-pool"):
        described = aws.try_read("cognito-idp", "describe-user-pool", "--user-pool-id", pool) or {}
        domain = (described.get("UserPool") or {}).get("Domain")
        if domain:
            aws.change(
                f"delete Cognito domain {domain}",
                "cognito-idp",
                "delete-user-pool-domain",
                "--domain",
                domain,
                "--user-pool-id",
                pool,
            )
        aws.change(
            f"delete Cognito user pool {pool}",
            "cognito-idp",
            "delete-user-pool",
            "--user-pool-id",
            pool,
        )
    for schedule in ids("schedule"):
        aws.change(
            f"delete schedule {schedule}", "scheduler", "delete-schedule", "--name", schedule
        )
    for budget in ids("budget"):
        aws.change(
            f"delete budget {budget}",
            "budgets",
            "delete-budget",
            "--account-id",
            aws.account,
            "--budget-name",
            budget,
        )
    params = ids("ssm-parameter")
    for start in range(0, len(params), 10):
        batch = params[start : start + 10]
        aws.change(
            f"delete {len(batch)} SSM parameter(s)", "ssm", "delete-parameters", "--names", *batch
        )

    # 6. IAM last: instance profiles, roles, and (bootstrap only) the GitHub OIDC provider.
    for profile in ids("iam-instance-profile"):
        roles = (
            (aws.try_read("iam", "get-instance-profile", "--instance-profile-name", profile) or {})
            .get("InstanceProfile", {})
            .get("Roles", [])
        )
        for role in roles:
            aws.change(
                f"remove role {role['RoleName']} from {profile}",
                "iam",
                "remove-role-from-instance-profile",
                "--instance-profile-name",
                profile,
                "--role-name",
                role["RoleName"],
            )
        aws.change(
            f"delete instance profile {profile}",
            "iam",
            "delete-instance-profile",
            "--instance-profile-name",
            profile,
        )
    for role in ids("iam-role"):
        _delete_role(aws, role)
    for provider in ids("iam-oidc-provider"):
        aws.change(
            f"delete OIDC provider {provider}",
            "iam",
            "delete-open-id-connect-provider",
            "--open-id-connect-provider-arn",
            provider,
        )

    return [
        f"{i.kind} {i.id}: found by the account-wide sweep, not tagged {PREFIX}; review by hand"
        for i in todo
        if i.scope == "account"
    ]


def _delete_distribution(aws: Aws, dist: str, sleep: Callable[[float], None] | None) -> None:
    current = aws.read("cloudfront", "get-distribution-config", "--id", dist)
    config = current["DistributionConfig"]
    if config.get("Enabled"):
        config["Enabled"] = False
        with _secret_file(json.dumps(config)) as path:
            aws.change(
                f"disable CloudFront distribution {dist}",
                "cloudfront",
                "update-distribution",
                "--id",
                dist,
                "--if-match",
                current["ETag"],
                "--distribution-config",
                f"file://{path}",
            )
    if aws.apply:
        _wait(
            f"distribution {dist} deployed as disabled",
            lambda: (
                (
                    aws.read("cloudfront", "get-distribution", "--id", dist).get("Distribution")
                    or {}
                ).get("Status")
                == "Deployed"
            ),
            1800,
            sleep,
        )
        current = aws.read("cloudfront", "get-distribution-config", "--id", dist)
    aws.change(
        f"delete CloudFront distribution {dist}",
        "cloudfront",
        "delete-distribution",
        "--id",
        dist,
        "--if-match",
        current.get("ETag", ""),
    )


def _delete_vpc(aws: Aws, vpc: str) -> None:
    flt = f"Name=vpc-id,Values={vpc}"
    for gw in aws.read(
        "ec2", "describe-internet-gateways", "--filters", f"Name=attachment.vpc-id,Values={vpc}"
    ).get("InternetGateways", []):
        aws.change(
            f"detach internet gateway {gw['InternetGatewayId']}",
            "ec2",
            "detach-internet-gateway",
            "--internet-gateway-id",
            gw["InternetGatewayId"],
            "--vpc-id",
            vpc,
        )
        aws.change(
            f"delete internet gateway {gw['InternetGatewayId']}",
            "ec2",
            "delete-internet-gateway",
            "--internet-gateway-id",
            gw["InternetGatewayId"],
        )
    for gw in aws.read("ec2", "describe-egress-only-internet-gateways").get(
        "EgressOnlyInternetGateways", []
    ):
        if any(a.get("VpcId") == vpc for a in gw.get("Attachments", [])):
            aws.change(
                f"delete egress-only gateway {gw['EgressOnlyInternetGatewayId']}",
                "ec2",
                "delete-egress-only-internet-gateway",
                "--egress-only-internet-gateway-id",
                gw["EgressOnlyInternetGatewayId"],
            )
    for subnet in aws.read("ec2", "describe-subnets", "--filters", flt).get("Subnets", []):
        aws.change(
            f"delete subnet {subnet['SubnetId']}",
            "ec2",
            "delete-subnet",
            "--subnet-id",
            subnet["SubnetId"],
        )
    for table in aws.read("ec2", "describe-route-tables", "--filters", flt).get("RouteTables", []):
        if any(a.get("Main") for a in table.get("Associations", [])):
            continue  # the main route table goes with the VPC
        aws.change(
            f"delete route table {table['RouteTableId']}",
            "ec2",
            "delete-route-table",
            "--route-table-id",
            table["RouteTableId"],
        )
    for group in aws.read("ec2", "describe-security-groups", "--filters", flt).get(
        "SecurityGroups", []
    ):
        if group.get("GroupName") == "default":
            continue  # deleted with the VPC
        aws.change(
            f"delete security group {group['GroupId']} ({group.get('GroupName')})",
            "ec2",
            "delete-security-group",
            "--group-id",
            group["GroupId"],
        )
    aws.change(f"delete VPC {vpc}", "ec2", "delete-vpc", "--vpc-id", vpc)


def _delete_role(aws: Aws, role: str) -> None:
    attached = aws.read("iam", "list-attached-role-policies", "--role-name", role).get(
        "AttachedPolicies", []
    )
    for policy in attached:
        aws.change(
            f"detach {policy['PolicyName']} from {role}",
            "iam",
            "detach-role-policy",
            "--role-name",
            role,
            "--policy-arn",
            policy["PolicyArn"],
        )
    for name in aws.read("iam", "list-role-policies", "--role-name", role).get("PolicyNames", []):
        aws.change(
            f"delete inline policy {name} of {role}",
            "iam",
            "delete-role-policy",
            "--role-name",
            role,
            "--policy-name",
            name,
        )
    aws.change(f"delete role {role}", "iam", "delete-role", "--role-name", role)


def _buckets_in(items: list[Item], root: str) -> list[Item]:
    # force_destroy is off for the backups and state buckets: empty them deliberately first.
    return [i for i in items if i.kind == "s3-bucket" and i.present and i.scope == root]


def cmd_cleanup(
    aws: Aws, args: argparse.Namespace, sleep: Callable[[float], None] | None = None
) -> int:
    ident = preflight(aws)
    root = "bootstrap" if args.bootstrap else "mvp"
    if aws.apply and args.confirm != ident["account"]:
        raise OpsError("--apply needs --confirm <account-id> matching the current account")
    if aws.apply and not args.sweep_only and not args.plan_id:
        raise OpsError(f"--apply needs --plan-id from the dry-run's reviewed {root} destroy plan")
    before = inventory(aws)
    mode = "sweep-only" if args.sweep_only else f"{root} root"
    log(f"cleanup ({'APPLY' if aws.apply else 'dry-run'}, {mode}); inventory before:")
    print_status(before, include_bootstrap=True)
    if args.bootstrap and summarize(before, include_bootstrap=False)["leftovers"]:
        raise OpsError("mvp resources still exist; clean the mvp root first")
    include_bootstrap = args.bootstrap

    if not aws.apply:
        if not args.sweep_only:
            log(f"step 1: saved {root} destroy plan for review (no AWS changes):")
            _run_tf_safe(root, "destroy-plan")
        log(
            "step 2 (apply only): empty buckets"
            + ("" if args.sweep_only else ", apply the reviewed destroy plan")
        )
        for bucket in _buckets_in(before, root):
            _empty_bucket(aws, bucket.id)
        log("step 3 (apply only): residual sweep; worst case if nothing was destroyed:")
        _sweep(aws, before, include_bootstrap, sleep)
        log(f"dry-run done: {len(aws.would)} change(s) would run. To apply after review:")
        flags = (" --bootstrap" if args.bootstrap else "") + (
            " --sweep-only" if args.sweep_only else ""
        )
        plan = "" if args.sweep_only else " --plan-id <plan id above>"
        log(f"  scripts/aws_mvp_cleanup.sh{flags} --apply --confirm {ident['account']}{plan}")
        return 0

    for bucket in _buckets_in(before, root):
        _empty_bucket(aws, bucket.id)
    if not args.sweep_only:
        log(f"applying the reviewed {root} destroy plan {args.plan_id}")
        _run_tf_safe(root, "apply", "--confirm", args.plan_id)
    log("residual sweep:")
    review = _sweep(aws, inventory(aws), include_bootstrap, sleep)
    final = summarize(inventory(aws), include_bootstrap)
    print_status(final["present"], include_bootstrap)
    for line in review:
        log(f"  REVIEW {line}")
    if final["leftovers"]:
        log(
            f"cleanup: {len(final['leftovers'])} project resource(s) remain; rerun cleanup --sweep-only"
        )
        return 1
    scope = "mvp and bootstrap" if include_bootstrap else "mvp (bootstrap kept)"
    log(f"cleanup: clean ({scope})")
    return 0


# ---------------------------------------------------------------- secrets


def cmd_secrets(aws: Aws, args: argparse.Namespace) -> int:
    preflight(aws)
    if args.name not in EXTERNAL_SECRETS:
        raise OpsError(f"name must be one of {', '.join(EXTERNAL_SECRETS)}")
    if args.from_file:
        value = Path(args.from_file).read_text(encoding="utf-8").strip()
    else:
        value = getpass.getpass(f"value for {args.name} (input hidden): ").strip()
    if not value or value == "unset":
        raise OpsError("empty value; nothing written")
    name = f"/{PREFIX}/{args.name}"
    with _secret_file(value) as path:
        aws.change(
            f"write SecureString {name} ({len(value)} chars, value not shown)",
            "ssm",
            "put-parameter",
            "--name",
            name,
            "--type",
            "SecureString",
            "--overwrite",
            "--value",
            f"file://{path}",
        )
    return 0


# ---------------------------------------------------------------- one-off tasks (#97)

# Commands an operator may run as a one-off proposal task (the flow of docs/runbooks/privacy-local.md).
TASK_MODULES = (
    "engine.erp_cli",
    "engine.full_stack_worker",
    "scripts.erp_minimal_fixtures",
    "scripts.ingest_policies",
)


def cmd_task(
    aws: Aws, args: argparse.Namespace, sleep: Callable[[float], None] | None = None
) -> int:
    """Run one command in a one-off proposal task, stream its logs, return its exit code."""
    preflight(aws)
    command = list(args.argv)
    if command[:1] == ["--"]:
        command = command[1:]
    if len(command) < 3 or command[:2] != ["python", "-m"] or command[2] not in TASK_MODULES:
        raise OpsError(
            f"command must be 'python -m <module> ...' with module in {', '.join(TASK_MODULES)}"
        )
    env = []
    for item in args.env or []:
        name, sep, value = item.partition("=")
        if not sep or not name.isupper() or "KEY" in name or "TOKEN" in name or "PASSWORD" in name:
            raise OpsError(f"--env {name!r}: use NAME=value for non-secret settings only")
        env.append({"name": name, "value": value})
    overrides = {
        "containerOverrides": [{"name": "proposal", "command": command, "environment": env}]
    }
    with _secret_file(json.dumps(overrides)) as path:
        started = aws.change(
            f"run one-off task: {' '.join(command)}",
            "ecs",
            "run-task",
            "--cluster",
            PREFIX,
            "--task-definition",
            f"{PREFIX}-proposal",
            "--launch-type",
            "EC2",
            "--overrides",
            f"file://{path}",
        )
    if not aws.apply:
        return 0
    tasks = (started or {}).get("tasks") or []
    if not tasks:
        raise OpsError(f"run-task started nothing: {(started or {}).get('failures')}")
    arn = tasks[0]["taskArn"]
    task_id = arn.rsplit("/", 1)[-1]
    holder: dict[str, Any] = {}

    def stopped() -> bool:
        holder["task"] = aws.read("ecs", "describe-tasks", "--cluster", PREFIX, "--tasks", arn)[
            "tasks"
        ][0]
        return holder["task"].get("lastStatus") == "STOPPED"

    _wait(f"task {task_id} stopped", stopped, args.timeout, sleep)
    for container in ("model-sync", "proposal"):
        events = (
            aws.try_read(
                "logs",
                "get-log-events",
                "--log-group-name",
                f"/{PREFIX}/ecs",
                "--log-stream-name",
                f"{container}/{container}/{task_id}",
                "--start-from-head",
            )
            or {}
        )
        for event in events.get("events", []):
            log(f"  [{container}] {event.get('message', '')}")
    exits = {c["name"]: c.get("exitCode") for c in holder["task"].get("containers", [])}
    log(
        f"task {task_id}: exit codes {exits}; stop reason: {holder['task'].get('stoppedReason', '')}"
    )
    code = exits.get("proposal")
    return code if isinstance(code, int) else 1


HF_BASE = "https://huggingface.co"


def _model_constants() -> tuple[str, str, list[str]]:
    sys.path.insert(0, str(REPO / "src"))
    from engine.retrieval import MODEL_FILES, MODEL_REPOSITORY, MODEL_REVISION

    return MODEL_REPOSITORY, MODEL_REVISION, list(MODEL_FILES)


def model_snapshot_key(repository: str, revision: str) -> str:
    """S3 key prefix that mirrors the Hugging Face cache layout engine.retrieval reads."""
    return f"models/hub/models--{repository.replace('/', '--')}/snapshots/{revision}"


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(url: str, target: Path, label: str) -> None:
    started = time.monotonic()
    last = started
    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as out:  # noqa: S310
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        for chunk in iter(lambda: response.read(1 << 20), b""):
            out.write(chunk)
            done += len(chunk)
            if time.monotonic() - last >= 15:
                last = time.monotonic()
                share = f" {done * 100 // total}%" if total else ""
                log(f"  downloading {label}:{share} {done >> 20} MiB (+{int(last - started)}s)")
    log(f"  downloaded {label}: {done >> 20} MiB (+{int(time.monotonic() - started)}s)")


def cmd_model_upload(aws: Aws, args: argparse.Namespace) -> int:
    """Stage the pinned embedding model in S3 for the IPv6-only host (F-072).

    Runs on an operator machine with IPv4. Downloads the pinned revision, checks every Git
    LFS file against the SHA-256 that Hugging Face publishes, writes manifest.sha256, and
    uploads (dry run unless --apply). The model-sync container re-checks the manifest.
    """
    preflight(aws)
    repository, revision, files = _model_constants()
    bucket = args.bucket
    if not bucket.startswith(f"{PREFIX}-backups-"):
        raise OpsError(f"--bucket must be the {PREFIX} backups bucket (terraform output)")
    key = model_snapshot_key(repository, revision)
    tree_url = f"{HF_BASE}/api/models/{repository}/tree/{revision}"
    with urllib.request.urlopen(tree_url, timeout=30) as response:  # noqa: S310
        tree = {entry["path"]: entry for entry in json.load(response)}
    missing = [name for name in files if name not in tree]
    if missing:
        raise OpsError(f"pinned revision lacks files: {missing}")
    total = sum(int(tree[name].get("size", 0)) for name in files)
    log(
        f"model {repository}@{revision[:12]}: {len(files)} files, {total / 1e9:.2f} GB -> s3://{bucket}/{key}/"
    )
    with tempfile.TemporaryDirectory(prefix="aorte-model-") as tmp:
        work = Path(tmp)
        lines = []
        for name in files:
            target = work / name
            _fetch(f"{HF_BASE}/{repository}/resolve/{revision}/{name}", target, name)
            digest = _sha256(target)
            expected = (tree[name].get("lfs") or {}).get("oid")
            if expected and expected != digest:
                raise OpsError(f"{name}: SHA-256 does not match the published LFS hash")
            if target.stat().st_size != int(tree[name].get("size", -1)):
                raise OpsError(f"{name}: size does not match the published size")
            lines.append(f"{digest}  {name}\n")
        (work / "manifest.sha256").write_text("".join(lines), encoding="utf-8")
        log("  verified: LFS files match published SHA-256; all sizes match")
        for name in [*files, "manifest.sha256"]:
            aws.change(
                f"upload {name} to s3://{bucket}/{key}/",
                "s3",
                "cp",
                str(work / name),
                f"s3://{bucket}/{key}/{name}",
                "--only-show-errors",
            )
    log(
        "model-upload: done"
        if aws.apply
        else f"model-upload dry run: {len(aws.would)} upload(s) would run"
    )
    return 0


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AWS MVP operations (#96). Dry-run unless --apply."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="read-only billable inventory")
    status.add_argument("--json", action="store_true")
    status.add_argument("--expect-clean", action="store_true", help="exit 1 if anything is left")
    status.add_argument(
        "--include-bootstrap", action="store_true", help="count bootstrap resources as leftovers"
    )
    shut = sub.add_parser("shutdown", help="stop the instance (idempotent)")
    shut.add_argument("--apply", action="store_true")
    shut.add_argument(
        "--disable-edge",
        action="store_true",
        help="also disable CloudFront (drifts from Terraform)",
    )
    start = sub.add_parser("start", help="start the instance and wait until the API answers")
    start.add_argument("--apply", action="store_true")
    start.add_argument("--url", help="API base URL (terraform output api_base_url)")
    clean = sub.add_parser("cleanup", help="dry-run first teardown with a reviewed destroy plan")
    clean.add_argument("--apply", action="store_true")
    clean.add_argument("--confirm", help="account id; required with --apply")
    clean.add_argument("--plan-id", help="plan id printed by the dry-run; required with --apply")
    clean.add_argument(
        "--bootstrap", action="store_true", help="tear down the bootstrap root (last)"
    )
    clean.add_argument(
        "--sweep-only",
        action="store_true",
        help="skip Terraform (lost or broken state): delete every tagged or named project resource",
    )
    secrets = sub.add_parser("secrets", help="write one external secret to SSM")
    secrets.add_argument("name", choices=EXTERNAL_SECRETS)
    secrets.add_argument("--from-file", help="read the value from this file (else prompt)")
    secrets.add_argument("--apply", action="store_true")
    task = sub.add_parser(
        "task", help="run a one-off proposal task (erp_cli, fixtures, worker, ingest)"
    )
    task.add_argument("--apply", action="store_true")
    task.add_argument("--env", action="append", metavar="NAME=value", help="non-secret override")
    task.add_argument("--timeout", type=int, default=1800)
    task.add_argument("argv", nargs=argparse.REMAINDER, help="-- python -m <module> ...")
    model = sub.add_parser("model-upload", help="stage the pinned embedding model in S3 (F-072)")
    model.add_argument("--bucket", required=True, help="terraform output backups_bucket")
    model.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None, runner: Runner = _default_runner) -> int:
    args = build_parser().parse_args(argv)
    aws = Aws(apply=getattr(args, "apply", False), runner=runner)
    started = time.monotonic()
    try:
        if args.command == "status":
            preflight(aws)
            summary = summarize(inventory(aws), args.include_bootstrap)
            if args.json:
                print(
                    json.dumps(
                        {
                            **summary,
                            "present": [asdict(i) for i in summary["present"]],
                            "leftovers": [asdict(i) for i in summary["leftovers"]],
                            "account_review": [asdict(i) for i in summary["account_review"]],
                        },
                        indent=2,
                    )
                )
            else:
                print_status(summary["present"], args.include_bootstrap)
            dirty = summary["leftovers"] or summary["account_review"]
            return 1 if args.expect_clean and dirty else 0
        handlers = {
            "shutdown": cmd_shutdown,
            "start": cmd_start,
            "cleanup": cmd_cleanup,
            "secrets": cmd_secrets,
            "model-upload": cmd_model_upload,
            "task": cmd_task,
        }
        return handlers[args.command](aws, args)
    except OpsError as error:
        print(
            f"FAILED ({args.command}, +{int(time.monotonic() - started)}s): {error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
