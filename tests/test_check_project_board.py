"""#123: the post-merge board check finds status/state mismatches. Hermetic (fake gh)."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_project_board import REPO, main, mismatches  # noqa: E402


def item(
    number: int, status: str | None, repo: str = REPO, kind: str = "Issue"
) -> dict[str, object]:
    return {
        "id": f"PVTI_{number}",
        "status": status,
        "content": {"type": kind, "number": number, "repository": repo},
    }


ITEMS = [
    item(88, "In Progress"),  # closed but not Done -> fixable
    item(101, "In Progress"),  # open, fine
    item(107, "Done"),  # closed and Done, fine
    item(120, "Done"),  # open but Done -> manual
    item(5, None, repo="someone/else"),  # other repository, ignored
    item(9, None, kind="DraftIssue"),  # draft, ignored
]
STATES = {88: "CLOSED", 101: "OPEN", 107: "CLOSED", 120: "OPEN"}


def test_finds_closed_not_done_and_open_done() -> None:
    found = mismatches(ITEMS, STATES)
    assert [(m.number, m.fixable) for m in found] == [(88, True), (120, False)]


def fake_gh(calls: list[list[str]], items: list[dict[str, object]]):  # type: ignore[no-untyped-def]
    def gh(args: Sequence[str]) -> str:
        args = list(args)
        calls.append(args)
        if args[:2] == ["project", "view"]:
            return json.dumps({"id": "PVT_14"})
        if args[:2] == ["project", "field-list"]:
            options = [{"id": "o-todo", "name": "Todo"}, {"id": "o-done", "name": "Done"}]
            return json.dumps(
                {"fields": [{"id": "F_status", "name": "Status", "options": options}]}
            )
        if args[:2] == ["project", "item-list"]:
            return json.dumps({"items": items})
        if args[:2] == ["issue", "list"]:
            return json.dumps([{"number": n, "state": s} for n, s in STATES.items()])
        return ""

    return gh


def edits(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:2] == ["project", "item-edit"]]


def test_dry_run_changes_nothing_and_fails() -> None:
    calls: list[list[str]] = []
    assert main([], gh=fake_gh(calls, ITEMS)) == 1
    assert edits(calls) == []


def test_fix_sets_only_closed_items_to_done() -> None:
    calls: list[list[str]] = []
    assert main(["--fix"], gh=fake_gh(calls, ITEMS)) == 1  # #120 still needs a manual check
    done = edits(calls)
    assert len(done) == 1
    assert "PVTI_88" in done[0] and "o-done" in done[0] and "PVT_14" in done[0]


def test_clean_board_passes() -> None:
    calls: list[list[str]] = []
    clean = [item(101, "In Progress"), item(107, "Done")]
    assert main([], gh=fake_gh(calls, clean)) == 0
