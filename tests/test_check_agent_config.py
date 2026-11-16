from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.check_agent_config import check

REPO = Path(__file__).resolve().parents[1]
SETTINGS = {
    "permissions": {
        "allow": ["Bash(make ci)"],
        "deny": ["Read(./.env)", "Read(./.env.local)", "Read(./secrets/**)"],
    }
}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / ".claude" / "skills" / "demo").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 'Use when demoing.'\n---\n\n# Demo\n"
    )
    (tmp_path / "AGENTS.md").write_text("# Rules\n\n## Skills\n\n- `demo`: a demo.\n")
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n\n# Notes\n")
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(SETTINGS))
    return tmp_path


def test_valid_tree_passes(tree: Path) -> None:
    assert check(tree) == []


def test_repository_agent_config_is_consistent() -> None:
    assert check(REPO) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        ("no_frontmatter", "frontmatter starting on line 1"),
        ("name_mismatch", "must equal the directory name"),
        ("empty_description", "description is empty"),
        ("long_description", "exceed 1536"),
        ("long_body", "longer than 500 lines"),
        ("github_skills", ".github/skills/ must not exist"),
        ("agents_mismatch", "differ from the directories"),
        ("no_claude_md", "CLAUDE.md is missing"),
        ("no_import", "@AGENTS.md"),
        ("no_deny", "must deny Read(./.env)"),
        ("broad_allow", "too broad"),
        ("bad_json", "not valid JSON"),
    ],
)
def test_each_violation_is_reported(tree: Path, mutate: str, expected: str) -> None:
    skill = tree / ".claude" / "skills" / "demo" / "SKILL.md"
    settings = tree / ".claude" / "settings.json"
    if mutate == "no_frontmatter":
        skill.write_text("# Demo\n")
    elif mutate == "name_mismatch":
        skill.write_text("---\nname: other\ndescription: x\n---\n")
    elif mutate == "empty_description":
        skill.write_text("---\nname: demo\ndescription:\n---\n")
    elif mutate == "long_description":
        skill.write_text(f"---\nname: demo\ndescription: {'x' * 1600}\n---\n")
    elif mutate == "long_body":
        skill.write_text("---\nname: demo\ndescription: x\n---\n" + "line\n" * 600)
    elif mutate == "github_skills":
        (tree / ".github" / "skills").mkdir(parents=True)
    elif mutate == "agents_mismatch":
        (tree / "AGENTS.md").write_text("## Skills\n\n- `demo`: a.\n- `ghost`: b.\n")
    elif mutate == "no_claude_md":
        (tree / "CLAUDE.md").unlink()
    elif mutate == "no_import":
        (tree / "CLAUDE.md").write_text("# no import here\n")
    elif mutate == "no_deny":
        settings.write_text(json.dumps({"permissions": {"allow": [], "deny": []}}))
    elif mutate == "broad_allow":
        settings.write_text(
            json.dumps({"permissions": {**SETTINGS["permissions"], "allow": ["Bash(git push *)"]}})
        )
    elif mutate == "bad_json":
        settings.write_text("{")
    assert any(expected in error for error in check(tree)), check(tree)


def test_missing_skill_file_and_unclosed_frontmatter(tree: Path) -> None:
    (tree / ".claude" / "skills" / "empty").mkdir()
    (tree / ".claude" / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n")
    errors = check(tree)
    assert any("empty: SKILL.md is missing" in e for e in errors)
    assert any("demo: SKILL.md needs frontmatter" in e for e in errors)


def test_copy_of_real_tree_still_passes(tmp_path: Path) -> None:
    for name in ("AGENTS.md", "CLAUDE.md"):
        shutil.copy(REPO / name, tmp_path / name)
    shutil.copytree(
        REPO / ".claude", tmp_path / ".claude", ignore=shutil.ignore_patterns("settings.local.json")
    )
    assert check(tmp_path) == []
