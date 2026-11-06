"""Debt gate parser/exit logic."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _module():
    path = ROOT / "scripts" / "debt_gate.py"
    spec = importlib.util.spec_from_file_location("debt_gate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_p0_must_be_closed(tmp_path):
    module = _module()
    debt = tmp_path / "debt.md"
    debt.write_text(
        "| D-01 | one | p | i | P0 | 1 | done |\n"
        "| D-02 | two | p | i | P1 | 1 | done |\n",
        encoding="utf-8",
    )
    status = tmp_path / "status.json"
    status.write_text('{"D-01":"open"}', encoding="utf-8")
    debts = module.read_debts(debt)
    statuses = module.read_statuses(status)
    assert module.open_p0(debts, statuses) == [("D-01", "P0")]

    status.write_text('{"D-01":"closed"}', encoding="utf-8")
    assert module.open_p0(debts, module.read_statuses(status)) == []
