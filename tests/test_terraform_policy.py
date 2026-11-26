"""INF-1..INF-7 policy gate (spec: docs/specs/aws-mvp-deployment.md). Hermetic: no AWS."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_terraform_policy import check_plan, check_source, load_source, main

GOOD = """
provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = [var.account_id]
}
data "aws_ssm_parameter" "ecs_ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
}
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}
data "aws_iam_policy_document" "ui" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.ui.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.edge.arn]
    }
  }
}
resource "aws_security_group" "host" {
  name = "host"
  ingress {
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }
}
resource "aws_instance" "host" {
  ami           = data.aws_ssm_parameter.ecs_ami.value
  instance_type = "t3a.medium"
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    encrypted = true
  }
}
resource "aws_s3_bucket" "ui" {
  bucket_prefix = "ui-"
}
resource "aws_s3_bucket_public_access_block" "ui" {
  bucket                  = aws_s3_bucket.ui.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "ui" {
  bucket = aws_s3_bucket.ui.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
resource "aws_s3_bucket_policy" "ui" {
  bucket = aws_s3_bucket.ui.id
  policy = data.aws_iam_policy_document.ui.json
}
resource "aws_ssm_parameter" "origin_secret" {
  name             = "/aorte/mvp/origin-verify"
  type             = "SecureString"
  value_wo         = ephemeral.random_password.origin.result
  value_wo_version = 1
}
resource "aws_scheduler_schedule" "nightly_stop" {
  schedule_expression          = "cron(0 22 * * ? *)"
  schedule_expression_timezone = var.stop_timezone
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ InstanceIds = [aws_instance.host.id] })
  }
}
"""


def rules(tmp_path: Path, text: str) -> list[str]:
    (tmp_path / "main.tf").write_text(text, encoding="utf-8")
    return [f"{v.rule} {v.address}" for v in check_source(load_source([tmp_path]))]


def test_good_configuration_passes(tmp_path: Path) -> None:
    assert rules(tmp_path, GOOD) == []


@pytest.mark.parametrize(
    "resource",
    [
        'resource "aws_nat_gateway" "x" {}',
        'resource "aws_eip" "x" {}',
        'resource "aws_lb" "x" {}',
        'resource "aws_db_instance" "x" {}',
        'resource "aws_route53domains_registered_domain" "x" {}',
        'resource "aws_route53_zone" "x" {}',
        'resource "aws_marketplace_subscription" "x" {}',
        'resource "aws_launch_template" "x" {}',
    ],
)
def test_rejects_denied_resource_types(tmp_path: Path, resource: str) -> None:
    found = rules(tmp_path, GOOD + resource)
    assert any(r.startswith("INF-1 ") and r.endswith(".x") for r in found)


def test_rejects_non_aws_ami(tmp_path: Path) -> None:
    text = GOOD.replace("data.aws_ssm_parameter.ecs_ami.value", '"ami-0123456789abcdef0"')
    assert "INF-1 aws_instance.host" in rules(tmp_path, text)
    other = GOOD.replace("/aws/service/ecs/optimized-ami/", "/marketplace/")
    assert "INF-1 aws_instance.host" in rules(tmp_path, other)


@pytest.mark.parametrize(
    "rule",
    [
        'cidr_blocks = ["0.0.0.0/0"]',
        'ipv6_cidr_blocks = ["::/0"]',
        "cidr_blocks = var.allowed",
        'cidr_blocks = ["10.0.0.0/8"]',
    ],
)
def test_rejects_open_ingress(tmp_path: Path, rule: str) -> None:
    text = GOOD.replace(
        "prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]",
        f"prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]\n    {rule}",
    )
    assert "INF-2 aws_security_group.host" in rules(tmp_path, text)


def test_rejects_port_22(tmp_path: Path) -> None:
    for low, high in (("22", "22"), ("0", "65535")):
        text = GOOD.replace("from_port       = 8080", f"from_port       = {low}").replace(
            "to_port         = 8080", f"to_port         = {high}"
        )
        assert "INF-2 aws_security_group.host" in rules(tmp_path, text)
    standalone = GOOD + (
        'resource "aws_vpc_security_group_ingress_rule" "ssh" {\n'
        '  from_port   = 22\n  to_port     = 22\n  ip_protocol = "tcp"\n'
        '  cidr_ipv4   = "0.0.0.0/0"\n}\n'
    )
    assert rules(tmp_path, standalone).count("INF-2 aws_vpc_security_group_ingress_rule.ssh") >= 2


def test_rejects_public_ipv4_on_instance(tmp_path: Path) -> None:
    text = GOOD.replace(
        'instance_type = "t3a.medium"',
        'instance_type = "t3a.medium"\n  associate_public_ip_address = true',
    )
    assert "INF-2 aws_instance.host" in rules(tmp_path, text)


def test_requires_encryption(tmp_path: Path) -> None:
    text = GOOD.replace("encrypted = true", "encrypted = false")
    assert "INF-3 aws_instance.host" in rules(tmp_path, text)
    no_sse = GOOD.replace(
        'resource "aws_s3_bucket_server_side_encryption_configuration" "ui"',
        'resource "aws_s3_bucket_server_side_encryption_configuration" "other"',
    ).replace("bucket = aws_s3_bucket.ui.id\n  rule", "bucket = aws_s3_bucket.other.id\n  rule")
    assert "INF-3 aws_s3_bucket.ui" in rules(tmp_path, no_sse)


def test_requires_public_access_block(tmp_path: Path) -> None:
    text = GOOD.replace("restrict_public_buckets = true", "restrict_public_buckets = false")
    assert "INF-3 aws_s3_bucket.ui" in rules(tmp_path, text)
    public = GOOD.replace('identifiers = ["cloudfront.amazonaws.com"]', 'identifiers = ["*"]')
    assert "INF-3 aws_s3_bucket_policy.ui" in rules(tmp_path, public)
    unscoped = GOOD.replace('variable = "AWS:SourceArn"', 'variable = "aws:Referer"')
    assert "INF-3 aws_s3_bucket_policy.ui" in rules(tmp_path, unscoped)


def test_requires_imdsv2_hop_limit_1(tmp_path: Path) -> None:
    assert "INF-4 aws_instance.host" in rules(
        tmp_path,
        GOOD.replace('http_tokens                 = "required"', 'http_tokens = "optional"'),
    )
    assert "INF-4 aws_instance.host" in rules(
        tmp_path, GOOD.replace("http_put_response_hop_limit = 1", "http_put_response_hop_limit = 2")
    )


def test_rejects_securestring_literal(tmp_path: Path) -> None:
    text = GOOD.replace(
        "value_wo         = ephemeral.random_password.origin.result\n  value_wo_version = 1",
        'value            = "not-a-real-secret"',
    )
    assert "INF-5 aws_ssm_parameter.origin_secret" in rules(tmp_path, text)
    stateful = GOOD + 'resource "random_password" "db" {\n  length = 32\n}\n'
    assert "INF-5 random_password.db" in rules(tmp_path, stateful)


def test_requires_nightly_stop(tmp_path: Path) -> None:
    disabled = GOOD.replace(
        "schedule_expression_timezone = var.stop_timezone",
        'schedule_expression_timezone = var.stop_timezone\n  state = "DISABLED"',
    )
    assert "INF-6 aws_scheduler_schedule" in rules(tmp_path, disabled)
    no_tz = GOOD.replace("  schedule_expression_timezone = var.stop_timezone\n", "")
    assert "INF-6 aws_scheduler_schedule" in rules(tmp_path, no_tz)
    wrong = GOOD.replace("ec2:stopInstances", "ec2:startInstances")
    assert "INF-6 aws_scheduler_schedule" in rules(tmp_path, wrong)


def test_requires_pinned_account(tmp_path: Path) -> None:
    unpinned = GOOD.replace("  allowed_account_ids = [var.account_id]\n", "")
    assert 'INF-7 provider "aws"' in rules(tmp_path, unpinned)
    no_provider = GOOD.split("}\n", 1)[1]
    assert 'INF-7 provider "aws"' in rules(tmp_path, no_provider)


def test_grafana_root_passes_without_an_aws_provider() -> None:
    """#139: the Grafana root has no AWS resource, so INF-7 needs no AWS provider there."""
    root = Path(__file__).resolve().parents[1] / "deploy" / "terraform" / "grafana"
    assert check_source(load_source([root])) == []
    plan = _plan(_change("grafana_dashboard.overview", ["create"]))
    assert check_plan(plan, set())[0] == []


