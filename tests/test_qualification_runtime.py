"""Runtime composition tests: config, ingest, discovery, watchdog wiring."""

from __future__ import annotations

import json

from free.qualification.runtime import (
    build_cutover_service,
    build_qualification,
    build_qualification_scheduler,
)


class FakeProber:
    async def preflight_many(self, candidates, transport, **kwargs):
        return []


class FakeStationProbe:
    async def probe(self, station):
        from free.qualification.watchdog import StationObservation

        return StationObservation(
            station=station,
            connected=True,
            dns_ok=True,
            egress_ip="1.2.3.4",
            client_usable=True,
        )


def test_runtime_scheduler_builds_per_station_workers(tmp_path):
    runtime = build_qualification(
        _config(
            tmp_path,
            per_station_workers=True,
            watchdog_stations=[1, 2, 3],
        ),
        clock=lambda: 100.0,
    )
    try:
        assert runtime is not None
        scheduler = build_qualification_scheduler(runtime, clock=lambda: 100.0)
        assert len(scheduler.sweeps) == 3
        assert [item.config.station for item in scheduler.sweeps] == [1, 2, 3]
        assert scheduler._station_state_getter is not None
        assert scheduler.config.service_priority is True
    finally:
        if runtime is not None:
            runtime.close()


def test_cutover_service_reads_timeout_config(tmp_path):
    from free.qualification.store import QualificationStore

    store = QualificationStore(tmp_path / "registry.db")
    try:
        service = build_cutover_service(
            store,
            object(),
            {"egress_catalog": {"qualification": {"cutover_timeout_ms": 2500}}},
        )
        assert service.timeout_s == 2.5
    finally:
        store.close()


def test_runtime_builds_preflight_and_scheduler(tmp_path):
    runtime = build_qualification(
        _config(tmp_path, preflight_timeout_s=7),
        preflight_prober=FakeProber(),
        clock=lambda: 100.0,
    )
    try:
        assert runtime is not None
        assert runtime.preflight is not None
        assert runtime.preflight.timeout_s == 7.0
        scheduler = build_qualification_scheduler(runtime, clock=lambda: 100.0)
        assert scheduler.sweep is runtime.sweep
        assert scheduler.discovery is runtime.discovery
        assert scheduler.watchdog is runtime.watchdog
        assert scheduler.preflight is runtime.preflight
        assert scheduler.config.shadow is True
        assert scheduler.config.run_watchdog is False
    finally:
        if runtime is not None:
            runtime.close()


def test_runtime_scheduler_enables_watchdog_with_probe(tmp_path):
    runtime = build_qualification(
        _config(tmp_path),
        watchdog_probe=FakeStationProbe(),
        clock=lambda: 100.0,
    )
    try:
        assert runtime is not None
        scheduler = build_qualification_scheduler(runtime, clock=lambda: 100.0)
        assert scheduler.config.run_watchdog is True
        assert scheduler.config.run_preflight is False
    finally:
        if runtime is not None:
            runtime.close()


def test_runtime_without_prober_has_no_preflight(tmp_path):
    runtime = build_qualification(_config(tmp_path), clock=lambda: 100.0)
    try:
        assert runtime is not None
        assert runtime.preflight is None
    finally:
        if runtime is not None:
            runtime.close()


def _row(hostname: str):
    return {
        "hostname": hostname,
        "status": "online",
        "load": 10,
        "locations": [{"country": {"name": "Sweden", "city": {"name": "Stockholm"}}}],
        "technologies": [{"identifier": "wireguard_udp"}, {"identifier": "openvpn_udp"}],
    }


def _config(tmp_path, **qualification):
    api = tmp_path / "api.json"
    api.write_text(json.dumps([_row("a.nordvpn.com"), _row("b.nordvpn.com")]), encoding="utf-8")
    cfg = {
        "egress_catalog": {
            "qualification": {
                "enabled": True,
                "api_cache": str(api),
                "shadow_mode": True,
                **qualification,
            }
        },
        "ip_registry": {"registry_db_path": str(tmp_path / "registry.db")},
    }
    return cfg


def test_disabled_runtime_returns_none(tmp_path):
    cfg = _config(tmp_path, enabled=False)
    assert build_qualification(cfg) is None


def test_runtime_builds_ingests_and_summarizes(tmp_path):
    cfg = _config(tmp_path)
    runtime = build_qualification(cfg, clock=lambda: 100.0)
    try:
        assert runtime is not None
        assert runtime.ingest(now=100.0) == 2
        summary = runtime.summary()
        assert summary["total"] == 2
        assert summary["pending"] == 2
        assert runtime.store.last_discovery_run() is None
        assert runtime.watchdog.config.stations == (1, 2, 3, 4, 5, 6)
    finally:
        runtime.close()


def test_transport_order_string_and_watchdog_stations(tmp_path):
    cfg = _config(
        tmp_path,
        transport_order="openvpn-tcp,wireguard",
        watchdog_stations=[1, 3],
        max_repair_attempts=2,
        discovery_interval_s=60,
    )
    runtime = build_qualification(cfg, clock=lambda: 100.0)
    try:
        assert runtime.sweep.config.transport_order == ("openvpn-tcp", "wireguard")
        assert runtime.watchdog.config.stations == (1, 3)
        assert runtime.watchdog.config.max_repair_attempts == 2
        assert runtime.discovery.config.interval_s == 60.0
    finally:
        runtime.close()


def test_invalid_values_fall_back_to_defaults(tmp_path):
    cfg = _config(
        tmp_path,
        infra_backoff_s="not-a-number",
        transport_retry_rounds="x",
        weights={"reliability": "x"},
    )
    runtime = build_qualification(cfg, clock=lambda: 100.0)
    try:
        assert runtime.sweep.config.infra_backoff_s == 300.0
        assert runtime.sweep.config.transport_rounds == 2
        assert runtime.source._weights.reliability == 0.30
    finally:
        runtime.close()


def test_registry_db_path_is_used_when_no_db_path_given(tmp_path):
    db = tmp_path / "registry.db"
    cfg = _config(tmp_path)
    cfg["ip_registry"] = {"registry_db_path": str(db)}
    runtime = build_qualification(cfg, clock=lambda: 100.0)
    try:
        assert runtime.store.path == str(db)
    finally:
        runtime.close()
