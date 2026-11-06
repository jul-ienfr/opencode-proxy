"""Single-source .env generation for server_countries."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _module():
    path = ROOT / "scripts" / "make_credentials_env.py"
    spec = importlib.util.spec_from_file_location("make_credentials_env", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_empty_server_countries_means_any_country(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    env = tmp_path / ".env"
    config.write_text("ip_rotation:\n  server_countries: ''\n", encoding="utf-8")
    env.write_text("KEEP=1\nSERVER_COUNTRIES=stale\n", encoding="utf-8")
    module = _module()
    monkeypatch.setattr(module, "CONFIG_PATH", str(config))
    monkeypatch.setattr(module, "ENV_PATH", str(env))
    assert module._sync_server_countries() is True
    assert "SERVER_COUNTRIES=\n" in env.read_text(encoding="utf-8")
    assert "KEEP=1" in env.read_text(encoding="utf-8")


def test_non_empty_server_countries_is_compacted(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    env = tmp_path / ".env"
    config.write_text(
        "ip_rotation:\n  server_countries: Germany, France ,Italy\n",
        encoding="utf-8",
    )
    env.write_text("", encoding="utf-8")
    module = _module()
    monkeypatch.setattr(module, "CONFIG_PATH", str(config))
    monkeypatch.setattr(module, "ENV_PATH", str(env))
    assert module._sync_server_countries() is True
    assert "SERVER_COUNTRIES=Germany,France,Italy\n" in env.read_text(
        encoding="utf-8"
    )
