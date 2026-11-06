"""Auto transport chain: try WireGuard, then OpenVPN TCP, same server."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

from free.ip_registry.domain import BindResult, ProbeResult

ROOT = Path(__file__).resolve().parent.parent


def _module():
    path = ROOT / "scripts" / "fresh_chain.py"
    spec = importlib.util.spec_from_file_location("fresh_chain_auto", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_auto_chain_tries_next_transport_for_same_host(tmp_path, monkeypatch):
    module = _module()
    calls = []

    class Binder:
        async def assign(
            self,
            station,
            hostname,
            country=None,
            *,
            vpn_type=None,
            openvpn_protocol=None,
            **kwargs,
        ):
            calls.append((vpn_type, openvpn_protocol))
            if vpn_type == "wireguard":
                return BindResult(False, hostname, error="no-egress")
            return BindResult(True, hostname, egress_ip="1.2.3.4")

    class Probe:
        async def probe(self, station, model):
            return ProbeResult(status=200, tested_at=0.0, valid=True)

    async def probe_with_fallbacks(probe, station, model, fallbacks):
        return ProbeResult(status=200, tested_at=0.0, valid=True), model

    async def restore(*args, **kwargs):
        return False

    monkeypatch.setattr(module, "_active_hosts", lambda *a, **k: set())
    monkeypatch.setattr(module, "_active_egress_ips", lambda *a, **k: set())
    monkeypatch.setattr(module, "_ledger_exhausted_ips", lambda *a, **k: set())
    monkeypatch.setattr(module, "_clean_station_container", lambda *a, **k: None)
    monkeypatch.setattr(module, "mark", lambda *a, **k: None)
    monkeypatch.setattr(module, "_record_registry_winner", lambda *a, **k: None)
    monkeypatch.setattr(module, "_record_verified_spare", lambda *a, **k: None)
    monkeypatch.setattr(module, "_persist_env_pin", lambda *a, **k: None)
    monkeypatch.setattr(module, "_station_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(module, "_restore_previous_host", restore)
    monkeypatch.setattr(module, "_probe_with_fallbacks", probe_with_fallbacks)
    monkeypatch.setattr(
        module,
        "yaml_get",
        lambda section, key, default=None: False
        if key == "allocator.scan_audit_enabled"
        else default,
    )

    args = argparse.Namespace(
        station=2,
        transport="auto",
        claims_db=str(tmp_path / "claims.db"),
        rebind_limit=100,
        delay=0.0,
        max=1,
        model="m",
        probe_models=[],
        api_base="http://127.0.0.1:4000",
        claims=True,
    )
    candidates = [
        {
            "host": "both.nordvpn.com",
            "country": "Germany",
            "transports": ["wireguard", "openvpn-tcp"],
        }
    ]
    rc = await module._run_claims(
        args,
        candidates,
        Binder(),
        Probe(),
        tmp_path / "state.json",
        {},
        set(),
        {},
    )
    assert rc == 0
    assert calls == [("wireguard", None), ("openvpn", "tcp")]


def test_station_transport_follows_station_stack(tmp_path, monkeypatch):
    module = _module()
    (tmp_path / ".env").write_text(
        "VPN_TYPE_STATION2=wireguard\n"
        "VPN_TYPE_STATION3=openvpn\n"
        "OPENVPN_PROTOCOL=tcp\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(
        module, "yaml_get", lambda section, key, default=None: default
    )
    assert module._station_transport(2) == "wireguard"
    assert module._station_transport(3) == "openvpn-tcp"


def test_station_transport_defaults_to_configured_order(tmp_path, monkeypatch):
    module = _module()
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(
        module,
        "yaml_get",
        lambda section, key, default=None: "openvpn-udp"
        if key == "ovpn_protocol"
        else default,
    )
    assert module._station_transport(4) == ""


def test_fleet_usable_counts_model_usable(monkeypatch):
    module = _module()

    class Response:
        def json(self):
            return {
                "stations": [
                    {"station": 1, "model_usable": True},
                    {"station": 2, "client_usable": True},
                    {"station": 3, "station_usable": False},
                ]
            }

    monkeypatch.setattr(
        module.requests, "get", lambda *a, **k: Response()
    )
    assert module._fleet_usable() == 2
