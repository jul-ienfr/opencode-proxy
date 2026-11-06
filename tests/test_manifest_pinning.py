"""Gluetun image pinning and atomic manifest installation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIGEST = "sha256:4af0c804003481e100180e1f8d4d5e9c48163fedc0f88d061881b615aef9d183"


def _module():
    path = ROOT / "scripts" / "fresh_chain.py"
    spec = importlib.util.spec_from_file_location("fresh_chain_manifest", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gluetun_image_is_digest_pinned():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    chain = (ROOT / "scripts" / "fresh_chain.py").read_text(encoding="utf-8")
    assert "qmcgaw/gluetun:latest" not in compose
    assert "qmcgaw/gluetun:latest" not in chain
    assert f"qmcgaw/gluetun@{DIGEST}" in compose
    assert f"qmcgaw/gluetun@{DIGEST}" in chain


def test_manifest_sync_is_atomic_and_verified(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "_station_volume_name", lambda station: "vol")
    calls: list[list[str]] = []

    class Proc:
        returncode = 0
        stderr = ""

    def run(command, **kwargs):
        calls.append(list(command))
        return Proc()

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.sync_manifest_to_station(
        1, {"servers": [{"hostname": "a.nordvpn.com"}]}
    )
    command = " ".join(calls[0])
    assert "nordvpn.json.tmp" in command
    assert "mv /gluetun/servers/nordvpn.json.tmp" in command
    assert "test -s /gluetun/servers/nordvpn.json" in command
