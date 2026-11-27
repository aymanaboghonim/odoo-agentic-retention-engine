"""BND-1..BND-5 and REC-1: engine module boundaries (#111, #126, #131). Hermetic, no database."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_import_boundaries import import_graph, main, violations  # noqa: E402

BASE = {
    "approval_api": "from engine import auth, db\n",
    "approval_cli": "",
    "auth": "",
    "db": "from engine.policy import Gate\n",
    "policy": "import decimal\n",
    "proposal_provider": "import json\n",
    "execution": "from engine import channels, odoo\n",
    "channels": "",
    "odoo": "from . import policy\n",
    "worker": "from engine.channels import send\n",
    "erp_cli": "from engine import channels\n",
    "telemetry": "import logging\n",
    "reconcile": "import uuid\n",
    "reconcile_cli": "from engine import db, odoo, reconcile\n",
}


def tree(tmp_path: Path, **overrides: str) -> Path:
    root = tmp_path / "engine"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    for name, body in {**BASE, **overrides}.items():
        (root / f"{name}.py").write_text(body, encoding="utf-8")
    return root


def problems(root: Path) -> list[str]:
    return violations(import_graph(root))


def test_real_engine_passes() -> None:
    assert main([]) == 0


def test_clean_tree_passes(tmp_path: Path) -> None:
    assert problems(tree(tmp_path)) == []


def test_approval_path_cannot_reach_side_effects_even_indirectly(tmp_path: Path) -> None:
    # approval_api -> db -> execution: indirect reach still counts.
    root = tree(
        tmp_path,
        db="from engine.policy import Gate\n\ndef f():\n    from engine import execution\n",
    )
    found = problems(root)
    assert any(p.startswith("BND-1: approval_api -> db -> execution") for p in found), found
    assert main(["--root", str(root)]) == 1


def test_gate_imports_no_engine_module(tmp_path: Path) -> None:
    found = problems(tree(tmp_path, policy="from engine import proposal_provider\n"))
    assert any(p.startswith("BND-2: policy -> proposal_provider") for p in found), found


def test_llm_adapter_imports_no_engine_module(tmp_path: Path) -> None:
    found = problems(tree(tmp_path, proposal_provider="from .odoo import OdooClient\n"))
    assert any(p.startswith("BND-3: proposal_provider -> odoo") for p in found), found


def test_only_execution_paths_import_channels(tmp_path: Path) -> None:
    found = problems(tree(tmp_path, odoo="import engine.channels\n"))
    assert any(p.startswith("BND-4: odoo -> channels") for p in found), found


@pytest.mark.parametrize("missing", ["policy", "auth"])
def test_missing_rule_module_fails_closed(tmp_path: Path, missing: str) -> None:
    root = tree(tmp_path)
    (root / f"{missing}.py").unlink()
    assert any("not found" in p for p in problems(root))


def test_gate_and_llm_adapter_never_import_telemetry(tmp_path: Path) -> None:
    # OBS-10 (#126): money rules and the model layer cannot depend on what is exported.
    for module in ("policy", "proposal_provider"):
        (tmp_path / module).mkdir()
        found = problems(tree(tmp_path / module, **{module: "from engine import telemetry\n"}))
        assert any(p.startswith(f"BND-5: {module} -> telemetry") for p in found), found


def test_telemetry_imports_no_engine_module(tmp_path: Path) -> None:
    found = problems(tree(tmp_path, telemetry="from engine import db\n"))
    assert any(p.startswith("BND-5: telemetry -> db") for p in found), found


def test_reconcile_check_cannot_reach_dispatch_or_execution(tmp_path: Path) -> None:
    """REC-1 (#131): the evidence check reads Odoo but never reaches dispatch or execution."""
    for module, target in (("reconcile_cli", "execution"), ("reconcile", "channels")):
        (tmp_path / module).mkdir()
        body = f"from engine import {target}\n"
        found = problems(tree(tmp_path / module, **{module: body}))
        assert any(p.startswith(f"REC-1: {module} -> {target}") for p in found), found
