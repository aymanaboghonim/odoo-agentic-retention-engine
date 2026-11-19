"""Post-merge board check for GitHub Project 14 (#123, pr-workflow skill).

The project's built-in workflows should move a closed issue to Done, but the
"Pull request linked to issue" workflow (sets In Progress) can fire at the same time
as the close and win. This lists items whose status does not match the issue state:

  - closed issue whose status is not Done   (fixable: --fix sets Done)
  - open issue whose status is Done         (reported only; decide by hand)

Dry run by default. Needs `gh` with the `project` scope. Exit 1 if a mismatch remains.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

OWNER = "aymanaboghonim"
REPO = "aymanaboghonim/odoo-agentic-retention-engine"
PROJECT = 14

Gh = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class Mismatch:
    item_id: str
    number: int
    state: str
    status: str | None

    @property
    def fixable(self) -> bool:
        return self.state == "CLOSED"


def _gh(args: Sequence[str]) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def mismatches(items: list[dict[str, object]], states: dict[int, str]) -> list[Mismatch]:
    """Pure check: project items vs issue states (OPEN/CLOSED)."""
    found = []
    for item in items:
        content = item.get("content")
        if not isinstance(content, dict) or content.get("type") != "Issue":
            continue
        if content.get("repository") != REPO:
            continue
        number = int(content["number"])
        state = states.get(number)
        status = item.get("status")
        status = status if isinstance(status, str) else None
        if (state == "CLOSED" and status != "Done") or (state == "OPEN" and status == "Done"):
            found.append(Mismatch(str(item["id"]), number, state, status))
    return sorted(found, key=lambda m: m.number)


def main(argv: list[str] | None = None, gh: Gh = _gh) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fix", action="store_true", help="set closed issues to Done")
    args = parser.parse_args(argv)

    owner = ["--owner", OWNER]
    project = json.loads(gh(["project", "view", str(PROJECT), *owner, "--format", "json"]))
    fields = json.loads(gh(["project", "field-list", str(PROJECT), *owner, "--format", "json"]))
    status_field = next(f for f in fields["fields"] if f["name"] == "Status")
    done = next(o["id"] for o in status_field["options"] if o["name"] == "Done")
    items = json.loads(
        gh(["project", "item-list", str(PROJECT), *owner, "--limit", "1000", "--format", "json"])
    )["items"]
    issues = json.loads(
        gh(
            [
                "issue",
                "list",
                "--repo",
                REPO,
                "--state",
                "all",
                "--limit",
                "1000",
                "--json",
                "number,state",
            ]
        )
    )
    states = {int(i["number"]): str(i["state"]) for i in issues}

    found = mismatches(items, states)
    for m in found:
        action = "set Done" if m.fixable else "check by hand"
        print(
            f"#{m.number}: issue {m.state.lower()}, board status {m.status or 'none'} -> {action}"
        )
        if m.fixable and args.fix:
            gh(
                [
                    "project", "item-edit", "--id", m.item_id, "--project-id", project["id"],
                    "--field-id", status_field["id"], "--single-select-option-id", done,
                ]
            )  # fmt: skip
    if not found:
        print(f"board ok: {len(items)} items match their issue state")
        return 0
    if args.fix:
        left = [m for m in found if not m.fixable]
        print(f"fixed {len(found) - len(left)}; {len(left)} left for a manual check")
        return 1 if left else 0
    print("dry run: re-run with --fix to set closed issues to Done")
    return 1


if __name__ == "__main__":
    sys.exit(main())
