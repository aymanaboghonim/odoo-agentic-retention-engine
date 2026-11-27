"""Enforce engine module boundaries (#111, ADR-0002, ADR-0012, ADR-0028).

Parses every module in `src/engine/` with `ast`, builds the import graph between engine
modules (including imports inside functions), and checks the rules below on the transitive
closure, so an indirect import also counts. No new dependency; runs in `make ci`.

Rules:
  BND-1 The approval path (`approval_api`, `approval_cli`, `auth`) never reaches the
        Odoo adapter, channel dispatch, execution, or the LLM provider. Only the worker
        changes Odoo or sends messages (ADR-0012).
  BND-2 The safety gate (`policy`) imports no engine module, so money rules cannot depend
        on the model, Odoo, or the database.
  BND-3 The LLM provider adapter (`proposal_provider`) imports no engine module, so the
        model layer cannot reach Odoo, channels, or the database.
  BND-4 Only `execution`, `worker`, and `erp_cli` import `channels` directly.
  BND-5 Telemetry (#126, OBS-10) imports no engine module, and the safety gate and the LLM
        adapter never import telemetry, so neither money rules nor the model layer depend
        on what is exported.
  REC-1 The reconcile evidence check (`reconcile`, `reconcile_cli`, #131) never reaches
        channel dispatch, execution, or the LLM provider; it reads Odoo only through a
        `search_read` lookup (spec docs/specs/execution-reconciliation.md).
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "engine"
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "src" / PACKAGE

SIDE_EFFECTS = frozenset({"odoo", "channels", "execution", "proposal_provider"})


@dataclass(frozen=True)
class Rule:
    id: str
    sources: frozenset[str]
    forbidden: frozenset[str] | None  # None: no engine import at all
    transitive: bool
    why: str


RULES = (
    Rule(
        "BND-1",
        frozenset({"approval_api", "approval_cli", "auth"}),
        SIDE_EFFECTS,
        True,
        "the approval path must not reach Odoo writes, dispatch, execution, or the LLM (ADR-0012)",
    ),
    Rule(
        "BND-2",
        frozenset({"policy"}),
        None,
        False,
        "the safety gate must stay free of engine imports",
    ),
    Rule(
        "BND-3",
        frozenset({"proposal_provider"}),
        None,
        False,
        "the LLM adapter must not reach other engine modules",
    ),
    Rule(
        "BND-5",
        frozenset({"policy", "proposal_provider"}),
        frozenset({"telemetry"}),
        True,
        "the gate and the LLM adapter must not depend on telemetry (OBS-10)",
    ),
    Rule(
        "REC-1",
        frozenset({"reconcile", "reconcile_cli", "reconcile_evidence", "reconcile_stage"}),
        frozenset({"channels", "execution", "proposal_provider"}),
        True,
        "the reconcile check must not reach dispatch, execution, or the LLM (ADR-0030)",
    ),
    Rule(
        "BND-5",
        frozenset({"telemetry"}),
        None,
        False,
        "telemetry must stay a leaf module (OBS-10)",
    ),
)
CHANNEL_IMPORTERS = frozenset({"execution", "worker", "erp_cli"})


def engine_imports(path: Path, modules: set[str]) -> set[str]:
    """Engine modules imported by one file, at any depth in the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # relative: from . import x / from .x import y
                if node.module:
                    found.add(node.module.split(".")[0])
                else:
                    found.update(alias.name for alias in node.names)
            elif node.module == PACKAGE:
                found.update(alias.name for alias in node.names)
            elif node.module and node.module.startswith(PACKAGE + "."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(PACKAGE + "."):
                    found.add(alias.name.split(".")[1])
    return {name for name in found if name in modules and name != path.stem}


def import_graph(root: Path) -> dict[str, set[str]]:
    files = {p.stem: p for p in root.glob("*.py") if p.stem != "__init__"}
    names = set(files)
    return {name: engine_imports(path, names) for name, path in files.items()}


def closure(graph: dict[str, set[str]], start: str) -> dict[str, list[str]]:
    """Every module reachable from start, with one import path to it."""
    paths: dict[str, list[str]] = {}
    stack = [(start, [start])]
    while stack:
        node, path = stack.pop()
        for nxt in sorted(graph.get(node, ())):
            if nxt not in paths:
                paths[nxt] = [*path, nxt]
                stack.append((nxt, paths[nxt]))
    return paths


def violations(graph: dict[str, set[str]]) -> list[str]:
    problems: list[str] = []
    for rule in RULES:
        for source in sorted(rule.sources):
            if source not in graph:
                problems.append(f"{rule.id}: module `{source}` not found; update the rule")
                continue
            if rule.transitive:
                reach = closure(graph, source)
                for target in sorted(set(reach) & (rule.forbidden or set())):
                    chain = " -> ".join(reach[target])
                    problems.append(f"{rule.id}: {chain} ({rule.why})")
            else:
                direct = graph[source]
                bad = direct if rule.forbidden is None else direct & rule.forbidden
                for target in sorted(bad):
                    problems.append(f"{rule.id}: {source} -> {target} ({rule.why})")
    for module in sorted(graph):
        if "channels" in graph[module] and module not in CHANNEL_IMPORTERS:
            problems.append(
                f"BND-4: {module} -> channels (only {', '.join(sorted(CHANNEL_IMPORTERS))} "
                "may import channel dispatch)"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="engine package directory")
    args = parser.parse_args(argv)
    graph = import_graph(args.root)
    if not graph:
        print(f"FAILED (import boundaries): no modules in {args.root}", file=sys.stderr)
        return 1
    problems = violations(graph)
    for problem in problems:
        print(f"FAILED (import boundaries): {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"import boundaries ok: {len(graph)} modules, rules BND-1..BND-5, REC-1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
