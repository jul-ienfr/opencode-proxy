"""Chain lock ownership: a dead chain must not block repair for an hour."""

from __future__ import annotations

import json
import os
import time

from scripts import fresh_chain


def _lock(tmp_path, pid: int):
    path = tmp_path / "fresh_chain_2_openvpn-tcp.lock"
    path.write_text(
        json.dumps({"pid": pid, "ts": time.time()}),
        encoding="utf-8",
    )
    return path


def test_dead_owner_lock_is_reclaimed(tmp_path, monkeypatch):
    path = _lock(tmp_path, 999_999)
    monkeypatch.setattr(fresh_chain, "_lock_path", lambda *a, **k: path)
    monkeypatch.setattr(fresh_chain, "_pid_alive", lambda pid: False)
    assert fresh_chain.acquire_lock(2, "openvpn-tcp") is True
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_live_owner_lock_is_respected(tmp_path, monkeypatch):
    path = _lock(tmp_path, 1234)
    monkeypatch.setattr(fresh_chain, "_lock_path", lambda *a, **k: path)
    monkeypatch.setattr(fresh_chain, "_pid_alive", lambda pid: True)
    assert fresh_chain.acquire_lock(2, "openvpn-tcp") is False


def test_release_does_not_delete_another_owner(tmp_path, monkeypatch):
    path = _lock(tmp_path, 1234)
    monkeypatch.setattr(fresh_chain, "_lock_path", lambda *a, **k: path)
    fresh_chain.release_lock(2, "openvpn-tcp")
    assert path.exists()


def test_release_deletes_own_lock(tmp_path, monkeypatch):
    path = _lock(tmp_path, os.getpid())
    monkeypatch.setattr(fresh_chain, "_lock_path", lambda *a, **k: path)
    fresh_chain.release_lock(2, "openvpn-tcp")
    assert not path.exists()
