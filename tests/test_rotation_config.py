"""Typed rotation configuration: defaults, normalization and validation."""

from __future__ import annotations

from rotation.infrastructure.config import schema


def test_default_config_is_valid():
    cfg = schema.load_rotation_config()
    assert cfg.selection.unique_ip is True
    # Le parc normal est en WireGuard (T018) ; `auto` reste le fallback
    # uniquement quand `vpn_stack` est absent ou invalide.
    assert cfg.transport.mode == "wireguard"
    assert cfg.transport.order == ("wireguard", "openvpn-udp", "openvpn-tcp")
    assert abs(sum(cfg.selection.weights.__dict__.values()) - 1.0) < 1e-9


def test_weights_are_normalized():
    w = schema.ScoreWeights(success=2, latency=1, throughput=1, stability=0, freshness=0)
    n = w.normalized()
    assert abs(n.success - 0.5) < 1e-9
    assert abs(n.latency - 0.25) < 1e-9


def test_invalid_transport_is_dropped(monkeypatch):
    values = {
        "transport_order": ["wireguard", "bad-transport", "openvpn-tcp"],
        "vpn_stack": "bad-mode",
    }
    monkeypatch.setattr(
        schema,
        "yaml_get",
        lambda section, key, default=None: values.get(key, default),
    )
    cfg = schema.load_rotation_config()
    assert cfg.transport.order == ("wireguard", "openvpn-tcp")
    assert cfg.transport.mode == "auto"
