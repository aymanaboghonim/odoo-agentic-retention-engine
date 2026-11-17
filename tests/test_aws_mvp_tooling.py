"""INF-8: AWS MVP ops tooling (scripts/aws_mvp.py). Hermetic: a stateful fake AWS CLI.

The fake account holds one of every resource the Terraform roots create. Delete calls
remove them, so a sweep is proven complete by `status --expect-clean` passing.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from scripts import aws_mvp
from scripts.aws_mvp import CLEANUP_COVERAGE, MUTATING_VERBS, main
from scripts.check_terraform_policy import ALLOWED_RESOURCE_TYPES

REPO = Path(__file__).resolve().parents[1]
ACCOUNT = "000000000000"
SSO_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_Admin/aws-mvp"
OIDC_ARN = f"arn:aws:iam::{ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"
TAG = [{"Key": "Project", "Value": "aorte-mvp"}]


def ok(argv: list[str], value: Any = None) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, json.dumps(value if value is not None else {}), "")


def err(argv: list[str], message: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 255, "", message)


def opt(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


class FakeAccount:
    """One of every project resource, plus unrelated ones that must never be touched."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.arn = SSO_ARN
        self.instances = {"i-0live": "running", "i-0old": "terminated"}
        self.volumes = {
            "vol-root": ("in-use", True),
            "vol-other": ("in-use", False),
            "vol-orphan": ("available", True),
        }
        self.snapshots = {"snap-1": True}
        self.vpcs = {"vpc-1"}
        self.vpc_children = {"igw-1", "eigw-1", "subnet-1", "rtb-1", "sg-host", "sg-cf"}
        self.repos = {"aorte-mvp/engine", "someone-else/app"}
        self.buckets = {
            "aorte-mvp-backups-1": [("a", "v1")],
            "aorte-mvp-ui-1": [],
            "aorte-mvp-tfstate-1": [("s", "v2")],
            "unrelated": [("x", "v3")],
        }
        self.distribution = {"enabled": True}
        self.vpc_origins = {"vo-1"}
        self.oacs = {"oac-1"}
        self.log_groups = {"/aorte-mvp/ecs"}
        self.cluster = "ACTIVE"
        self.services = {"core"}
        self.task_defs = {
            "aorte-mvp-core:1": "ACTIVE",
            "aorte-mvp-proposal:1": "ACTIVE",
            "aorte-mvp-core:0": "INACTIVE",
        }
        self.pools = {"us-east-1_X"}
        self.schedules = {"aorte-mvp-nightly-stop"}
        self.budgets = {"aorte-mvp-monthly"}
        self.params = {"/aorte-mvp/openai-api-key", "/aorte-mvp/engine-database-url"}
        self.roles = {
            "aorte-mvp-instance": ["AmazonSSM"],
            "aorte-mvp-ecs-exec": [],
            "aorte-mvp-gha-deploy": [],
            "other": [],
        }
        self.inline = {
            "aorte-mvp-ecs-exec": ["read-own-parameters"],
            "aorte-mvp-gha-deploy": ["deploy"],
        }
        self.profiles = {"aorte-mvp-instance": ["aorte-mvp-instance"], "other-profile": []}
        self.oidc = {OIDC_ARN}

    # -- reads -------------------------------------------------------------
    def read(self, argv: list[str], key: str) -> subprocess.CompletedProcess[str]:  # noqa: C901
        d = self.distribution
        reads: dict[str, Any] = {
            "sts get-caller-identity": {"Account": ACCOUNT, "Arn": self.arn},
            "ec2 describe-instances": {
                "Reservations": [
                    {
                        "Instances": [
                            {"InstanceId": i, "InstanceType": "t3a.medium", "State": {"Name": st}}
                            for i, st in self.instances.items()
                        ]
                    }
                ]
            },
            "ec2 describe-volumes": {
                "Volumes": [
                    {
                        "VolumeId": v,
                        "State": st,
                        "Size": 10,
                        "VolumeType": "gp3",
                        "Tags": TAG if ours else [],
                    }
                    for v, (st, ours) in self.volumes.items()
                ]
            },
            "ec2 describe-snapshots": {
                "Snapshots": [
                    {"SnapshotId": s, "State": "completed", "VolumeSize": 10, "Tags": TAG}
                    for s in self.snapshots
                ]
            },
            "ec2 describe-addresses": {"Addresses": []},
            "ec2 describe-nat-gateways": {"NatGateways": []},
            "elbv2 describe-load-balancers": {"LoadBalancers": []},
            "ec2 describe-vpcs": {"Vpcs": [{"VpcId": v, "State": "available"} for v in self.vpcs]},
            "ec2 describe-internet-gateways": {
                "InternetGateways": [{"InternetGatewayId": "igw-1"}]
                if "igw-1" in self.vpc_children
                else []
            },
            "ec2 describe-egress-only-internet-gateways": {
                "EgressOnlyInternetGateways": [
                    {"EgressOnlyInternetGatewayId": "eigw-1", "Attachments": [{"VpcId": "vpc-1"}]}
                ]
                if "eigw-1" in self.vpc_children
                else []
            },
            "ec2 describe-subnets": {
                "Subnets": [{"SubnetId": "subnet-1"}] if "subnet-1" in self.vpc_children else []
            },
            "ec2 describe-route-tables": {
                "RouteTables": [{"RouteTableId": "rtb-main", "Associations": [{"Main": True}]}]
                + (
                    [{"RouteTableId": "rtb-1", "Associations": []}]
                    if "rtb-1" in self.vpc_children
                    else []
                )
            },
            "ec2 describe-security-groups": {
                "SecurityGroups": [{"GroupId": "sg-default", "GroupName": "default"}]
                + [
                    {"GroupId": g, "GroupName": g}
                    for g in ("sg-host", "sg-cf")
                    if g in self.vpc_children
                ]
            },
            "ecr describe-repositories": {
                "repositories": [{"repositoryName": r} for r in self.repos]
            },
            "ecr describe-images": {"imageDetails": [{"imageSizeInBytes": 2 * 1024**3}]},
            "s3api list-buckets": {"Buckets": [{"Name": b} for b in self.buckets]},
            "cloudfront list-distributions": {
                "DistributionList": {
                    "Items": [{"Id": "E1", "Comment": "aorte-mvp", "Enabled": d["enabled"]}]
                    if d
                    else []
                }
            },
            "cloudfront get-distribution-config": {
                "ETag": "etag-1",
                "DistributionConfig": {"Enabled": bool(d and d["enabled"])},
            },
            "cloudfront get-distribution": {"Distribution": {"Status": "Deployed"}},
            "cloudfront list-vpc-origins": {
                "VpcOriginList": {
                    "Items": [
                        {"Id": v, "Name": "aorte-mvp-api", "Status": "Deployed"}
                        for v in self.vpc_origins
                    ]
                }
            },
            "cloudfront list-origin-access-controls": {
                "OriginAccessControlList": {
                    "Items": [{"Id": o, "Name": "aorte-mvp-ui"} for o in self.oacs]
                }
            },
            "cloudfront get-origin-access-control": {"ETag": "etag-oac"},
            "logs describe-log-groups": {
                "logGroups": [{"logGroupName": g, "storedBytes": 1024} for g in self.log_groups]
            },
            "ecs list-clusters": {"clusterArns": ["arn:aws:ecs:us-east-1:0:cluster/aorte-mvp"]},
            "ecs describe-clusters": {
                "clusters": [{"clusterName": "aorte-mvp", "status": self.cluster}]
            },
            "ecs list-services": {
                "serviceArns": [
                    f"arn:aws:ecs:us-east-1:0:service/aorte-mvp/{s}" for s in self.services
                ]
            },
            "cognito-idp list-user-pools": {
                "UserPools": [{"Id": p, "Name": "aorte-mvp-operators"} for p in self.pools]
            },
            "cognito-idp describe-user-pool": {"UserPool": {"Domain": "aorte-mvp-abcd"}},
            "scheduler list-schedules": {
                "Schedules": [{"Name": s, "State": "ENABLED"} for s in self.schedules]
            },
            "budgets describe-budgets": {"Budgets": [{"BudgetName": b} for b in self.budgets]},
            "ssm describe-parameters": {"Parameters": [{"Name": p} for p in self.params]},
            "iam list-roles": {"Roles": [{"RoleName": r} for r in self.roles]},
            "iam list-instance-profiles": {
                "InstanceProfiles": [{"InstanceProfileName": p} for p in self.profiles]
            },
            "iam list-open-id-connect-providers": {
                "OpenIDConnectProviderList": [{"Arn": a} for a in self.oidc]
            },
            "iam list-open-id-connect-provider-tags": {"Tags": TAG},
        }
        if key == "s3api list-object-versions":
            versions = self.buckets.get(opt(argv, "--bucket"), [])
            return ok(
                argv,
                {
                    "Versions": [{"Key": k, "VersionId": v, "Size": 100} for k, v in versions],
                    "DeleteMarkers": [],
                },
            )
        if key == "ecs list-task-definitions":
            status = opt(argv, "--status")
            return ok(
                argv,
                {
                    "taskDefinitionArns": [
                        f"arn:task-definition/{t}"
                        for t, st in self.task_defs.items()
                        if st == status
                    ]
                },
            )
        if key == "cloudfront get-vpc-origin":
            return (
                ok(argv, {"ETag": "etag-vo"})
                if opt(argv, "--id") in self.vpc_origins
                else err(argv, "NoSuchVpcOrigin")
            )
        if key == "iam get-instance-profile":
            return ok(
                argv,
                {
                    "InstanceProfile": {
                        "Roles": [
                            {"RoleName": r}
                            for r in self.profiles.get(opt(argv, "--instance-profile-name"), [])
                        ]
                    }
                },
            )
        if key == "iam list-attached-role-policies":
            return ok(
                argv,
                {
                    "AttachedPolicies": [
                        {"PolicyName": n, "PolicyArn": f"arn:policy/{n}"}
                        for n in self.roles.get(opt(argv, "--role-name"), [])
                    ]
                },
            )
        if key == "iam list-role-policies":
            return ok(argv, {"PolicyNames": self.inline.get(opt(argv, "--role-name"), [])})
        if key in reads:
            value = reads[key]
            return value if isinstance(value, subprocess.CompletedProcess) else ok(argv, value)
        return err(argv, f"unexpected call {key}")

    # -- changes -----------------------------------------------------------
    def change(self, argv: list[str], op: str) -> None:  # noqa: C901
        o = lambda name: opt(argv, name)  # noqa: E731
        if op == "stop-instances":
            self.instances[o("--instance-ids")] = "stopped"
        elif op == "start-instances":
            self.instances[o("--instance-ids")] = "running"
        elif op == "terminate-instances":
            self.instances[o("--instance-ids")] = "terminated"
            self.volumes.pop("vol-root", None)  # delete_on_termination
        elif op == "delete-service":
            self.services.discard(o("--service"))
        elif op == "delete-cluster":
            self.cluster = "INACTIVE"
        elif op == "deregister-task-definition":
            self.task_defs[o("--task-definition")] = "INACTIVE"
        elif op == "delete-task-definitions":
            for t in argv[argv.index("--task-definitions") + 1 : argv.index("--region")]:
                self.task_defs.pop(t, None)
        elif op == "update-distribution":
            self.distribution["enabled"] = False
        elif op == "delete-distribution":
            assert not self.distribution["enabled"], "must disable before delete"
            self.distribution = {}
        elif op == "delete-vpc-origin":
            assert not self.distribution, "VPC origin is in use by a distribution (F-070)"
            self.vpc_origins.discard(o("--id"))
            self.vpc_children.discard("sg-cf")  # the service-managed SG goes with it
        elif op == "delete-origin-access-control":
            self.oacs.discard(o("--id"))
        elif op in {
            "delete-internet-gateway",
            "delete-egress-only-internet-gateway",
            "delete-subnet",
            "delete-route-table",
            "delete-security-group",
        }:
            ident = argv[4]
            assert ident != "sg-default"
            self.vpc_children.discard(ident)
        elif op == "delete-vpc":
            assert not self.vpc_children, f"VPC still has {self.vpc_children}"
            self.vpcs.discard(o("--vpc-id"))
        elif op == "delete-volume":
            self.volumes.pop(o("--volume-id"))
        elif op == "delete-snapshot":
            self.snapshots.pop(o("--snapshot-id"))
        elif op == "delete-repository":
            self.repos.discard(o("--repository-name"))
        elif op == "delete-log-group":
            self.log_groups.discard(o("--log-group-name"))
        elif op == "delete-objects":
            self.buckets[o("--bucket")] = []
        elif op == "delete-bucket":
            assert not self.buckets[o("--bucket")], "bucket not empty"
            self.buckets.pop(o("--bucket"))
        elif op == "delete-user-pool":
            self.pools.discard(o("--user-pool-id"))
        elif op == "delete-schedule":
            self.schedules.discard(o("--name"))
        elif op == "delete-budget":
            self.budgets.discard(o("--budget-name"))
        elif op == "delete-parameters":
            for name in argv[argv.index("--names") + 1 : argv.index("--region")]:
                self.params.discard(name)
        elif op == "remove-role-from-instance-profile":
            self.profiles[o("--instance-profile-name")].remove(o("--role-name"))
        elif op == "delete-instance-profile":
            assert not self.profiles[o("--instance-profile-name")], "profile still has roles"
            self.profiles.pop(o("--instance-profile-name"))
        elif op == "detach-role-policy":
            self.roles[o("--role-name")] = []
        elif op == "delete-role-policy":
            self.inline[o("--role-name")] = []
        elif op == "delete-role":
            name = o("--role-name")
            assert not self.roles[name] and not self.inline.get(name), f"{name} has policies"
            self.roles.pop(name)
        elif op == "delete-open-id-connect-provider":
            self.oidc.discard(o("--open-id-connect-provider-arn"))

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        op = argv[2]
        if op.startswith(MUTATING_VERBS):
            self.change(argv, op)
            return ok(argv)
        return self.read(argv, f"{argv[1]} {op}")

    def mutations(self) -> list[str]:
        return [f"{c[1]} {c[2]}" for c in self.calls if c[2].startswith(MUTATING_VERBS)]

    def touched(self) -> str:
        return " ".join(" ".join(c) for c in self.calls if c[2].startswith(MUTATING_VERBS))


