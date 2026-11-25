"""#142: the public deploy log must not show the AWS account ID or the backups bucket.

Hermetic: reads `.github/workflows/deploy-aws.yml` as text.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy-aws.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
STEPS = TEXT.split("    steps:\n", 1)[1]
FIRST_STEP = STEPS.split("\n      - ", 2)[1]  # step 1 text, after the leading comments


def test_mask_step_runs_before_any_other_step() -> None:
    first_use = STEPS.index("- uses:")
    mask = STEPS.index("::add-mask::")
    assert mask < first_use
    assert "name: Mask the AWS account ID" in FIRST_STEP
    assert "::add-mask::" in FIRST_STEP


def test_no_variables_are_used_because_they_print_in_step_headers() -> None:
    code = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))
    assert "${{ vars." not in code


def test_role_arn_and_bucket_come_from_secrets() -> None:
    assert "secrets.AWS_DEPLOY_ROLE_ARN" in TEXT
    assert "secrets.AWS_BACKUPS_BUCKET" in TEXT


def test_credentials_step_masks_the_account_id() -> None:
    assert "mask-aws-account-id: true" in TEXT


def test_build_summary_and_record_upload_are_disabled() -> None:
    assert re.search(r'^\s+DOCKER_BUILD_SUMMARY: "false"$', TEXT, re.M)
    assert re.search(r'^\s+DOCKER_BUILD_RECORD_UPLOAD: "false"$', TEXT, re.M)


def test_no_account_id_literal_in_the_workflow() -> None:
    assert not re.search(r"(?<![\w.])\d{12}(?![\w.])", TEXT)
