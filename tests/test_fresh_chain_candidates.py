"""Candidate ordering: measured speed first, then load, no duplicates."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _module():
    path = ROOT / "scripts" / "fresh_chain.py"
    spec = importlib.util.spec_from_file_location("fresh_chain_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _api_server(host: str, country: str, station: str, load: int) -> dict:
    return {
        "hostname": host,
        "station": station,
        "load": load,
        "status": "online",
        "technologies": [{"identifier": "wireguard_udp"}],
        "locations": [{"country": {"name": country}}],
    }


def test_measured_speed_beats_country_and_load(tmp_path, monkeypatch):
    module = _module()
    api = [
        _api_server("de1.nordvpn.com", "Germany", "10.0.0.1", 1),
        _api_server("al1.nordvpn.com", "Albania", "10.0.1.1", 5),
        _api_server("al2.nordvpn.com", "Albania", "10.0.2.1", 2),
    ]
    embedded = tmp_path / "embedded.json"
    embedded.write_text('{"servers": []}', encoding="utf-8")
    monkeypatch.setattr(module, "_fetch_api", lambda: api)
    monkeypatch.setattr(module, "_registry_hosts", lambda: [])
    monkeypatch.setattr(module, "_active_hosts", lambda: set())
    monkeypatch.setattr(module, "EMBEDDED", embedded)
    monkeypatch.setattr(
        module,
        "_host_quality",
        lambda: {
            "al1.nordvpn.com": {"n": 5, "tps": 80.0},
            "al2.nordvpn.com": {"n": 5, "tps": 35.0},
            "de1.nordvpn.com": {"n": 5, "tps": 10.0},
        },
    )

    hosts = [row["host"] for row in module.build_candidates()]

    # Measured throughput wins over country and API load.
    assert hosts.index("al1.nordvpn.com") < hosts.index("al2.nordvpn.com")
    assert hosts.index("al2.nordvpn.com") < hosts.index("de1.nordvpn.com")



async def test_probe_falls_back_to_another_model_bucket():
    module = _module()

    class _Result:
        def __init__(self, status):
            self.status = status
            self.retry_after_s = 10.0 if status == 429 else 0.0

    class _Probe:
        def __init__(self):
            self.calls = []

        async def probe(self, station, model):
            self.calls.append(model)
            return _Result(429 if model == "primary" else 200)

    probe = _Probe()
    result, model = await module._probe_with_fallbacks(
        probe, 2, "primary", ["glm", "mimo"]
    )
    assert (result.status, model) == (200, "glm")
    assert probe.calls == ["primary", "glm"]


async def test_probe_returns_primary_429_when_all_buckets_fail():
    module = _module()

    class _Result:
        status = 429
        retry_after_s = 10.0

    class _Probe:
        async def probe(self, station, model):
            return _Result()

    result, model = await module._probe_with_fallbacks(
        _Probe(), 2, "primary", ["glm", "mimo"]
    )
    assert (result.status, model) == (429, "primary")


def test_known_200_then_load_without_country_bias(tmp_path, monkeypatch):
    module = _module()
    api = [
        _api_server("us1.nordvpn.com", "United States", "10.9.0.1", 1),
        _api_server("al9.nordvpn.com", "Albania", "10.8.0.1", 9),
        _api_server("de2.nordvpn.com", "Germany", "10.7.0.1", 0),
    ]
    embedded = tmp_path / "embedded.json"
    embedded.write_text('{"servers": []}', encoding="utf-8")
    monkeypatch.setattr(module, "_fetch_api", lambda: api)
    monkeypatch.setattr(
        module, "_registry_hosts", lambda: ["us1.nordvpn.com", "al9.nordvpn.com"]
    )
    monkeypatch.setattr(module, "_active_hosts", lambda: set())
    monkeypatch.setattr(module, "EMBEDDED", embedded)
    monkeypatch.setattr(module, "_host_quality", lambda: {})
    monkeypatch.setattr(
        module,
        "yaml_get",
        lambda section, key, default=None: "Albania"
        if (section, key) == ("ip_rotation", "server_countries")
        else default,
    )
    hosts = [row["host"] for row in module.build_candidates()]
    # Both known-200 hosts outrank the never-seen one, and load orders them:
    # the configured country has no bonus anymore.
    assert hosts.index("al9.nordvpn.com") < hosts.index("de2.nordvpn.com")
    assert hosts.index("us1.nordvpn.com") < hosts.index("de2.nordvpn.com")


def test_transport_filters_candidates(tmp_path, monkeypatch):
    module = _module()
    api = [
        {
            **_api_server("wg.nordvpn.com", "Albania", "10.0.0.1", 1),
            "technologies": [{"identifier": "wireguard_udp"}],
        },
        {
            **_api_server("ovpn.nordvpn.com", "Albania", "10.0.0.2", 2),
            "technologies": [{"identifier": "openvpn_tcp"}],
        },
    ]
    embedded = tmp_path / "embedded.json"
    embedded.write_text('{"servers": []}', encoding="utf-8")
    monkeypatch.setattr(module, "_fetch_api", lambda: api)
    monkeypatch.setattr(module, "_registry_hosts", lambda: [])
    monkeypatch.setattr(module, "_active_hosts", lambda: set())
    monkeypatch.setattr(module, "EMBEDDED", embedded)
    monkeypatch.setattr(
        module,
        "yaml_get",
        lambda section, key, default=None: "Albania"
        if (section, key) == ("ip_rotation", "server_countries")
        else default,
    )
    wg = {row["host"] for row in module.build_candidates("wireguard")}
    ovpn = {row["host"] for row in module.build_candidates("openvpn-tcp")}
    assert wg == {"wg.nordvpn.com"}
    assert ovpn == {"ovpn.nordvpn.com"}
