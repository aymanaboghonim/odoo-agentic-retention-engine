"""#129 RL-1: the /lf-otel/* relay to Langfuse Cloud US. Hermetic.

The Terraform part is read with python-hcl2 (the same loader as the INF policy check). The
CloudFront Function runs under Node with a stub for the `cloudfront` KeyValueStore module;
the live `aws cloudfront test-function` check is in the runbook.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_terraform_policy import _blocks, load_source  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
MVP = REPO / "deploy" / "terraform" / "mvp"
FUNCTION = MVP / "templates" / "lf_relay.js"
CONFIG = load_source([MVP])
SECRET = "k" * 43  # low-entropy placeholder with the real length (gitleaks-safe)


def one(resource_type: str, name: str) -> dict[str, Any]:
    found = [r.body for r in CONFIG.of_type(resource_type) if r.name == name]
    assert len(found) == 1, f"{resource_type}.{name}"
    return found[0]


def data_name(data_type: str, name: str) -> str:
    found = [d.body for d in CONFIG.data_of_type(data_type) if d.name == name]
    assert len(found) == 1
    return str(found[0]["name"])


def test_relay_behavior_policy() -> None:
    edge = one("aws_cloudfront_distribution", "edge")
    behaviors = _blocks(edge["ordered_cache_behavior"])
    to_langfuse = [b for b in behaviors if b["target_origin_id"] == "langfuse"]
    assert len(to_langfuse) == 1, "only one behavior reaches the Langfuse origin"
    relay = to_langfuse[0]
    assert relay["path_pattern"] == "/lf-otel/*"
    assert relay["viewer_protocol_policy"] == "https-only"
    assert "${data.aws_cloudfront_cache_policy.disabled.id}" == relay["cache_policy_id"]
    assert data_name("aws_cloudfront_cache_policy", "disabled") == "Managed-CachingDisabled"
    assert (
        relay["origin_request_policy_id"]
        == "${data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id}"
    )
    assert (
        data_name("aws_cloudfront_origin_request_policy", "all_viewer_except_host")
        == "Managed-AllViewerExceptHostHeader"
    )
    (assoc,) = _blocks(relay["function_association"])
    assert assoc["event_type"] == "viewer-request"
    assert assoc["function_arn"] == "${aws_cloudfront_function.lf_relay.arn}"
    # The default behavior and /api/* never reach Langfuse.
    (default,) = _blocks(edge["default_cache_behavior"])
    assert default["target_origin_id"] == "ui"


def test_langfuse_origin_is_https_to_the_us_host() -> None:
    edge = one("aws_cloudfront_distribution", "edge")
    (origin,) = [o for o in _blocks(edge["origin"]) if o["origin_id"] == "langfuse"]
    assert origin["domain_name"] == "${var.langfuse_host}"
    assert CONFIG.variable_defaults["langfuse_host"] == "us.cloud.langfuse.com"  # ADR-0029
    (custom,) = _blocks(origin["custom_origin_config"])
    assert custom["origin_protocol_policy"] == "https-only"
    assert custom["origin_ssl_protocols"] == ["TLSv1.2"]
    assert "custom_header" not in origin, "no secret goes to Langfuse"


def test_function_reads_the_secret_from_the_store_not_from_state() -> None:
    fn = one("aws_cloudfront_function", "lf_relay")
    assert fn["runtime"] == "cloudfront-js-2.0"  # KeyValueStore needs runtime 2.0 (F-089)
    assert fn["key_value_store_associations"] == ["${aws_cloudfront_key_value_store.relay.arn}"]
    assert not CONFIG.of_type("aws_cloudfrontkeyvaluestore_key"), "the value stays out of state"


# ---------------------------------------------------------------- function logic (Node)

HARNESS = """
const stored = process.argv[1];
const cf = { kvs: () => ({ get: async (key) => {
  if (key !== 'relay-key' || stored === '<missing>') throw new Error('KeyNotFound');
  return stored;
} }) };
%s
(async () => {
  const event = JSON.parse(require('fs').readFileSync(0, 'utf8'));
  process.stdout.write(JSON.stringify(await handler(event)));
})();
"""


def run_function(event: dict[str, Any], stored: str = SECRET) -> dict[str, Any]:
    code = FUNCTION.read_text(encoding="utf-8").replace("import cf from 'cloudfront';", "")
    result = subprocess.run(
        ["node", "-e", HARNESS % code, "--", stored],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return dict(json.loads(result.stdout))


def event(
    *, method: str = "POST", uri: str = "/lf-otel/api/public/otel/v1/traces", key: str | None
) -> dict[str, Any]:
    headers: dict[str, Any] = {"authorization": {"value": "Basic placeholder"}}
    if key is not None:
        headers["x-aorte-relay-key"] = {"value": key}
    return {"request": {"method": method, "uri": uri, "headers": headers}}


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")


@needs_node
def test_valid_request_is_rewritten_and_loses_the_secret_header() -> None:
    out = run_function(event(key=SECRET))
    assert out["uri"] == "/api/public/otel/v1/traces"
    assert "x-aorte-relay-key" not in out["headers"]
    assert out["headers"]["authorization"] == {"value": "Basic placeholder"}


@needs_node
@pytest.mark.parametrize(
    ("request_event", "stored"),
    [
        (event(key=None), SECRET),
        (event(key="wrong"), SECRET),
        (event(key=SECRET, method="GET"), SECRET),
        (event(key=SECRET, uri="/lf-otel/api/public/ingestion"), SECRET),
        (event(key=SECRET, uri="/api/public/otel/v1/traces"), SECRET),
        (event(key="unset"), "unset"),  # placeholder value never opens the relay
        (event(key=SECRET), "<missing>"),  # no key in the store: fail closed
    ],
)
def test_everything_else_gets_403(request_event: dict[str, Any], stored: str) -> None:
    assert run_function(request_event, stored) == {
        "statusCode": 403,
        "statusDescription": "Forbidden",
    }
