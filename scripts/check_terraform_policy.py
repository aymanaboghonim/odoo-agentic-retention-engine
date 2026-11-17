"""Policy gate for the AWS MVP Terraform (spec: docs/specs/aws-mvp-deployment.md).

Source mode (CI, no AWS credentials): parse every ``.tf`` file under the given roots
and enforce INF-1..INF-6 on the configuration itself. Rules are written so that a
variable cannot hide a violation (for example, ingress may not use CIDR blocks at all).

Plan mode (``scripts/tf_safe.sh``): read ``terraform show -json <plan>`` and enforce
INF-7 (no replace or delete of a stateful resource unless ``--allow-replace`` names
it) plus checks on resolved values. Exit 0 when clean, 1 on any violation, 2 on usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hcl2

# INF-1: 1st-party AWS resource types this deployment may create. Anything else fails
# closed until it is reviewed and added here (NAT, EIP, load balancers, RDS,
# Route 53, Marketplace, launch templates, and Secrets Manager are deliberately absent).
ALLOWED_RESOURCE_TYPES = frozenset(
    {
        "aws_budgets_budget",
        "aws_cloudfront_distribution",
        "aws_cloudfront_origin_access_control",
        "aws_cloudfront_vpc_origin",
        "aws_cloudwatch_event_rule",
        "aws_cloudwatch_event_target",
        "aws_cloudwatch_log_group",
        "aws_cognito_user_group",
        "aws_cognito_user_pool",
        "aws_cognito_user_pool_client",
        "aws_cognito_user_pool_domain",
        "aws_default_security_group",
        "aws_ecr_lifecycle_policy",
        "aws_ecr_repository",
        "aws_ecs_cluster",
        "aws_ecs_service",
        "aws_ecs_task_definition",
        "aws_egress_only_internet_gateway",
        "aws_iam_instance_profile",
        "aws_iam_openid_connect_provider",
        "aws_iam_policy",
        "aws_iam_role",
        "aws_iam_role_policy",
        "aws_iam_role_policy_attachment",
        "aws_instance",
        "aws_internet_gateway",
        "aws_lambda_function",
        "aws_lambda_permission",
        "aws_route",
        "aws_route_table",
        "aws_route_table_association",
        "aws_s3_bucket",
        "aws_s3_bucket_lifecycle_configuration",
        "aws_s3_bucket_ownership_controls",
        "aws_s3_bucket_policy",
        "aws_s3_bucket_public_access_block",
        "aws_s3_bucket_server_side_encryption_configuration",
        "aws_s3_bucket_versioning",
        "aws_s3_object",
        "aws_scheduler_schedule",
        "aws_security_group",
        "aws_ssm_parameter",
        "aws_subnet",
        "aws_vpc",
        "aws_vpc_security_group_egress_rule",
        "aws_vpc_security_group_ingress_rule",
        "terraform_data",
        # hashicorp/random: random_id for name suffixes; random_password only per INF-5.
        "random_id",
        "random_password",
    }
)

# INF-7: resources whose replace or delete loses data, identity, or a stable URL.
STATEFUL_RESOURCE_TYPES = frozenset(
    {
        "aws_cloudfront_distribution",
        "aws_cognito_user_pool",
        "aws_ebs_volume",
        "aws_ecr_repository",
        "aws_instance",
        "aws_s3_bucket",
        "aws_ssm_parameter",
    }
)

# INF-5 exception (owner, 2026-11-17): CloudFront keeps custom origin header values in the
# distribution config, so the X-Origin-Verify value is in state whatever Terraform does.
STATE_SECRET_EXCEPTIONS = frozenset({"random_password.origin_verify"})

ECS_AMI_PARAMETER_PREFIX = "/aws/service/ecs/optimized-ami/"
STOP_INSTANCES_TARGET = "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
OPEN_CIDRS = frozenset({"0.0.0.0/0", "::/0"})
PUBLIC_ACCESS_FLAGS = (
    "block_public_acls",
    "block_public_policy",
    "ignore_public_acls",
    "restrict_public_buckets",
)


@dataclass(frozen=True)
class Violation:
    rule: str
    address: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule} {self.address}: {self.message}"


@dataclass(frozen=True)
class Resource:
    type: str
    name: str
    body: dict[str, Any]

    @property
    def address(self) -> str:
        return f"{self.type}.{self.name}"


@dataclass
class Config:
    resources: list[Resource]
    data: list[Resource]
    aws_providers: list[dict[str, Any]] = field(default_factory=list)
    variable_defaults: dict[str, Any] = field(default_factory=dict)

    def of_type(self, resource_type: str) -> list[Resource]:
        return [r for r in self.resources if r.type == resource_type]

    def data_of_type(self, data_type: str) -> list[Resource]:
        return [d for d in self.data if d.type == data_type]


# ---------------------------------------------------------------- loading


def _blocks(value: Any) -> list[dict[str, Any]]:
    """Nested HCL blocks parse as a list of dicts (or one dict); normalize to a list."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _labelled(entries: Any) -> Iterator[Resource]:
    for entry in _blocks(entries):
        for resource_type, named in entry.items():
            for name, body in (named or {}).items():
                yield Resource(resource_type, name, body if isinstance(body, dict) else {})


