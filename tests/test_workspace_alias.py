"""Alias de workspace pour les chemins factices `/workspace/oc-<station>`."""

from __future__ import annotations

import os

import pytest

from free.station_identity import ensure_workspace_alias


@pytest.mark.skipif(os.name != "nt", reason="jonctions Windows uniquement")
def test_ensure_workspace_alias_creates_junction(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "AGENTS.md").write_text("hello", encoding="utf-8")
    base = tmp_path / "workspace"
    assert ensure_workspace_alias("/workspace/oc-warp-test", str(real), root=base)
    link = base / "oc-warp-test"
    assert link.exists()
    assert (link / "AGENTS.md").read_text(encoding="utf-8") == "hello"


@pytest.mark.skipif(os.name != "nt", reason="jonctions Windows uniquement")
def test_ensure_workspace_alias_follows_new_workspace(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "marker.txt").write_text("first", encoding="utf-8")
    (second / "marker.txt").write_text("second", encoding="utf-8")
    base = tmp_path / "workspace"
    assert ensure_workspace_alias("/workspace/oc-warp-test", str(first), root=base)
    link = base / "oc-warp-test"
    assert (link / "marker.txt").read_text(encoding="utf-8") == "first"
    assert ensure_workspace_alias("/workspace/oc-warp-test", str(second), root=base)
    assert (link / "marker.txt").read_text(encoding="utf-8") == "second"
