"""Dedicated qualifier slots: config clamping, reconciliation, snapshot.

The manager is exercised with a fake Docker runner and a fake epoch runner so
no container or tunnel is ever touched.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from free.qualification import qualifiers as module
from free.qualification.qualifiers import (
    QualifierConfig,
    QualifierManager,
    persist_qualifier_request,
    qualifier_config_snapshot,
)
from free.qualification.sweep import SweepConfig


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self, results: list[FakeCompleted] | None = None):
        self.calls: list[list[str]] = []
        self.results = list(results or [])

    def __call__(self, command, cwd):
        self.calls.append(list(command))
        if self.results:
            return self.results.pop(0)
        return FakeCompleted()


def test_config_clamps_count_and_station_range():
    config = QualifierConfig.from_mapping(
        {
            "enabled": True,
            "count": 9,
            "max_count": 4,
            "recommended_max": 2,
            "station_base": 7,
            "transport": "openvpn-tcp",
            "max_hosts_per_epoch": 9000,
            "interval_s": 0,
            "max_attempts_per_host": 0,
            "transport_rounds": 99,
        }
    )
    assert config.count == 4
    assert config.active_count == 4
    assert config.stations == (7, 8, 9, 10)
    assert config.max_hosts_per_epoch == 1000
    assert config.interval_s == 1.0
    assert config.max_attempts_per_host == 1
    assert config.transport_rounds == 5
    assert config.transport == "openvpn-tcp"


def test_config_ignores_unknown_transport_and_bad_station_base():
    config = QualifierConfig.from_mapping(
        {"station_base": 99, "max_count": 99, "transport": "l2tp"}
    )
    assert config.station_base == 10
    assert config.max_count == 1
    assert config.transport == "wireguard"
    assert config.stations == ()


def test_disabled_config_has_no_active_slot():
    config = QualifierConfig.from_mapping({"enabled": False, "count": 3})
    assert config.count == 3
    assert config.active_count == 0
    assert config.stations == ()


def test_snapshot_warns_above_recommended_max():
    snapshot = qualifier_config_snapshot(
        {
            "enabled": True,
            "count": 4,
            "max_count": 4,
            "recommended_max": 2,
            "station_base": 7,
        }
    )
    assert snapshot["manager"] is False
    assert snapshot["count"] == 4
    assert snapshot["desired_count"] == 4
    assert len(snapshot["slots"]) == 4
    assert [slot["station"] for slot in snapshot["slots"]] == [7, 8, 9, 10]
    assert "AUTH_FAILED" in snapshot["warning"]
    assert all(slot["state"] == "stopped" for slot in snapshot["slots"])


def test_snapshot_without_warning_under_recommended_max():
    snapshot = qualifier_config_snapshot(
        {"enabled": True, "count": 2, "recommended_max": 2}
    )
    assert snapshot["warning"] == ""
    assert snapshot["count"] == 2


def test_persist_merges_operator_request(monkeypatch):
    calls: dict = {}

    def fake_get(section, path, default=None):
        calls["get"] = (section, path)
        return {
            "shadow_mode": True,
            "qualifiers": {"enabled": False, "count": 0, "max_count": 4},
        }

    def fake_set(section, path, value):
        calls["set"] = (section, path, value)

    import config.settings as settings

    monkeypatch.setattr(settings, "load_yaml_config", lambda: {}, raising=False)
    monkeypatch.setattr(settings, "yaml_get", fake_get, raising=False)
    monkeypatch.setattr(settings, "yaml_set", fake_set, raising=False)

    snapshot = persist_qualifier_request(enabled=True, count=3)
    assert calls["get"] == ("egress_catalog", "qualification")
    assert calls["set"][0] == "egress_catalog"
    assert calls["set"][1] == "qualification"
    stored = calls["set"][2]
    assert stored["shadow_mode"] is True
    assert stored["qualifiers"]["enabled"] is True
    assert stored["qualifiers"]["count"] == 3
    assert stored["qualifiers"]["max_count"] == 4
    assert snapshot["enabled"] is True
    assert snapshot["count"] == 3


@pytest.mark.asyncio
async def test_apply_starts_and_stops_the_slots():
    runner = FakeRunner()
    epochs: list[tuple[int, QualifierConfig]] = []

    async def epoch_runner(station, config):
        epochs.append((station, config))
        return {
            "epoch_id": f"qualifier-{station}-1",
            "claimed": 5,
            "promoted": 2,
            "errors": [],
        }

    manager = QualifierManager(
        ".",
        config=QualifierConfig(enabled=False, count=0),
        runner=runner,
        epoch_runner=epoch_runner,
        persist=None,
        clock=lambda: 1000.0,
    )
    await manager.start()
    assert manager.snapshot()["count"] == 0

    snapshot = await manager.apply(enabled=True, count=2)
    await asyncio.sleep(0)
    assert snapshot["count"] == 2
    assert snapshot["manager"] is True
    assert [slot["station"] for slot in snapshot["slots"]] == [7, 8]

    ups = [call for call in runner.calls if "up" in call]
    assert len(ups) == 2
    assert "vpn-gluetun-7" in ups[0]
    assert "vpn-gluetun-8" in ups[1]
    assert all("--no-deps" in call for call in ups)
    assert all("multi-station" in call for call in ups)
    assert sorted(station for station, _ in epochs) == [7, 8]

    await asyncio.sleep(0)
    snapshot = manager.snapshot()
    slot = next(item for item in snapshot["slots"] if item["station"] == 7)
    assert slot["last_epoch"].startswith("qualifier-7-")
    assert slot["last_claimed"] == 5
    assert slot["last_promoted"] == 2

    snapshot = await manager.apply(enabled=False, count=0)
    await asyncio.sleep(0)
    assert snapshot["count"] == 0
    stops = [call for call in runner.calls if call[:2] == ["docker", "stop"]]
    assert ["docker", "stop", "opencode-vpn-7"] in stops
    assert ["docker", "stop", "opencode-vpn-8"] in stops
    assert manager.snapshot()["slots"] == []


@pytest.mark.asyncio
async def test_apply_reports_docker_failure_without_crashing():
    runner = FakeRunner(
        [
            FakeCompleted(returncode=1, stderr="service not found"),
            FakeCompleted(),
        ]
    )

    async def epoch_runner(station, config):
        return {"epoch_id": f"qualifier-{station}-1"}

    manager = QualifierManager(
        ".",
        config=QualifierConfig(),
        runner=runner,
        epoch_runner=epoch_runner,
        persist=None,
        clock=lambda: 1000.0,
    )
    snapshot = await manager.apply(enabled=True, count=2)
    await asyncio.sleep(0)
    slots = {slot["station"]: slot for slot in snapshot["slots"]}
    assert slots[7]["state"] == "error"
    assert "service not found" in slots[7]["last_error"]
    assert slots[8]["running"] is True
    assert all("vpn-gluetun-9" not in " ".join(call) for call in runner.calls)
    await manager.stop()


@pytest.mark.asyncio
async def test_apply_persists_the_request():
    persisted: list[dict] = []
    runner = FakeRunner()

    async def epoch_runner(station, config):
        return {"epoch_id": f"qualifier-{station}-1"}

    manager = QualifierManager(
        ".",
        config=QualifierConfig(),
        runner=runner,
        epoch_runner=epoch_runner,
        persist=lambda payload: persisted.append(dict(payload)),
        clock=lambda: 1000.0,
    )
    await manager.apply(enabled=True, count=1)
    assert persisted[-1]["enabled"] is True
    assert persisted[-1]["count"] == 1
    assert persisted[-1]["transport"] == "wireguard"
    await manager.stop()


@pytest.mark.asyncio
async def test_stop_cancels_tasks_and_stops_managed_containers():
    runner = FakeRunner()
    never = asyncio.Event()

    async def epoch_runner(station, config):
        await never.wait()
        return {}

    manager = QualifierManager(
        ".",
        config=QualifierConfig(enabled=True, count=1),
        runner=runner,
        epoch_runner=epoch_runner,
        persist=None,
        clock=lambda: 1000.0,
    )
    await manager.start()
    assert manager.snapshot()["count"] == 1
    await manager.stop()
    assert ["docker", "stop", "opencode-vpn-7"] in runner.calls
    slots = manager.snapshot()["slots"]
    assert [slot["station"] for slot in slots] == [7]
    assert slots[0]["state"] == "stopped"
    assert slots[0]["running"] is False


def test_service_name_and_container_mapping():
    assert QualifierManager.service_name(1) == "vpn-gluetun"
    assert QualifierManager.service_name(7) == "vpn-gluetun-7"
    assert QualifierManager.service_name(10) == "vpn-gluetun-10"


def test_override_written_with_requested_transport(tmp_path):
    manager = QualifierManager(tmp_path, config=QualifierConfig())
    path = manager._write_override(8, "wireguard")
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "vpn-gluetun-8:" in text
    assert "VPN_TYPE=wireguard" in text

    path = manager._write_override(9, "openvpn-udp")
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "VPN_TYPE=openvpn" in text
    assert "OPENVPN_PROTOCOL=udp" in text


def test_default_epoch_builds_a_live_sweep(tmp_path, monkeypatch):
    created: dict = {}

    class FakeSweep:
        def __init__(self, store, adapter, *, config, clock):
            created["store"] = store
            created["adapter"] = adapter
            created["config"] = config
            created["clock"] = clock

        async def run_epoch(self, *, epoch_id, max_hosts, shadow):
            created["epoch_id"] = epoch_id
            created["max_hosts"] = max_hosts
            created["shadow"] = shadow

            class Result:
                def snapshot(self):
                    return {"epoch_id": epoch_id, "claimed": 0, "promoted": 0}

            return Result()

    class FakeStore:
        def __init__(self):
            self.closed_epoch_prefixes: list[str] = []

        def close_running_epochs(self, prefix, **_kwargs):
            self.closed_epoch_prefixes.append(prefix)
            return 0

    class FakeRuntime:
        def __init__(self):
            self.store = FakeStore()
            self.sweep = type(
                "S",
                (),
                {
                    "config": replace(SweepConfig(), station=1),
                    "adapter": "adapter",
                    "_clock": lambda: 500.0,
                },
            )()
            self.closed = False

        def close(self):
            self.closed = True

    runtime = FakeRuntime()
    monkeypatch.setattr(
        "free.qualification.runtime.build_qualification",
        lambda config, adapter: runtime,
        raising=False,
    )
    monkeypatch.setattr(module, "QualificationSweep", FakeSweep)
    monkeypatch.setattr(
        module,
        "build_registry_station_adapter",
        lambda *args, **kwargs: "adapter",
        raising=False,
    )

    manager = QualifierManager(
        tmp_path,
        config=QualifierConfig(enabled=True, count=1, max_hosts_per_epoch=12),
        clock=lambda: 1000.0,
    )
    result = asyncio.run(manager._default_epoch(7, manager.config))
    assert result["epoch_id"].startswith("qualifier-7-")
    assert created["config"].station == 7
    assert created["config"].max_hosts == 12
    assert created["config"].shadow is False
    assert created["config"].transport_order == ("wireguard",)
    assert created["shadow"] is False
    assert created["max_hosts"] == 12
    assert runtime.closed is False
    assert manager._runtimes[7] is runtime

    second = asyncio.run(manager._default_epoch(7, manager.config))
    assert second["epoch_id"].startswith("qualifier-7-")
    assert len(manager._runtimes) == 1
    assert runtime.store.closed_epoch_prefixes == ["qualifier-7-", "qualifier-7-"]

    asyncio.run(manager.stop())
    assert runtime.closed is True
    assert manager._runtimes == {}