def load_source(roots: Iterable[Path]) -> Config:
    options = hcl2.SerializationOptions(
        strip_string_quotes=True, with_comments=False, explicit_blocks=False
    )
    resources: list[Resource] = []
    data: list[Resource] = []
    providers: list[dict[str, Any]] = []
    defaults: dict[str, Any] = {}
    for root in roots:
        files = sorted(root.rglob("*.tf"))
        if not files:
            raise FileNotFoundError(f"no .tf files under {root}")
        for path in files:
            if ".terraform" in path.parts:
                continue
            with path.open(encoding="utf-8") as handle:
                parsed = hcl2.load(handle, serialization_options=options)
            resources.extend(_labelled(parsed.get("resource")))
            data.extend(_labelled(parsed.get("data")))
            for entry in _blocks(parsed.get("provider")):
                providers.extend(_blocks(entry.get("aws")))
            for entry in _blocks(parsed.get("variable")):
                for name, body in entry.items():
                    if isinstance(body, dict) and "default" in body:
                        defaults[name] = body["default"]
    return Config(resources, data, providers, defaults)


def _ref(value: Any, kind: str) -> str | None:
    """Return `name` when value is an interpolation like `${aws_s3_bucket.name.id}`."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("${") and text.endswith("}"):
        text = text[2:-1]
    prefix = f"{kind}."
    if not text.startswith(prefix):
        return None
    return text[len(prefix) :].split(".", 1)[0].split("[", 1)[0]


def _resolve(value: Any, defaults: dict[str, Any] | None) -> Any:
    """Resolve a plain `${var.name}` to its declared default; anything else is unchanged."""
    name = _ref(value, "var")
    if name is not None and defaults is not None and name in defaults:
        return defaults[name]
    return value


def _ports_include(rule: dict[str, Any], port: int, defaults: dict[str, Any] | None = None) -> bool:
    if str(rule.get("protocol", "")) == "-1":
        return True
    try:
        low = int(_resolve(rule.get("from_port", -1), defaults))
        high = int(_resolve(rule.get("to_port", low), defaults))
    except TypeError, ValueError:
        return True  # unresolved port: treat as possibly 22 (fail closed)
    return low <= port <= high


# ---------------------------------------------------------------- source rules


def check_source(config: Config) -> list[Violation]:
    violations: list[Violation] = []
    violations += _inf1_types_and_ami(config)
    violations += _inf2_ingress(config)
    violations += _inf3_encryption_and_buckets(config)
    violations += _inf4_imds(config)
    violations += _inf5_secrets(config)
    violations += _inf6_nightly_stop(config)
    violations += _inf7_account_pinned(config)
    return violations


def _inf7_account_pinned(config: Config) -> list[Violation]:
    """Every AWS provider pins allowed_account_ids, so a plan cannot target another account."""
    if config.resources and not config.aws_providers:
        return [Violation("INF-7", 'provider "aws"', "an aws provider block is required")]
    return [
        Violation("INF-7", 'provider "aws"', "must set allowed_account_ids")
        for provider in config.aws_providers
        if not provider.get("allowed_account_ids")
    ]


def _inf1_types_and_ami(config: Config) -> list[Violation]:
    out = [
        Violation("INF-1", r.address, "resource type is not on the 1st-party allowlist")
        for r in config.resources
        if r.type not in ALLOWED_RESOURCE_TYPES
    ]
    ssm = {d.name: str(d.body.get("name", "")) for d in config.data_of_type("aws_ssm_parameter")}
    for inst in config.of_type("aws_instance"):
        name = _ref(inst.body.get("ami"), "data.aws_ssm_parameter")
        if name is None or not ssm.get(name, "").startswith(ECS_AMI_PARAMETER_PREFIX):
            out.append(
                Violation(
                    "INF-1",
                    inst.address,
                    f"ami must come from an SSM parameter under {ECS_AMI_PARAMETER_PREFIX}",
                )
            )
    return out


def _ingress_rules(config: Config) -> Iterator[tuple[str, dict[str, Any]]]:
    for sg in config.of_type("aws_security_group"):
        for rule in _blocks(sg.body.get("ingress")):
            yield sg.address, rule
    for standalone in config.of_type("aws_vpc_security_group_ingress_rule"):
        body = dict(standalone.body)
        body.setdefault("cidr_blocks", body.get("cidr_ipv4"))
        body.setdefault("ipv6_cidr_blocks", body.get("cidr_ipv6"))
        body.setdefault("protocol", body.get("ip_protocol", ""))
        yield standalone.address, body
    for sg in config.of_type("aws_default_security_group"):
        for rule in _blocks(sg.body.get("ingress")):
            yield sg.address, rule


def _inf2_ingress(config: Config) -> list[Violation]:
    out: list[Violation] = []
    for address, rule in _ingress_rules(config):
        if rule.get("cidr_blocks") or rule.get("ipv6_cidr_blocks"):
            out.append(
                Violation("INF-2", address, "ingress may not use CIDR blocks; use a prefix list")
            )
        if rule.get("self") is True:
            out.append(Violation("INF-2", address, "ingress may not use self"))
        if _ports_include(rule, 22, config.variable_defaults):
            out.append(Violation("INF-2", address, "ingress may not include port 22"))
        if not (rule.get("prefix_list_ids") or rule.get("prefix_list_id")) and not (
            rule.get("security_groups") or rule.get("referenced_security_group_id")
        ):
            out.append(
                Violation("INF-2", address, "ingress must come from a prefix list or a group")
            )
    for inst in config.of_type("aws_instance"):
        if inst.body.get("associate_public_ip_address") is True:
            out.append(Violation("INF-2", inst.address, "instance may not take a public IPv4"))
    return out


def _bucket_refs(config: Config, resource_type: str) -> dict[str, Resource]:
    refs: dict[str, Resource] = {}
    for res in config.of_type(resource_type):
        name = _ref(res.body.get("bucket"), "aws_s3_bucket")
        if name:
            refs[name] = res
    return refs


def _inf3_encryption_and_buckets(config: Config) -> list[Violation]:
    out: list[Violation] = []
    for inst in config.of_type("aws_instance"):
        roots = _blocks(inst.body.get("root_block_device"))
        if not roots or not all(r.get("encrypted") is True for r in roots):
            out.append(Violation("INF-3", inst.address, "root volume must set encrypted = true"))
        for ebs in _blocks(inst.body.get("ebs_block_device")):
            if ebs.get("encrypted") is not True:
                out.append(Violation("INF-3", inst.address, "EBS block device not encrypted"))
    pab = _bucket_refs(config, "aws_s3_bucket_public_access_block")
    sse = _bucket_refs(config, "aws_s3_bucket_server_side_encryption_configuration")
    for bucket in config.of_type("aws_s3_bucket"):
        block = pab.get(bucket.name)
        if block is None or not all(block.body.get(f) is True for f in PUBLIC_ACCESS_FLAGS):
            out.append(
                Violation("INF-3", bucket.address, "needs a public access block with all 4 = true")
            )
        if bucket.name not in sse:
            out.append(Violation("INF-3", bucket.address, "needs server-side encryption config"))
    out += _bucket_policies_cloudfront_only(config)
    return out


def _bucket_policies_cloudfront_only(config: Config) -> list[Violation]:
    """A bucket policy may only grant the CloudFront service, scoped by AWS:SourceArn."""
    documents = {d.name: d for d in config.data_of_type("aws_iam_policy_document")}
    out: list[Violation] = []
    for policy in config.of_type("aws_s3_bucket_policy"):
        doc_name = _ref(policy.body.get("policy"), "data.aws_iam_policy_document")
        doc = documents.get(doc_name or "")
        if doc is None:
            out.append(
                Violation("INF-3", policy.address, "policy must be an aws_iam_policy_document")
            )
            continue
        for statement in _blocks(doc.body.get("statement")):
            if statement.get("effect", "Allow") != "Allow":
                continue
            principals = _blocks(statement.get("principals"))
            services_only = principals and all(
                p.get("type") == "Service" and p.get("identifiers") == ["cloudfront.amazonaws.com"]
                for p in principals
            )
            scoped = any(
                c.get("variable") == "AWS:SourceArn" for c in _blocks(statement.get("condition"))
            )
            if not (services_only and scoped):
                out.append(
                    Violation(
                        "INF-3",
                        policy.address,
                        "Allow must be CloudFront only, with an AWS:SourceArn condition",
                    )
                )
    return out


def _inf4_imds(config: Config) -> list[Violation]:
    out: list[Violation] = []
    for inst in config.of_type("aws_instance"):
        meta = _blocks(inst.body.get("metadata_options"))
        ok = len(meta) == 1 and (
            meta[0].get("http_tokens") == "required"
            and meta[0].get("http_put_response_hop_limit") == 1
        )
        if not ok:
            out.append(
                Violation("INF-4", inst.address, "metadata_options must require IMDSv2, hop 1")
            )
    return out


def _inf5_secrets(config: Config) -> list[Violation]:
    out: list[Violation] = []
    for param in config.of_type("aws_ssm_parameter"):
        if param.body.get("type") == "SecureString" and (
            "value" in param.body or "insecure_value" in param.body
        ):
            out.append(
                Violation("INF-5", param.address, "SecureString must use value_wo, not value")
            )
    for res in config.resources:
        if res.type in {"random_password", "random_string", "tls_private_key"} and (
            res.address not in STATE_SECRET_EXCEPTIONS
        ):
            out.append(Violation("INF-5", res.address, "stores a secret in state; use ephemeral"))
    return out


def _inf6_nightly_stop(config: Config) -> list[Violation]:
    if not config.of_type("aws_instance"):
        return []
    for schedule in config.of_type("aws_scheduler_schedule"):
        targets = _blocks(schedule.body.get("target"))
        if (
            any(t.get("arn") == STOP_INSTANCES_TARGET for t in targets)
            and schedule.body.get("schedule_expression_timezone")
            and str(schedule.body.get("state", "ENABLED")) == "ENABLED"
        ):
            return []
    return [
        Violation(
            "INF-6",
            "aws_scheduler_schedule",
            "an enabled ec2:stopInstances schedule with a timezone is required",
        )
    ]


# ---------------------------------------------------------------- plan rules


def check_plan(plan: dict[str, Any], allow_replace: set[str]) -> tuple[list[Violation], Counter]:
    violations: list[Violation] = []
    summary: Counter = Counter()
    for change in plan.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        address = change.get("address", "?")
        resource_type = change.get("type", "")
        mode = change.get("mode", "managed")
        if mode != "managed" or actions in (["no-op"], ["read"]):
            continue
        kind = "replace" if {"create", "delete"} <= set(actions) else "/".join(actions)
        summary[kind] += 1
        if resource_type not in ALLOWED_RESOURCE_TYPES and "delete" not in actions:
            violations.append(Violation("INF-1", address, "type is not on the allowlist"))
        if (
            "delete" in actions
            and resource_type in STATEFUL_RESOURCE_TYPES
            and address not in allow_replace
        ):
            violations.append(
                Violation("INF-7", address, f"{kind} of a stateful resource needs --allow-replace")
            )
        after = change.get("change", {}).get("after") or {}
        if "create" in actions or "update" in actions:
            violations += _resolved_value_checks(address, resource_type, after)
    return violations, summary


def _resolved_value_checks(address: str, resource_type: str, after: dict[str, Any]) -> list:
    out: list[Violation] = []
    if resource_type == "aws_security_group":
        for rule in _blocks(after.get("ingress")):
            cidrs = set(rule.get("cidr_blocks") or []) | set(rule.get("ipv6_cidr_blocks") or [])
            if cidrs:
                label = "open " if cidrs & OPEN_CIDRS else ""
                out.append(
                    Violation("INF-2", address, f"resolved ingress {label}CIDRs {sorted(cidrs)}")
                )
            if _ports_include(rule, 22):
                out.append(Violation("INF-2", address, "resolved ingress includes port 22"))
    if resource_type == "aws_instance":
        if after.get("associate_public_ip_address") is True:
            out.append(Violation("INF-2", address, "resolved instance takes a public IPv4"))
        meta = _blocks(after.get("metadata_options"))
        if not meta or meta[0].get("http_tokens") != "required":
            out.append(Violation("INF-4", address, "resolved http_tokens is not required"))
        if meta and meta[0].get("http_put_response_hop_limit") != 1:
            out.append(Violation("INF-4", address, "resolved hop limit is not 1"))
        roots = _blocks(after.get("root_block_device"))
        if roots and not all(r.get("encrypted") is True for r in roots):
            out.append(Violation("INF-3", address, "resolved root volume is not encrypted"))
    if resource_type == "aws_ssm_parameter" and after.get("type") == "SecureString":
        if after.get("value") not in (None, "") or after.get("insecure_value") not in (None, ""):
            out.append(Violation("INF-5", address, "SecureString value would be stored in state"))
    return out


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", action="append", type=Path, default=[], help="root dir")
    parser.add_argument("--plan", type=Path, help="terraform show -json output")
    parser.add_argument("--allow-replace", action="append", default=[], metavar="ADDRESS")
    args = parser.parse_args(argv)
    if not args.source and not args.plan:
        parser.print_usage(sys.stderr)
        return 2

    violations: list[Violation] = []
    if args.source:
        violations += check_source(load_source(args.source))
        print(f"source: checked {', '.join(str(p) for p in args.source)}")
    if args.plan:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        plan_violations, summary = check_plan(plan, set(args.allow_replace))
        violations += plan_violations
        counts = ", ".join(f"{k}={v}" for k, v in sorted(summary.items())) or "no changes"
        print(f"plan: {counts}")
    for violation in violations:
        print(f"FAIL {violation}")
    print("policy: OK" if not violations else f"policy: {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
