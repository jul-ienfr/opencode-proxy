"""Tests du superviseur d'app (021 T014)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_supervisor():
    spec = importlib.util.spec_from_file_location(
        "app_supervisor_test", ROOT / "scripts" / "app_supervisor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_fail_limit_is_bounded() -> None:
    module = _load_supervisor()
    assert 5.0 <= module.HEALTH_FAIL_LIMIT_S <= 60.0


def test_health_ok_false_on_closed_port(monkeypatch) -> None:
    module = _load_supervisor()
    monkeypatch.setattr(module, "HEALTH_PORT", 1)
    assert module._health_ok(timeout=0.2) is False


def test_stop_file_constant_is_under_logs() -> None:
    module = _load_supervisor()
    assert module.STOP_FILE.endswith("app.stop")
    assert "logs" in module.STOP_FILE