@pytest.fixture(autouse=True)
def profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_PROFILE", "aws-mvp")
    monkeypatch.setattr(aws_mvp.time, "sleep", lambda _s: None)


@pytest.fixture
def tf_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(aws_mvp, "_run_tf_safe", lambda root, *args: calls.append((root, *args)))
    return calls


# ---------------------------------------------------------------- coverage


def test_cleanup_covers_every_allowed_type() -> None:
    # INF-8: anything Terraform may create (INF-1 allowlist) has a cleanup path.
    assert set(ALLOWED_RESOURCE_TYPES) == set(CLEANUP_COVERAGE)
    used = set()
    for path in (REPO / "deploy" / "terraform").rglob("*.tf"):
        if ".terraform" not in path.parts:
            used |= set(
                re.findall(r'^resource "([a-z0-9_]+)"', path.read_text(encoding="utf-8"), re.M)
            )
    assert used <= set(CLEANUP_COVERAGE)
    kinds = {k for k in CLEANUP_COVERAGE.values() if k}
    swept = (REPO / "scripts" / "aws_mvp.py").read_text(encoding="utf-8")
    for kind in kinds:
        assert f'"{kind}"' in swept, kind


def test_full_sweep_reaches_clean_and_spares_unrelated(tf_calls: list[tuple[str, ...]]) -> None:
    fake = FakeAccount()
    assert main(["status", "--expect-clean"], runner=fake) == 1
    assert main(["cleanup", "--sweep-only", "--apply", "--confirm", ACCOUNT], runner=fake) == 0
    assert tf_calls == []  # sweep-only never runs Terraform
    assert main(["status", "--expect-clean"], runner=fake) == 0  # 100% of the mvp scope
    assert (
        "aorte-mvp-tfstate-1" in fake.buckets and "aorte-mvp-gha-deploy" in fake.roles
    )  # bootstrap kept
    assert (
        main(
            ["cleanup", "--bootstrap", "--sweep-only", "--apply", "--confirm", ACCOUNT], runner=fake
        )
        == 0
    )
    assert main(["status", "--expect-clean", "--include-bootstrap"], runner=fake) == 0
    # Unrelated resources are never touched.
    assert fake.buckets == {"unrelated": [("x", "v3")]}
    assert (
        fake.repos == {"someone-else/app"}
        and "other" in fake.roles
        and "other-profile" in fake.profiles
    )
    assert "vol-other" in fake.volumes and "vol-other" not in fake.touched()
    assert "sg-default" not in fake.touched()


