"""#106: mirror only the linux/amd64 manifest of each pinned image, and prune the rest.

Hermetic: fake `aws` and `skopeo` commands on PATH, and a fake runner for `aws_mvp.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import aws_mvp  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
MIRROR = REPO / "scripts" / "aws_mvp_mirror_images.sh"
IMAGES = json.loads((REPO / "deploy" / "aws-mvp-images.json").read_text(encoding="utf-8"))
OTHER = "sha256:" + "e" * 64


def index_for(item: dict[str, str]) -> str:
    """Registry answer for `skopeo inspect --raw <digest>`: an index whose amd64 child is the pin."""
    if item["aws_digest"] == item["digest"]:
        return json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json"})
    return json.dumps(
        {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"digest": OTHER, "platform": {"os": "linux", "architecture": "arm64"}},
                {
                    "digest": item["aws_digest"],
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {"digest": OTHER, "platform": {"os": "unknown", "architecture": "unknown"}},
            ],
        }
    )


def run_mirror(
    tmp_path: Path, images: dict[str, dict[str, str]], aws_body: str
) -> subprocess.CompletedProcess[str]:
    """The fake registry always answers with the real pins (IMAGES), not with `images`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    for item in IMAGES.values():
        (raw_dir / item["digest"].removeprefix("sha256:")).write_text(index_for(item))
    skopeo = '#!/usr/bin/env bash\nref="${@: -1}"; cat "$RAW_DIR/${ref##*sha256:}"\n'
    for tool, body in (("aws", f"#!/usr/bin/env bash\n{aws_body}\n"), ("skopeo", skopeo)):
        (bin_dir / tool).write_text(body)
        (bin_dir / tool).chmod(0o755)
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "scripts" / MIRROR.name).write_text(MIRROR.read_text())
    (root / "deploy" / "aws-mvp-images.json").write_text(json.dumps(images))
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "RAW_DIR": str(raw_dir)}
    return subprocess.run(
        ["bash", str(root / "scripts" / MIRROR.name), "registry.example", "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_pin_has_an_amd64_digest() -> None:
    for name, item in IMAGES.items():
        assert item["aws_digest"].startswith("sha256:"), name
    # pgvector is pinned to a single-platform manifest, so both pins are the same.
    assert IMAGES["pgvector"]["aws_digest"] == IMAGES["pgvector"]["digest"]


def test_mirror_copies_only_the_amd64_manifest_by_digest(tmp_path: Path) -> None:
    result = run_mirror(tmp_path, IMAGES, "exit 1")  # nothing in ECR yet
    assert result.returncode == 0, result.stderr
    for name, item in IMAGES.items():
        repo = item["source"].rsplit(":", 1)[0]
        assert (
            f"WOULD copy {repo}@{item['aws_digest']} -> registry.example/aorte-mvp/{name}:amd64-"
            in result.stdout
        ), name
    # skopeo rejects tag+digest references (#97).
    assert ":2.40.4@" not in result.stdout and ":15@" not in result.stdout


def test_mirror_fails_closed_when_the_amd64_pin_drifts(tmp_path: Path) -> None:
    images = json.loads(json.dumps(IMAGES))
    images["odoo"]["aws_digest"] = "sha256:" + "d" * 64  # not the amd64 child of the index
    result = run_mirror(tmp_path, images, "exit 1")
    assert result.returncode == 1
    assert "odoo aws_digest" in result.stderr and "not the linux/amd64 manifest" in result.stderr
    assert IMAGES["odoo"]["aws_digest"] in result.stderr  # shows the real amd64 child


def test_mirror_tags_an_existing_untagged_amd64_manifest(tmp_path: Path) -> None:
    # describe-images succeeds with no tags: present as a child of the old --all copy.
    result = run_mirror(tmp_path, IMAGES, 'echo "None"; exit 0')
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("WOULD tag") == len(IMAGES)
    assert "WOULD copy" not in result.stdout


# ---------------------------------------------------------------- ecr-prune

INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def fake_runner(images_in_use: list[str]) -> tuple[list[list[str]], aws_mvp.Runner]:
    calls: list[list[str]] = []
    pins = {name: item["aws_digest"] for name, item in IMAGES.items()}

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        calls.append(args)
        cmd = " ".join(args[1:3])
        out: object = {}
        if cmd == "sts get-caller-identity":
            out = {"Arn": "arn:aws:sts::000000000000:assumed-role/Admin/aws-mvp", "Account": "0"}
        elif cmd == "ecs describe-services":
            out = {"services": [{"taskDefinition": "core:9"}]}
        elif cmd == "ecs describe-task-definition":
            out = {
                "taskDefinition": {"containerDefinitions": [{"image": i} for i in images_in_use]}
            }
        elif cmd == "ecr describe-images":
            name = args[args.index("--repository-name") + 1].split("/", 1)[1]
            out = {
                "imageDetails": [
                    {"imageDigest": pins[name], "imageManifestMediaType": MANIFEST},
                    {
                        "imageDigest": IMAGES[name]["digest"],
                        "imageManifestMediaType": INDEX,
                        "imageSizeInBytes": 0,
                    },
                    {
                        "imageDigest": f"sha256:{name[:1]}" + "0" * 63,
                        "imageManifestMediaType": MANIFEST,
                        "imageSizeInBytes": 1_000_000,
                    },
                ]
            }
        return subprocess.CompletedProcess(args, 0, json.dumps(out), "")

    return calls, run


@pytest.fixture(autouse=True)
def _profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_PROFILE", "aws-mvp")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def deletes(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[1:3] == ["ecr", "batch-delete-image"]]


def test_prune_dry_run_deletes_nothing() -> None:
    in_use = [f"r/aorte-mvp/{n}@{i['aws_digest']}" for n, i in IMAGES.items()]
    calls, runner = fake_runner(in_use)
    assert aws_mvp.main(["ecr-prune"], runner=runner) == 0
    assert deletes(calls) == []


def test_prune_keeps_the_pin_and_deletes_indexes_first() -> None:
    in_use = [f"r/aorte-mvp/{n}@{i['aws_digest']}" for n, i in IMAGES.items()]
    calls, runner = fake_runner(in_use)
    assert aws_mvp.main(["ecr-prune", "--apply"], runner=runner) == 0
    done = deletes(calls)
    deleted = [a for c in done for a in c if a.startswith("imageDigest=")]
    pins = {f"imageDigest={i['aws_digest']}" for i in IMAGES.values()}
    assert not pins & set(deleted), "a pinned amd64 manifest must never be deleted"
    for name, item in IMAGES.items():
        repo_calls = [c for c in done if f"aorte-mvp/{name}" in c]
        if item["digest"] == item["aws_digest"]:
            assert all(f"imageDigest={item['digest']}" not in c for c in repo_calls)
            continue
        assert f"imageDigest={item['digest']}" in repo_calls[0], "index goes first"


def test_prune_refuses_while_the_task_still_uses_an_index() -> None:
    in_use = [f"r/aorte-mvp/odoo@{IMAGES['odoo']['digest']}"]
    calls, runner = fake_runner(in_use)
    assert aws_mvp.main(["ecr-prune", "--apply"], runner=runner) == 1
    assert deletes(calls) == []
