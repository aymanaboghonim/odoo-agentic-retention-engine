"""#136: the facts register accepts only official sources or our own measurements."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_facts_sources import REGISTER, check  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def row(source: str, value: str = "v") -> str:
    return f"| F-999 | fact | {value} | {source} | 2026-11-26 | verified |"


def test_current_register_passes() -> None:
    assert check((REPO / REGISTER).read_text(encoding="utf-8").splitlines()) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://stackoverflow.com/questions/1/x",
        "https://www.reddit.com/r/aws/comments/1",
        "https://someone.medium.com/post",
        "https://medium.com/@someone/post",
        "https://dev.to/someone/post",
        "https://community.grafana.com/t/1",
        "https://forum.langfuse.example/t/1",
        "https://github.com/langfuse/langfuse/discussions/1",
        "https://chatgpt.com/share/1",
        "https://myblog.wordpress.com/2026/post",
    ],
)
def test_community_blog_and_ai_sources_are_refused(url: str) -> None:
    (error,) = check([row(url)])
    assert error.startswith("F-999: ")


@pytest.mark.parametrize(
    "source",
    [
        "https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/x.html",
        "https://github.com/hashicorp/terraform-provider-aws/tree/main/website/docs/r",
        "https://github.blog/changelog/2026-01-01-x",  # the vendor's own changelog
        "https://blog.google/technology/x",  # the vendor's own blog
        "measured: `aws ecr describe-images`",
        "measured with `uv run python scripts/spike_s1_odoo.py`",
        "measured in the pinned S3 runner against Qdrant 1.20.0",
    ],
)
def test_official_and_measured_sources_pass(source: str) -> None:
    assert check([row(source)]) == []


@pytest.mark.parametrize("source", ["measured", "measured: yes", "https://pypi.org/x; measured"])
def test_measured_without_a_method_is_refused(source: str) -> None:
    assert check([row(source)]) == [
        "F-999: a `measured` source must name its method (command, script, or setup)"
    ]


def test_a_pipe_in_the_value_does_not_shift_the_source_column() -> None:
    line = row("https://stackoverflow.com/q/1", value="a | b")
    assert check([line]) and check([row("https://pypi.org/x", value="a | b")]) == []