def _plan(*changes: dict[str, object]) -> dict[str, object]:
    return {"resource_changes": list(changes)}


def _change(address: str, actions: list[str], after: dict[str, object] | None = None) -> dict:
    resource_type = address.split(".")[0]
    return {
        "address": address,
        "type": resource_type,
        "mode": "managed",
        "change": {"actions": actions, "after": after or {}},
    }


def test_rejects_unapproved_replace() -> None:
    replace = _change("aws_cognito_user_pool.operators", ["delete", "create"])
    violations, summary = check_plan(_plan(replace), set())
    assert [v.rule for v in violations] == ["INF-7"]
    assert summary["replace"] == 1
    violations, _ = check_plan(_plan(replace), {"aws_cognito_user_pool.operators"})
    assert violations == []
    delete = _change("aws_s3_bucket.backups", ["delete"])
    assert [v.rule for v in check_plan(_plan(delete), set())[0]] == ["INF-7"]
    harmless = _change("aws_iam_role_policy.x", ["delete", "create"])
    assert check_plan(_plan(harmless), set())[0] == []


def test_plan_checks_resolved_values() -> None:
    sg = _change(
        "aws_security_group.host",
        ["update"],
        {
            "ingress": [
                {"from_port": 22, "to_port": 22, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]}
            ]
        },
    )
    instance = _change(
        "aws_instance.host",
        ["create"],
        {
            "associate_public_ip_address": True,
            "metadata_options": [{"http_tokens": "optional", "http_put_response_hop_limit": 2}],
            "root_block_device": [{"encrypted": False}],
        },
    )
    secret = _change(
        "aws_ssm_parameter.x", ["create"], {"type": "SecureString", "value": "not-a-real-secret"}
    )
    unknown = _change("aws_nat_gateway.x", ["create"])
    rules_hit = sorted(v.rule for v in check_plan(_plan(sg, instance, secret, unknown), set())[0])
    assert rules_hit == ["INF-1", "INF-2", "INF-2", "INF-2", "INF-3", "INF-4", "INF-4", "INF-5"]


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "main.tf").write_text(GOOD, encoding="utf-8")
    assert main(["--source", str(tmp_path)]) == 0
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(_plan(_change("aws_instance.host", ["delete"]))), encoding="utf-8")
    assert main(["--plan", str(plan)]) == 1
    assert "INF-7 aws_instance.host" in capsys.readouterr().out
    assert main([]) == 2


