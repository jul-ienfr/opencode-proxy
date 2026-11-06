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
    monkeypatch.setattr(module, "_verified_spares", lambda **kw: [])
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
    monkeypatch.setattr(module, "_verified_spares", lambda **kw: [])
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
    monkeypatch.setattr(module, "_verified_spares", lambda **kw: [])
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


def test_measured_success_beats_api_source(tmp_path, monkeypatch):
    """A Gluetun-only host with better recent success outranks an API host."""
    module = _module()
    monkeypatch.setattr(module, "_verified_spares", lambda **kw: [])
    api = [_api_server("api-low.nordvpn.com", "Germany", "10.0.0.1", 1)]
    embedded = tmp_path / "embedded.json"
    embedded.write_text(
        '{"servers": [{"hostname": "gl-high.nordvpn.com", '
        '"vpn": "wireguard", "wgpubkey": "key", "country": "Netherlands", '
        '"ips": ["10.0.0.2"]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_fetch_api", lambda: api)
    monkeypatch.setattr(module, "_registry_hosts", lambda: [])
    monkeypatch.setattr(module, "_active_hosts", lambda: set())
    monkeypatch.setattr(module, "EMBEDDED", embedded)
    monkeypatch.setattr(module, "_host_quality", lambda: {})
    monkeypatch.setattr(
        module,
        "_host_success",
        lambda: {
            "api-low.nordvpn.com": {"rate": 0.2, "latency_score": 0.2},
            "gl-high.nordvpn.com": {"rate": 0.9, "latency_score": 0.9},
        },
    )

    hosts = [row["host"] for row in module.build_candidates()]
    assert hosts.index("gl-high.nordvpn.com") < hosts.index("api-low.nordvpn.com")


def test_score_weights_are_configurable(tmp_path, monkeypatch):
    module = _module()
    api = [
        _api_server("low.nordvpn.com", "Germany", "10.0.0.1", 1),
        _api_server("high.nordvpn.com", "France", "10.0.0.2", 50),
    ]
    embedded = tmp_path / "embedded.json"
    embedded.write_text('{"servers": []}', encoding="utf-8")
    monkeypatch.setattr(module, "_fetch_api", lambda: api)
    monkeypatch.setattr(module, "_registry_hosts", lambda: [])
    monkeypatch.setattr(module, "_active_hosts", lambda: set())
    monkeypatch.setattr(module, "EMBEDDED", embedded)
    monkeypatch.setattr(module, "_host_quality", lambda: {})
    monkeypatch.setattr(module, "_verified_spares", lambda **kw: [])
    monkeypatch.setattr(
        module,
        "_host_success",
        lambda: {
            "low.nordvpn.com": {"rate": 0.1, "latency_score": 0.1, "freshness": 0.1},
            "high.nordvpn.com": {"rate": 0.9, "latency_score": 0.9, "freshness": 0.9},
        },
    )
    monkeypatch.setattr(
        module,
        "yaml_get",
        lambda section, key, default=None: (
            1.0
            if key == "selection.score_weights.success"
            else 0.0
            if key.startswith("selection.score_weights.")
            else default
        ),
    )
    hosts = [row["host"] for row in module.build_candidates()]
    assert hosts.index("high.nordvpn.com") < hosts.index("low.nordvpn.com")


def test_transport_parts_maps_openvpn_protocols():
    module = _module()
    assert module._transport_parts("wireguard") == ("wireguard", None)
    assert module._transport_parts("openvpn-udp") == ("openvpn", "udp")
    assert module._transport_parts("openvpn-tcp") == ("openvpn", "tcp")
    assert module._transport_parts("auto") == ("wireguard", None)


def test_build_auto_candidates_merges_transports(monkeypatch):
    module = _module()
    monkeypatch.setattr(
        module, "_transport_order", lambda: ("wireguard", "openvpn-udp", "openvpn-tcp")
    )
    rows = {
        "wireguard": [
            {
                "host": "both.nordvpn.com",
                "country": "Germany",
                "source": "api",
                "score": 0.7,
                "in_api": True,
                "load": 10,
            }
        ],
        "openvpn-udp": [
            {
                "host": "both.nordvpn.com",
                "country": "Germany",
                "source": "api",
                "score": 0.9,
                "in_api": True,
                "load": 10,
            },
            {
                "host": "udp-only.nordvpn.com",
                "country": "France",
                "source": "gluetun",
                "score": 0.4,
                "in_api": False,
                "load": 20,
            },
        ],
        "openvpn-tcp": [],
    }
    monkeypatch.setattr(
        module,
        "build_candidates",
        lambda transport, valid, station=None: rows[transport],
    )
    merged = module.build_auto_candidates({}, station=2)
    by_host = {row["host"]: row for row in merged}
    assert by_host["both.nordvpn.com"]["transports"] == [
        "wireguard",
        "openvpn-udp",
    ]
    assert by_host["both.nordvpn.com"]["score"] == 0.9
    assert by_host["udp-only.nordvpn.com"]["transports"] == ["openvpn-udp"]
    assert merged[0]["host"] == "both.nordvpn.com"


def test_read_station_manifest_parses_the_station_volume(monkeypatch):
    module = _module()

    class _Proc:
        returncode = 0
        stdout = '{"servers": [{"hostname": "a.nordvpn.com"}]}'

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(module, "_station_volume_name", lambda station: "vol")
    payload = module.read_station_manifest(2)
    assert payload["servers"][0]["hostname"] == "a.nordvpn.com"


def test_manifest_hosts_selects_the_bound_transport(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_verified_spares", lambda **kw: [])
    payload = {
        "servers": [
            {
                "hostname": "wg.nordvpn.com",
                "vpn": "wireguard",
                "wgpubkey": "key",
                "country": "Albania",
            },
            {
                "hostname": "ovpn.nordvpn.com",
                "vpn": "openvpn",
                "tcp": True,
                "udp": False,
                "country": "Germany",
            },
            {
                "hostname": "no-key.nordvpn.com",
                "vpn": "wireguard",
                "country": "France",
            },
        ]
    }
    assert module.manifest_hosts(payload, "wireguard") == {
        "wg.nordvpn.com": "Albania"
    }
    assert module.manifest_hosts(payload, "openvpn-tcp") == {
        "ovpn.nordvpn.com": "Germany"
    }


def test_candidates_are_filtered_to_the_runtime_manifest(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_verified_spares", lambda **kw: [])
    api = [
        _api_server("valid.nordvpn.com", "Albania", "10.0.0.1", 1),
        _api_server("stale.nordvpn.com", "Albania", "10.0.0.2", 2),
    ]
    embedded = tmp_path / "embedded.json"
    embedded.write_text(
        '{"servers": [{"hostname": "stale.nordvpn.com", '
        '"country": "Albania", "vpn": "wireguard", "wgpubkey": "old"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_fetch_api", lambda: api)
    monkeypatch.setattr(
        module, "_registry_hosts", lambda: ["stale.nordvpn.com"]
    )
    monkeypatch.setattr(module, "_active_hosts", lambda: set())
    monkeypatch.setattr(module, "EMBEDDED", embedded)
    monkeypatch.setattr(module, "_host_quality", lambda: {})

    rows = module.build_candidates(
        "wireguard", {"valid.nordvpn.com": "Albania"}
    )
    assert [row["host"] for row in rows] == ["valid.nordvpn.com"]
    assert rows[0]["country"] == "Albania"


def test_repair_chain_can_reclaim_its_own_current_host(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_verified_spares", lambda **kw: [])

    class _Response:
        def json(self):
            return {
                "stations": [
                    {
                        "station": 1,
                        "vpn": {
                            "status": "connected",
                            "pinned_hostname": "old-pin.nordvpn.com",
                            "current_host": "al75.nordvpn.com",
                        },
                    },
                    {
                        "station": 2,
                        "vpn": {
                            "status": "connected",
                            "current_host": "al37.nordvpn.com",
                        },
                    },
                ]
            }

    monkeypatch.setattr(
        module.requests, "get", lambda *args, **kwargs: _Response()
    )
    # Station 2 may retry the server that gave it a bad NAT egress.
    assert module._active_hosts(2) == {"al75.nordvpn.com"}
    assert module._active_hosts() == {
        "al75.nordvpn.com",
        "al37.nordvpn.com",
    }
    # A stale pin is not a live ownership claim: it must not exclude (and then
    # prune) a host that can still produce a fresh egress.
    assert "old-pin.nordvpn.com" not in module._active_hosts()


def test_verified_spare_roundtrip_and_expiry(tmp_path):
    module = _module()
    db = tmp_path / "ip_registry.db"
    module._record_verified_spare(
        2,
        "al37.nordvpn.com",
        "Albania",
        "87.120.102.22",
        "deepseek-v4.1-flash-free",
        "openvpn-udp",
        db_path=db,
        now=1_000.0,
    )
    rows = module._verified_spares(db, now=1_001.0, transport="openvpn-udp")
    assert len(rows) == 1
    assert rows[0]["hostname"] == "al37.nordvpn.com"
    assert rows[0]["egress_ip"] == "87.120.102.22"
    # Wrong transport is not a spare for this station.
    assert module._verified_spares(db, now=1_001.0, transport="wireguard") == []
    # Expired spares are not returned.
    assert module._verified_spares(db, now=1_000.0 + 7 * 3600) == []


def test_build_candidates_tries_a_verified_spare_first(monkeypatch):
    module = _module()
    api = [
        _api_server("other.nordvpn.com", "Albania", "10.0.0.1", 1),
    ]
    monkeypatch.setattr(module, "_fetch_api", lambda: api)
    monkeypatch.setattr(module, "_registry_hosts", lambda: [])
    monkeypatch.setattr(module, "_active_hosts", lambda: set())
    monkeypatch.setattr(module, "_host_quality", lambda: {})
    monkeypatch.setattr(
        module,
        "_verified_spares",
        lambda **kw: [
            {
                "hostname": "spare.nordvpn.com",
                "country": "Albania",
                "egress_ip": "87.120.102.20",
                "last_200_at": 999.0,
            }
        ],
    )
    embedded = Path("nonexistent-embedded.json")
    monkeypatch.setattr(module, "EMBEDDED", embedded)
    rows = module.build_candidates(
        "wireguard",
        {
            "spare.nordvpn.com": "Albania",
            "other.nordvpn.com": "Albania",
        },
    )
    assert rows[0]["host"] == "spare.nordvpn.com"
    assert rows[0]["source"] == "spare"


def test_previous_host_is_restored_only_when_it_was_alive():
    module = _module()
    snapshot = {
        "host": "al37.nordvpn.com",
        "ip": "87.120.102.22",
        "status": "connected",
    }
    assert module._should_restore_previous(
        snapshot, exhausted=set(), current_host="at160.nordvpn.com"
    ) is True
    # Burned egress: no point restoring it.
    assert module._should_restore_previous(
        snapshot, exhausted={"87.120.102.22"}, current_host="at160.nordvpn.com"
    ) is False
    # Tunnel was already down before the scan: do not restore a dead host.
    snapshot["status"] = "error"
    assert module._should_restore_previous(
        snapshot, exhausted=set(), current_host="at160.nordvpn.com"
    ) is False
    # Already on the previous host.
    snapshot["status"] = "connected"
    assert module._should_restore_previous(
        snapshot, exhausted=set(), current_host="al37.nordvpn.com"
    ) is False


def test_transient_429_does_not_mark_the_ip_ledger():
    module = _module()

    class _Result:
        quota_scope = "transient"

    assert module._should_mark_ledger_429(_Result()) is False
    _Result.quota_scope = "daily"
    assert module._should_mark_ledger_429(_Result()) is True
    _Result.quota_scope = "rate_limit"
    assert module._should_mark_ledger_429(_Result()) is True
    _Result.quota_scope = ""
    assert module._should_mark_ledger_429(_Result()) is True


def test_ledger_exhausted_buckets_only_keep_future_resets():
    module = _module()
    buckets = [
        {"ip": "1.1.1.1", "exhausted": True, "reset_at": 200.0},
        {"ip": "2.2.2.2", "exhausted": True, "reset_at": 50.0},
        {"ip": "3.3.3.3", "exhausted": False, "reset_at": 0.0},
        {"ip": "", "exhausted": True, "reset_at": 200.0},
    ]
    assert module._exhausted_ips_from_buckets(buckets, now=100.0) == {
        "1.1.1.1"
    }
