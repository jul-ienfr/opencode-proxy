"""Préflight DNS/port avant bind — feature 019, tâche T311."""

from __future__ import annotations

import scripts.preflight as pf


def test_dns_failure_blocks_before_any_bind(monkeypatch):
    calls = []
    monkeypatch.setattr(pf, "_resolve", lambda host, timeout=2.0: [])
    monkeypatch.setattr(pf, "_tcp_open", lambda *a, **k: calls.append(1) or True)
    ok, reason, info = pf.preflight_host("dead.example", "wireguard")
    assert ok is False and reason == "dns-failed"
    assert calls == [], "aucun test de port après un échec DNS"


def test_openvpn_tcp_closed_is_rejected(monkeypatch):
    monkeypatch.setattr(pf, "_resolve", lambda host, timeout=2.0: ["203.0.113.5"])
    monkeypatch.setattr(pf, "_tcp_open", lambda *a, **k: False)
    ok, reason, _ = pf.preflight_host("host.example", "openvpn-tcp")
    assert ok is False and reason == "port-unreachable"


def test_wireguard_tcp_closed_is_advisory(monkeypatch):
    monkeypatch.setattr(pf, "_resolve", lambda host, timeout=2.0: ["203.0.113.5"])
    monkeypatch.setattr(pf, "_tcp_open", lambda *a, **k: False)
    ok, reason, info = pf.preflight_host("host.example", "wireguard")
    assert ok is True and reason == "dns-ok-tcp-closed-advisory"
    assert info["resolved"] == ["203.0.113.5"]


def test_openvpn_tcp_open_passes(monkeypatch):
    monkeypatch.setattr(pf, "_resolve", lambda host, timeout=2.0: ["203.0.113.5"])
    monkeypatch.setattr(pf, "_tcp_open", lambda *a, **k: True)
    ok, reason, info = pf.preflight_host("host.example", "openvpn-udp")
    assert ok is True and reason == "ok"
    assert info["elapsed_ms"] >= 0


def test_empty_host_is_rejected():
    ok, reason, _ = pf.preflight_host("", "wireguard")
    assert ok is False and reason == "empty-host"