def test_full_cleanup_with_terraform_then_sweep(tf_calls: list[tuple[str, ...]]) -> None:
    fake = FakeAccount()
    code = main(["cleanup", "--apply", "--confirm", ACCOUNT, "--plan-id", "abc123"], runner=fake)
    assert tf_calls == [("mvp", "apply", "--confirm", "abc123")]
    assert code == 0  # whatever the destroy left (here: everything), the sweep removed
    assert main(["status", "--expect-clean"], runner=fake) == 0


# ---------------------------------------------------------------- status


def test_status_reports_billables_and_skips_inactive(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeAccount()
    assert main(["status", "--json"], runner=fake) == 0
    report = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    kinds = {(i["kind"], i["id"]) for i in report["present"]}
    assert ("ec2-instance", "i-0live") in kinds
    assert ("ec2-instance", "i-0old") not in kinds  # terminated: not billed (F-070)
    assert ("ebs-volume", "vol-orphan") in kinds
    assert ("ebs-volume", "vol-other") not in kinds  # attached and not ours
    assert ("ecr-repository", "someone-else/app") not in kinds
    for kind in (
        "budget",
        "ssm-parameter",
        "cloudfront-oac",
        "ecs-task-definition",
        "iam-instance-profile",
        "iam-oidc-provider",
        "ecs-service",
    ):
        assert any(k == kind for k, _ in kinds), kind
    assert report["unknown_price"] == []  # CloudFront: Always Free at MVP traffic (F-065)
    assert report["monthly_known_usd"] > 27  # running t3a.medium (F-050) dominates
    assert fake.mutations() == []


def test_status_expect_clean_counts_account_billables() -> None:
    fake = FakeAccount()
    assert main(["cleanup", "--sweep-only", "--apply", "--confirm", ACCOUNT], runner=fake) == 0
    assert main(["status", "--expect-clean"], runner=fake) == 0
    fake.volumes["vol-stray"] = ("available", False)  # someone else's unattached volume
    assert main(["status", "--expect-clean"], runner=fake) == 1  # still billing: flagged
    assert main(["cleanup", "--sweep-only", "--apply", "--confirm", ACCOUNT], runner=fake) == 0
    assert "vol-stray" in fake.volumes  # reported for review, never deleted
    assert "vol-stray" not in fake.touched()


# ---------------------------------------------------------------- guards


def test_refuses_root_and_default_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = FakeAccount()
    root.arn = f"arn:aws:iam::{ACCOUNT}:root"
    for command in (
        ["status"],
        ["shutdown", "--apply"],
        ["cleanup"],
        ["secrets", "odoo-api-key", "--apply"],
    ):
        assert main(command, runner=root) == 1
    assert root.mutations() == []
    assert "root identity" in capsys.readouterr().err
    monkeypatch.delenv("AWS_PROFILE")
    fake = FakeAccount()
    assert main(["status"], runner=fake) == 1
    assert fake.calls == []  # refused before any AWS call


def test_expired_sso_names_the_login_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeAccount()
    expired = "Error when retrieving token from sso: Token has expired"
    monkeypatch.setattr(fake, "read", lambda argv, key: err(argv, expired))
    assert main(["status"], runner=fake) == 1
    assert "aws sso login --profile aws-mvp" in capsys.readouterr().err


def test_cleanup_dry_run_makes_no_changes(
    tf_calls: list[tuple[str, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeAccount()
    assert main(["cleanup"], runner=fake) == 0
    assert fake.mutations() == []
    assert tf_calls == [("mvp", "destroy-plan")]
    out = capsys.readouterr().out
    for expected in (
        "WOULD delete ECR repository aorte-mvp/engine",
        "WOULD delete log group /aorte-mvp/ecs",
        "WOULD delete CloudFront distribution E1",
        "WOULD delete VPC vpc-1",
        "WOULD delete budget aorte-mvp-monthly",
    ):
        assert expected in out
    assert "aorte-mvp-tfstate-1" not in out.split("step 2", 1)[1]  # bootstrap bucket untouched
    sweep = FakeAccount()
    assert main(["cleanup", "--sweep-only"], runner=sweep) == 0
    assert sweep.mutations() == [] and tf_calls == [("mvp", "destroy-plan")]


def test_cleanup_apply_requires_matching_confirm_and_plan_id(
    tf_calls: list[tuple[str, ...]],
) -> None:
    for args in (
        ["--apply"],
        ["--apply", "--confirm", "111111111111", "--plan-id", "abc"],
        ["--apply", "--confirm", ACCOUNT],
        ["--sweep-only", "--apply"],
    ):
        fake = FakeAccount()
        assert main(["cleanup", *args], runner=fake) == 1
        assert fake.mutations() == []
    assert tf_calls == []


def test_bootstrap_cleanup_refused_while_mvp_exists(
    tf_calls: list[tuple[str, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["cleanup", "--bootstrap"], runner=FakeAccount()) == 1
    assert "clean the mvp root first" in capsys.readouterr().err
    assert tf_calls == []


# ---------------------------------------------------------------- start / shutdown / secrets


def test_shutdown_is_idempotent() -> None:
    fake = FakeAccount()
    assert main(["shutdown"], runner=fake) == 0  # dry-run
    assert fake.mutations() == []
    assert main(["shutdown", "--apply"], runner=fake) == 0
    assert fake.mutations() == ["ec2 stop-instances"]
    assert main(["shutdown", "--apply"], runner=fake) == 0
    assert fake.mutations() == ["ec2 stop-instances"]  # already stopped: no second call


def test_start_dry_run_makes_no_changes() -> None:
    fake = FakeAccount()
    fake.instances["i-0live"] = "stopped"
    assert main(["start"], runner=fake) == 0
    assert fake.mutations() == []


def test_secrets_never_put_value_in_argv_or_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "sk-not-a-real-key-0123456789"
    source = tmp_path / "key"
    source.write_text(secret + "\n", encoding="utf-8")
    seen_files: list[str] = []
    fake = FakeAccount()

    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        if argv[2] == "put-parameter":
            path = Path(opt(argv, "--value").removeprefix("file://"))
            assert path.read_text(encoding="utf-8") == secret
            assert (path.stat().st_mode & 0o777) == 0o600
            seen_files.append(str(path))
        return fake(argv)

    assert (
        main(["secrets", "openai-api-key", "--from-file", str(source), "--apply"], runner=runner)
        == 0
    )
    assert fake.mutations() == ["ssm put-parameter"]
    assert all(secret not in " ".join(call) for call in fake.calls)
    assert secret not in capsys.readouterr().out
    assert seen_files and not Path(seen_files[0]).exists()  # temp file removed
    with pytest.raises(SystemExit):
        main(["secrets", "engine-db-password", "--from-file", str(source)], runner=FakeAccount())


# ---------------------------------------------------------------- model upload (#97, F-072)


def _fake_hf(monkeypatch: pytest.MonkeyPatch, tamper: str | None = None) -> None:
    import hashlib
    import io

    _repo, _rev, files = aws_mvp._model_constants()
    content = {name: f"synthetic {name}".encode() for name in files}
    tree = [
        {
            "path": n,
            "size": len(c),
            "lfs": {"oid": hashlib.sha256(c).hexdigest()} if n.startswith("model") else None,
        }
        for n, c in content.items()
    ]

    class Response(io.BytesIO):
        headers: dict[str, str] = {}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def urlopen(url: str, timeout: int = 0) -> Response:
        if "/api/models/" in url:
            return Response(json.dumps(tree).encode())
        name = url.rsplit("/", 1)[-1]
        body = b"tampered!" if name == tamper else content[name]
        return Response(body)

    monkeypatch.setattr(aws_mvp.urllib.request, "urlopen", urlopen)


def test_model_upload_verifies_hashes_and_dry_run_uploads_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_hf(monkeypatch)
    fake = FakeAccount()
    assert main(["model-upload", "--bucket", "aorte-mvp-backups-1"], runner=fake) == 0
    assert fake.mutations() == []
    calls: list[list[str]] = []
    fake2 = FakeAccount()

    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return ok(list(argv)) if list(argv)[1:3] == ["s3", "cp"] else fake2(argv)

    assert main(["model-upload", "--bucket", "aorte-mvp-backups-1", "--apply"], runner=runner) == 0
    uploads = [c for c in calls if c[1:3] == ["s3", "cp"]]
    assert uploads and uploads[-1][4].endswith("/manifest.sha256")
    repo, rev, files = aws_mvp._model_constants()
    assert len(uploads) == len(files) + 1
    assert all(f"/{aws_mvp.model_snapshot_key(repo, rev)}/" in c[4] for c in uploads)


def test_model_upload_rejects_tampered_file_and_wrong_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_hf(monkeypatch, tamper="model.onnx_data")
    fake = FakeAccount()
    assert main(["model-upload", "--bucket", "aorte-mvp-backups-1", "--apply"], runner=fake) == 1
    assert fake.mutations() == []
    _fake_hf(monkeypatch)
    assert main(["model-upload", "--bucket", "someone-else", "--apply"], runner=FakeAccount()) == 1


def test_terraform_model_path_matches_engine() -> None:
    repo, rev, _files = aws_mvp._model_constants()
    ecs = (REPO / "deploy" / "terraform" / "mvp" / "ecs.tf").read_text(encoding="utf-8")
    snapshot = aws_mvp.model_snapshot_key(repo, rev).removeprefix("models/")
    assert f'model_snapshot    = "{snapshot}"' in ecs


def test_image_list_matches_compose_digests() -> None:
    images = json.loads((REPO / "deploy" / "aws-mvp-images.json").read_text(encoding="utf-8"))
    compose = (REPO / "deploy" / "full-stack-compose.yml").read_text(encoding="utf-8")
    for name in ("postgres", "odoo", "pgvector", "mailpit"):
        source = images[name]["source"].removeprefix("docker.io/").removeprefix("library/")
        assert f"image: {source}@{images[name]['digest']}" in compose, name
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", v["digest"]) for v in images.values())


def test_task_runs_allowed_modules_and_returns_exit_code() -> None:
    fake = FakeAccount()
    state = {"n": 0}

    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        if argv[1:3] == ["ecs", "run-task"]:
            overrides = json.loads(
                Path(opt(argv, "--overrides").removeprefix("file://")).read_text()
            )
            assert overrides["containerOverrides"][0]["command"][:3] == [
                "python",
                "-m",
                "engine.erp_cli",
            ]
            return ok(argv, {"tasks": [{"taskArn": "arn:aws:ecs:task/aorte-mvp/abc123"}]})
        if argv[1:3] == ["ecs", "describe-tasks"]:
            state["n"] += 1
            status = "STOPPED" if state["n"] > 1 else "RUNNING"
            return ok(
                argv,
                {
                    "tasks": [
                        {
                            "lastStatus": status,
                            "containers": [
                                {"name": "model-sync", "exitCode": 0},
                                {"name": "proposal", "exitCode": 3},
                            ],
                        }
                    ]
                },
            )
        if argv[1:3] == ["logs", "get-log-events"]:
            return ok(argv, {"events": [{"message": "preflight=ok"}]})
        return fake(argv)

    code = main(
        [
            "task",
            "--apply",
            "--env",
            "EXECUTION_ERP_MODE=YES",
            "--",
            "python",
            "-m",
            "engine.erp_cli",
            "preflight",
        ],
        runner=runner,
    )
    assert code == 3  # the container's exit code
    assert (
        main(["task", "--", "python", "-m", "engine.erp_cli", "preflight"], runner=fake) == 0
    )  # dry run
    assert fake.mutations() == []


def test_task_rejects_other_commands_and_secret_env() -> None:
    for argv in (
        ["task", "--apply", "--", "sh", "-c", "env"],
        ["task", "--apply", "--", "python", "-m", "os"],
        [
            "task",
            "--apply",
            "--env",
            "OPENAI_API_KEY=x",
            "--",
            "python",
            "-m",
            "engine.erp_cli",
            "preflight",
        ],
    ):
        fake = FakeAccount()
        assert main(argv, runner=fake) == 1
        assert fake.mutations() == [] and not any(c[2] == "run-task" for c in fake.calls)


def test_ci_accepts_only_the_oidc_deploy_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_PROFILE")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    other = FakeAccount()  # an SSO admin session is not the deploy role
    assert main(["status"], runner=other) == 1
    deploy = FakeAccount()
    deploy.arn = f"arn:aws:sts::{ACCOUNT}:assumed-role/aorte-mvp-gha-deploy/GitHubActions"
    assert main(["status"], runner=deploy) == 0


def test_long_s3_copies_get_a_long_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    monkeypatch.setattr(aws_mvp.subprocess, "run", lambda argv, **kw: seen.append(kw["timeout"]))
    aws_mvp._default_runner(["aws", "s3", "cp", "a", "s3://b/c"])
    aws_mvp._default_runner(["aws", "ec2", "describe-instances"])
    assert seen == [3600, 300]
