"""Production cutover ports: bind, probe, rollback with injected doubles."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from free.qualification.apply import station_snapshot
from free.qualification.cutover import CutoverTarget
from free.qualification.cutover_adapter import AdapterCutoverPorts
from free.qualification.store import QualificationStore


class _Binder:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def assign(self, station, hostname, country=None, **kwargs):
        self.calls.append((station, hostname, kwargs))
        return SimpleNamespace(ok=self.ok, hostname=hostname, egress_ip="1.2.3.4")


class _Prober:
    def __init__(self, status=200, valid=True):
        self.status = status
        self.valid = valid
        self.calls = []

    async def probe(self, station, model):
        self.calls.append((station, model))
        return SimpleNamespace(status=self.status, valid=self.valid)


def _ports(binder=None, prober=None):
    adapter = SimpleNamespace(
        binder=binder or _Binder(),
        prober=prober or _Prober(),
        model="deepseek-v4.1-flash-free",
    )
    return AdapterCutoverPorts(adapter, station=2), adapter.binder, adapter.prober


@pytest.mark.asyncio
async def test_adapter_cutover_switches_probes_and_rolls_back():
    ports, binder, prober = _ports()
    target = CutoverTarget(
        station=2,
        to_option_key="se1.nordvpn.com",
        to_egress_ip="1.2.3.4",
        to_transport="openvpn-tcp",
        from_option_key="old.nordvpn.com",
        from_egress_ip="5.6.7.8",
        from_transport="wireguard",
    )
    assert await ports.prepare(target) is True
    assert await ports.switch(target) is True
    assert await ports.probe(target) is True
    assert await ports.rollback(target) is True
    assert binder.calls == [
        (
            2,
            "se1.nordvpn.com",
            {"vpn_type": "openvpn", "openvpn_protocol": "tcp"},
        ),
        (
            2,
            "old.nordvpn.com",
            {"vpn_type": "wireguard", "openvpn_protocol": None},
        ),
    ]
    assert prober.calls == [(2, "deepseek-v4.1-flash-free")]


@pytest.mark.asyncio
async def test_adapter_cutover_rejects_empty_target_and_bad_probe():
    ports, binder, prober = _ports(prober=_Prober(status=429, valid=False))
    assert await ports.prepare(CutoverTarget(station=2)) is False
    assert await ports.rollback(CutoverTarget(station=2)) is True
    assert await ports.probe(CutoverTarget(station=2)) is False
    assert binder.calls == []


def test_station_snapshot_reads_hostname_and_quota(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_station_state(
            2,
            state="serving",
            current_ip="1.2.3.4",
            current_host="se1.nordvpn.com",
            client_usable=True,
            now=100.0,
        )
        snapshot = station_snapshot(
            store,
            2,
            {"quota_remaining": 10, "quota_estimate": 200},
            standby_size=1,
        )
        assert snapshot.current_ip == "1.2.3.4"
        assert snapshot.current_hostname == "se1.nordvpn.com"
        assert snapshot.quota_remaining == 10
        assert snapshot.quota_estimate == 200
    finally:
        store.close()


@pytest.mark.asyncio
async def test_apply_rotation_commits_through_real_cutover(tmp_path, monkeypatch):
    from free.qualification import apply as apply_module
    from free.qualification.domain import Candidate

    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="new.nordvpn.com",
                    country="Sweden",
                    transports=("openvpn-tcp",),
                    potential_score=90.0,
                    first_seen_at=100.0,
                    last_seen_at=100.0,
                    updated_at=100.0,
                )
            ],
            now=100.0,
        )
        store.upsert_station_state(
            1,
            state="serving",
            current_ip="1.1.1.1",
            current_host="old.nordvpn.com",
            client_usable=True,
            now=100.0,
        )
    finally:
        store.close()

    adapter = SimpleNamespace(
        binder=_Binder(),
        prober=_Prober(),
        model="deepseek-v4.1-flash-free",
    )
    monkeypatch.setattr(
        apply_module,
        "build_registry_station_adapter",
        lambda *args, **kwargs: adapter,
    )
    result = await apply_module.apply_rotation(
        root=tmp_path,
        db_path=db,
        config={},
        station=1,
        body={"quota_remaining": 0, "quota_estimate": 200},
        usable_stations=6,
    )
    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["decision"]["action"] == "rotate"
    assert result["decision"]["candidate"] == "new.nordvpn.com"
    assert result["cutover"]["status"] == "committed"
    assert adapter.binder.calls[0][1] == "new.nordvpn.com"