def test_allows_only_origin_verify_in_state(tmp_path: Path) -> None:
    allowed = GOOD + 'resource "random_password" "origin_verify" {\n  length = 48\n}\n'
    assert rules(tmp_path, allowed) == []
    other = GOOD + 'resource "random_password" "db_password" {\n  length = 32\n}\n'
    assert "INF-5 random_password.db_password" in rules(tmp_path, other)


def test_port_from_variable_default(tmp_path: Path) -> None:
    variable = 'variable "api_port" {\n  type    = number\n  default = PORT\n}\n'
    text = GOOD.replace("from_port       = 8080", "from_port       = var.api_port").replace(
        "to_port         = 8080", "to_port         = var.api_port"
    )
    assert rules(tmp_path, text + variable.replace("PORT", "8080")) == []
    assert "INF-2 aws_security_group.host" in rules(tmp_path, text + variable.replace("PORT", "22"))
    no_default = text + 'variable "api_port" {\n  type = number\n}\n'
    assert "INF-2 aws_security_group.host" in rules(tmp_path, no_default)


def test_destroy_plan_allows_only_deletes() -> None:
    deletes = _plan(
        _change("aws_cognito_user_pool.operators", ["delete"]),
        _change("aws_s3_bucket.ui", ["delete"]),
    )
    violations, summary = check_plan(deletes, set(), destroy_plan=True)
    assert violations == [] and summary["delete"] == 2
    sneaky = _plan(_change("aws_s3_bucket.ui", ["delete"]), _change("aws_iam_role.x", ["create"]))
    assert [v.rule for v in check_plan(sneaky, set(), destroy_plan=True)[0]] == ["INF-7"]
