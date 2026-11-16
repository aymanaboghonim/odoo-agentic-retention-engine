"""Check the Claude Code agent setup (#91, ADR-0021, facts F-060 and F-061).

A skill the harness cannot load is a silent failure (#86 found two). This gate keeps
`.claude/skills/`, `AGENTS.md`, `CLAUDE.md`, and `.claude/settings.json` consistent.
"""

import json
import re
from pathlib import Path

MAX_LISTING_CHARS = 1536  # F-060: description + when_to_use is truncated at this length
MAX_SKILL_LINES = 500  # F-060: keep SKILL.md under 500 lines
REQUIRED_DENY = ("Read(./.env)", "Read(./.env.local)", "Read(./secrets/**)")


def _frontmatter(text: str) -> dict[str, str] | None:
    """Top-level `key: value` pairs between line-1 `---` and the next `---`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return values
        match = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if match:
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[match.group(1)] = value
    return None  # never closed


def _agents_skill_names(agents: str) -> set[str]:
    section = agents.split("## Skills", 1)[-1]
    section = section.split("\n## ", 1)[0]
    return set(re.findall(r"^- `([a-z0-9-]+)`:", section, re.MULTILINE))


def check(root: Path) -> list[str]:
    errors: list[str] = []
    if (root / ".github" / "skills").exists():
        errors.append(".github/skills/ must not exist; skills live in .claude/skills/")

    skills_dir = root / ".claude" / "skills"
    directories = (
        {p.name for p in skills_dir.iterdir() if p.is_dir()} if skills_dir.is_dir() else set()
    )
    for name in sorted(directories):
        path = skills_dir / name / "SKILL.md"
        if not path.is_file():
            errors.append(f"{name}: SKILL.md is missing")
            continue
        text = path.read_text(encoding="utf-8")
        fields = _frontmatter(text)
        if fields is None:
            errors.append(f"{name}: SKILL.md needs frontmatter starting on line 1 with ---")
            continue
        if fields.get("name") != name:
            errors.append(f"{name}: frontmatter name must equal the directory name")
        description = fields.get("description", "")
        if not description:
            errors.append(f"{name}: frontmatter description is empty")
        if len(description) + len(fields.get("when_to_use", "")) > MAX_LISTING_CHARS:
            errors.append(f"{name}: description + when_to_use exceed {MAX_LISTING_CHARS} chars")
        if len(text.splitlines()) > MAX_SKILL_LINES:
            errors.append(f"{name}: SKILL.md is longer than {MAX_SKILL_LINES} lines")

    agents = root / "AGENTS.md"
    if not agents.is_file():
        errors.append("AGENTS.md is missing")
    elif _agents_skill_names(agents.read_text(encoding="utf-8")) != directories:
        errors.append("skills listed in AGENTS.md differ from the directories in .claude/skills/")

    claude = root / "CLAUDE.md"
    if not claude.is_file():
        errors.append("CLAUDE.md is missing")
    elif not re.search(r"^@AGENTS\.md\s*$", claude.read_text(encoding="utf-8"), re.MULTILINE):
        errors.append("CLAUDE.md must import AGENTS.md with a line containing @AGENTS.md")

    settings = root / ".claude" / "settings.json"
    if not settings.is_file():
        errors.append(".claude/settings.json is missing")
    else:
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            errors.append(".claude/settings.json is not valid JSON")
        else:
            permissions = data.get("permissions", {}) if isinstance(data, dict) else {}
            deny = permissions.get("deny", []) if isinstance(permissions, dict) else []
            allow = permissions.get("allow", []) if isinstance(permissions, dict) else []
            for rule in REQUIRED_DENY:
                if rule not in deny:
                    errors.append(f".claude/settings.json must deny {rule}")
            for rule in allow:
                if rule in {"Bash", "Bash(*)"} or "git push" in str(rule):
                    errors.append(f".claude/settings.json allow rule is too broad: {rule}")
    return errors


def main() -> int:
    errors = check(Path("."))
    for error in errors:
        print(f"agent config: {error}")
    if not errors:
        print("Agent config checks passed.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
