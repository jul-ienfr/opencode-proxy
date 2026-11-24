"""Tests — 024 Phase 1.5 « disponibilité free (429) » (T314/T316/T319/T320/T321).

Preuve du besoin : ``docs/evidence/free-model-429-2026-11-23.md`` — le modèle
free ``deepseek-v4.1-flash-free`` répond 200 via WARP ; les 429 NordVPN sont un
quota **par IP** ; 3 stations ont un tunnel HS.

Ces tests verrouillent les invariants du lot de correction :
un 429 quota marque l'IP ``spent`` (inadmissible jusqu'au ``reset_at``),
``client_usable`` exige une preuve 200 (jamais le seul état du tunnel), la
disponibilité est exposée par egress, et un modèle reste ``alive`` dès qu'une
IP répond — jamais parce qu'un 429 a été vu.
"""

from __future__ import annotations

import time
import types

import pytest

from free import pool as pool_mod
from free import wire_admission as wa
from free.egress_availability import (
    ALIVE,
    QUOTA,
    TRANSPORT,
    egress_verdict,
    model_alive_from_egress,
    reset_rows,
    state_rows,
)
from free.ip_state import SPENT, IPState, IPStateStore
from free.pool import FreeIPPool
from free.qualification.station_probe import HttpStationProbe
from observability.metrics import MetricsSnapshot, build_metrics_text


def _store(tmp_path) -> IPStateStore:
    return IPStateStore(tmp_path / "registry.db")


# -- T314 : verdict par egress ----------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, ALIVE),
        (204, ALIVE),
        (429, QUOTA),
        (500, TRANSPORT),
        (0, TRANSPORT),
        (None, TRANSPORT),
    ],
)
def test_egress_verdict(status, expected):
    assert egress_verdict(status) == expected


def test_model_alive_only_on_a_200():
    assert model_alive_from_egress([QUOTA, TRANSPORT]) is False
    assert model_alive_from_egress([QUOTA, ALIVE]) is True


def test_probe_verdict_never_alive_on_a_429():
    """[T314] Le verdict modèle suit la même règle : 2xx → alive, 429 → limited."""
    from config.discovery import free_probe_verdict

    assert free_probe_verdict(200, "", {}) == ("alive", "2xx")
    verdict, _reason = free_probe_verdict(429, "", {})
    assert verdict == "limited"
    assert verdict != "alive"
    assert free_probe_verdict(503, "", {})[0] == "unknown"


# -- T316 : 429 quota -> spent ----------------------------------------------


def test_note_quota_marks_spent(tmp_path):
    store = _store(tmp_path)
    store.transition("nordvpn", "1.2.3.4", "proven", wire_fp="wf", now=1000.0)
    st = store.note_quota("nordvpn", "1.2.3.4", reset_at=2000.0, now=1000.0)
    assert st is not None
    assert st.state == SPENT
    assert st.reset_at == 2000.0


def test_spent_ip_is_refused_by_admission(tmp_path):
    db = tmp_path / "registry.db"
    IPStateStore(db).note_quota("nordvpn", "9.9.9.9", reset_at=10_000.0, now=1000.0)
    adm = wa.admit_ip("wf", "9.9.9.9", mode="convert", db_path=db)
    assert adm.allowed is False
    assert adm.action == "refuse"


def test_note_quota_never_shortens_a_running_punishment(tmp_path):
    store = _store(tmp_path)
    store.note_quota("warp", "5.5.5.5", reset_at=5000.0, now=1000.0)
    st = store.note_quota("warp", "5.5.5.5", reset_at=2000.0, now=1500.0)
    assert st.reset_at == 5000.0


def test_note_quota_revives_after_reset(tmp_path):
    store = _store(tmp_path)
    store.note_quota("warp", "6.6.6.6", reset_at=1000.0, now=500.0)
    st = store.note_quota("warp", "6.6.6.6", reset_at=9000.0, now=3000.0)
    assert st.state == SPENT
    assert st.reset_at == 9000.0


# -- T316 : câblage pool (note_model_ip -> spent) ---------------------------


class _StubPool:
    """Exerce le vrai ``_note_ip_state_quota`` sans construire un pool complet."""

    _ipstate_store = FreeIPPool._ipstate_store
    _note_ip_state_quota = FreeIPPool._note_ip_state_quota

    def __init__(self, store):
        self._ipstate_store_override = store

    def _provider_for_station(self, station):  # noqa: ARG002
        return "nordvpn"


def test_pool_quota_429_marks_spent(tmp_path):
    store = _store(tmp_path)
    pool = _StubPool(store)
    station = types.SimpleNamespace(current_ip="7.7.7.7")
    FreeIPPool._note_ip_state_quota(
        pool, "m", station, None, None, "FreeUsageLimitError", 3600
    )
    st = store.get("nordvpn", "7.7.7.7")
    assert st is not None
    assert st.state == SPENT


def test_pool_transient_429_does_not_mark(tmp_path):
    store = _store(tmp_path)
    pool = _StubPool(store)
    station = types.SimpleNamespace(current_ip="8.8.8.8")
    FreeIPPool._note_ip_state_quota(
        pool, "m", station, None, "too many requests", "RateLimitError", 30
    )
    assert store.get("nordvpn", "8.8.8.8") is None


# -- T319 : client_usable exige une preuve 200 ------------------------------


def test_client_usable_requires_a_200_proof():
    obs = HttpStationProbe()._observe(1, {"status": "connected", "ip": "1.2.3.4"})
    assert obs.connected is True
    assert obs.client_usable is False


def test_client_usable_true_with_a_proof():
    obs = HttpStationProbe()._observe(
        1, {"status": "connected", "ip": "1.2.3.4", "model_usable": True}
    )
    assert obs.client_usable is True


# -- T320 : observabilité par egress ----------------------------------------


def test_metrics_egress_section_emits_series():
    snap = MetricsSnapshot(
        egress_states=[('provider="nordvpn",egress="1.2.3.4",state="spent"', 1)],
        egress_resets=[('provider="nordvpn",egress="1.2.3.4"', 2000.0)],
    )
    text = build_metrics_text(snap)
    assert 'egress_ip_state{provider="nordvpn",egress="1.2.3.4",state="spent"} 1' in text
    assert 'egress_reset_at_seconds{provider="nordvpn",egress="1.2.3.4"} 2000.0' in text


def test_metrics_egress_section_empty_is_silent():
    assert "egress_ip_state" not in build_metrics_text(MetricsSnapshot())


def test_state_and_reset_rows():
    rows = state_rows([IPState(provider="warp", ip="2.2.2.2", state="proven")])
    assert rows == [('provider="warp",egress="2.2.2.2",state="proven"', 1)]
    resets = reset_rows(
        [IPState(provider="warp", ip="2.2.2.2", state="spent", reset_at=123.0)]
    )
    assert resets == [('provider="warp",egress="2.2.2.2"', 123.0)]
    assert state_rows([IPState(provider="", ip="x", state="proven")]) == []
    assert reset_rows([IPState(provider="warp", ip="2.2.2.2", state="proven")]) == []


# -- T317 : une IP spent est exclue de la sélection --------------------------


class _Station:
    """Double minimal duck-typé par ``FreeIPPool``."""

    def __init__(self, sid: int, ip: str) -> None:
        self._station = sid
        self.current_ip = ip
        self.status = "connected"
        self.enabled = True
        self.proxy_mode = "vpn"

    def get_status(self) -> dict:
        return {"station": self._station, "status": self.status}

    def arm_egress_watchdog(self) -> None:
        pass


def _pool_with(ip: str, store) -> tuple[FreeIPPool, _Station]:
    station = _Station(1, ip)
    pool = FreeIPPool(station)
    pool._ipstate_store_override = store
    return pool, station


def test_spent_egress_ip_is_excluded_from_selection(tmp_path):
    store = _store(tmp_path)
    pool, station = _pool_with("203.0.113.9", store)
    assert pool._station_usable(station, exclude_approaching=False) is True
    store.note_quota("nordvpn", "203.0.113.9", reset_at=time.time() + 3600)
    pool._spent_egress_cache = None
    assert pool._station_usable(station, exclude_approaching=False) is False


def test_spent_egress_cache_expires_at_reset(tmp_path):
    store = _store(tmp_path)
    pool, _station = _pool_with("203.0.113.10", store)
    store.note_quota("nordvpn", "203.0.113.10", reset_at=1000.0, now=500.0)
    pool._spent_egress_cache = None
    assert pool._spent_egress_ips(now=600.0) == {"203.0.113.10"}
    pool._spent_egress_cache = None
    assert pool._spent_egress_ips(now=2000.0) == set()


def test_no_store_means_no_spent_exclusion(tmp_path):
    station = _Station(1, "203.0.113.11")
    pool = FreeIPPool(station)  # aucune injection : sous pytest, pas de store
    assert pool._spent_egress_ips() == set()
    assert pool._station_usable(station, exclude_approaching=False) is True


def test_real_200_clears_a_stale_spent_gate(tmp_path):
    """[Option 2] Une preuve 200 réelle prime sur un `spent` antérieur : la
    barrière dure T317 ne doit pas exclure une IP qui vient de répondre 200."""
    store = _store(tmp_path)
    pool, station = _pool_with("203.0.113.12", store)
    store.note_quota("nordvpn", "203.0.113.12", reset_at=time.time() + 3600)
    pool._spent_egress_cache = None
    assert pool._spent_egress_ips() == {"203.0.113.12"}
    assert pool._station_usable(station, exclude_approaching=False) is False

    pool.note_model_ip("deepseek-v4.1-flash-free", station, 200)

    assert store.get("nordvpn", "203.0.113.12").state == "unproven"
    pool._spent_egress_cache = None
    assert pool._spent_egress_ips() == set()
    assert pool._station_usable(station, exclude_approaching=False) is True


# -- T318 : plancher de réserve prouvée --------------------------------------


class _EmptyStore:
    def list(self):
        return []


def test_reserve_health_reports_the_floor(monkeypatch):
    monkeypatch.setattr(pool_mod, "_PROVEN_RESERVE_CACHE", None)
    monkeypatch.setattr("free.ip_state.store", lambda *a, **k: _EmptyStore())
    monkeypatch.setattr(
        "free.client_reserve.reserved_ips", lambda **_: {"1.1.1.1", "2.2.2.2"}
    )
    monkeypatch.setattr(
        "free.reserve_settings.get_int",
        lambda name, default=0: {"min": 3, "target": 6}.get(name, default),
    )
    assert pool_mod.reserve_health() == {
        "count": 2,
        "min": 3,
        "target": 6,
        "below_min": True,
    }


# -- T321 : la chaîne réelle écarte NordVPN `spent` et choisit WARP ----------


def test_provider_chain_switches_to_warp_when_nordvpn_is_spent(tmp_path, monkeypatch):
    from free import pool as pool_mod

    store = _store(tmp_path)
    nv = _Station(1, "203.0.113.50")
    warp = pool_mod.Socks5Endpoint(
        host="127.0.0.1", port=11080, egress_ip="104.28.9.9", managed=True, index=5
    )

    pool = object.__new__(pool_mod.FreeIPPool)
    pool._stations = [nv]
    pool._per = {}
    pool._station_health = pool_mod.StationHealthRegistry()
    pool._strict_health = False
    pool._quota_enabled = False
    pool._spare_station = 0
    pool._draining_stations = set()
    pool._egress_providers = ["nordvpn", "warp"]
    pool._egress_strategy = "failover"
    pool._ipstate_store_override = store
    monkeypatch.setattr(pool, "_socks5_enabled_eps", lambda: [warp])
    monkeypatch.setattr(
        pool, "_socks5_usable", lambda ep, exclude_approaching=False: True
    )
    monkeypatch.setattr(pool, "_provider_matches", lambda ep, name: name == "warp")

    pool._spent_egress_cache = None
    assert pool._available_egress_providers() == ["nordvpn", "warp"]

    store.note_quota("nordvpn", "203.0.113.50", reset_at=time.time() + 3600)
    pool._spent_egress_cache = None
    assert pool._available_egress_providers() == ["warp"]
    assert pool._select_egress_provider(["warp"]) == "warp"


def test_client_429_reason_uses_pool_socks5_usable(monkeypatch):
    """Le motif 429 interroge `_socks5_client_usable` (l'attribut
    `client_usable` n'existe pas sur `Socks5Endpoint`)."""
    import opencode as oc
    import shared_state

    ep = types.SimpleNamespace()  # aucun attribut client_usable
    state = {"ok": True}
    pool = types.SimpleNamespace(
        _socks5_eps=[ep],
        _socks5_client_usable=lambda e: state["ok"],
    )
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)
    monkeypatch.setattr("free.client_reserve.reserved_ips", lambda **_: set())
    assert oc._client_429_reason() == "reserve_empty"
    state["ok"] = False
    assert oc._client_429_reason() == "all_fallbacks_dead"


# -- Qualification coverage : une sonde WARP réelle vaut preuve ledger -------


def _warp_qualification_pool(ip: str):
    from free.quota_ledger import QuotaLedger

    pool = object.__new__(pool_mod.FreeIPPool)
    pool._per = {}
    pool._ledger = QuotaLedger(persist=False)
    pool._ledger_enabled = True
    pool._model_ip_health = {}
    pool._egress_outcome_log = None
    warp = pool_mod.Socks5Endpoint(
        host="127.0.0.1",
        port=11080,
        egress_ip=ip,
        managed=True,
        provider="warp",
        container="warp-container-1",
        index=5,
    )
    pool._socks5_eps = [warp]
    return pool, warp


def _warp_option():
    return types.SimpleNamespace(station="warp-container-1", egress_ip="104.28.9.9")


def test_warp_unproven_is_not_a_free_bucket_for_the_model():
    from free.egress_probes import DEFAULT_PROBE_MODEL

    pool, warp = _warp_qualification_pool("104.28.9.9")
    _ = warp
    assert pool.model_bucket_usable(DEFAULT_PROBE_MODEL, warp) is False
    assert pool._provider_has_free_bucket("warp", None, DEFAULT_PROBE_MODEL) is False


def test_catalog_warp_target_picks_the_matching_family(monkeypatch):
    """Un conteneur WARP a deux entrées (v4/v6) : l'option doit résoudre la
    sienne par egress exact, sinon l'IPv6 est sondée via l'egress IPv4."""
    import opencode as oc
    import shared_state
    from free.pool import Socks5Endpoint

    v4 = Socks5Endpoint(
        host="127.0.0.1", port=11080, egress_ip="104.28.230.202",
        managed=True, provider="warp", container="warp-1", index=1, label="warp-1:v4",
    )
    v6 = Socks5Endpoint(
        host="127.0.0.1", port=11080, egress_ip="2a09:bac5:32d2:14e6::215:b5",
        managed=True, provider="warp", container="warp-1", ipv6=True, index=2,
        label="warp-1:v6",
    )
    monkeypatch.setattr(
        shared_state, "free_ip_pool",
        types.SimpleNamespace(_socks5_eps=[v4, v6]), raising=False,
    )
    _p, ep6 = oc._catalog_warp_target(types.SimpleNamespace(station="warp-1", egress_ip=v6.egress_ip))
    assert ep6 is v6
    _p2, ep4 = oc._catalog_warp_target(types.SimpleNamespace(station="warp-1", egress_ip=v4.egress_ip))
    assert ep4 is v4


def test_catalog_probe_200_seeds_a_real_ledger_proof(monkeypatch):
    """Principe VII respecté : c'est le 200 réel de la sonde qui prouve l'IP."""
    import asyncio

    import shared_state
    from free.egress_probes import DEFAULT_PROBE_MODEL
    from free.egress_validation import ProbeOutcome

    pool, warp = _warp_qualification_pool("104.28.9.9")
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)

    async def _probe(url, **kwargs):
        assert url == warp.socks5_url
        assert kwargs["egress_ip"] == "104.28.9.9"
        assert kwargs["model"] == DEFAULT_PROBE_MODEL
        return ProbeOutcome(status=200, body='{"ok":true}')

    monkeypatch.setattr("free.egress_probes.probe_proxy_free_gate", _probe)
    import opencode as oc

    outcome = asyncio.run(oc._catalog_warp_probe(_warp_option()))
    assert outcome is not None and outcome.status == 200
    assert pool.model_bucket_usable(DEFAULT_PROBE_MODEL, warp) is True
    assert pool._provider_has_free_bucket("warp", None, DEFAULT_PROBE_MODEL) is True


def test_catalog_probe_failure_never_invents_a_proof(monkeypatch):
    import asyncio

    import shared_state
    from free.egress_probes import DEFAULT_PROBE_MODEL
    from free.egress_validation import ProbeOutcome

    pool, warp = _warp_qualification_pool("104.28.9.10")
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)

    async def _probe(url, **kwargs):
        return ProbeOutcome(status=403, error="denied")

    monkeypatch.setattr("free.egress_probes.probe_proxy_free_gate", _probe)
    import opencode as oc

    outcome = asyncio.run(oc._catalog_warp_probe(_warp_option()))
    assert outcome is not None and outcome.status == 403
    assert pool.model_bucket_usable(DEFAULT_PROBE_MODEL, warp) is False


def test_catalog_nordvpn_probe_seeds_a_real_proof(monkeypatch):
    """Symétrie WARP/NordVPN : une sonde catalogue 200 sur un tunnel managé
    devient une preuve ledger, jamais inventée."""
    import asyncio

    import shared_state
    from free.egress_probes import DEFAULT_PROBE_MODEL
    from free.egress_validation import ProbeOutcome
    from free.quota_ledger import QuotaLedger

    pool = object.__new__(pool_mod.FreeIPPool)
    pool._per = {}
    pool._ledger = QuotaLedger(persist=False)
    pool._ledger_enabled = True
    manager = types.SimpleNamespace(
        current_ip="203.0.113.99",
        socks5_url="socks5://127.0.0.1:1080",
        provider="nordvpn",
        _station=1,
    )
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)
    monkeypatch.setattr(shared_state, "vpn_managers", [manager], raising=False)

    async def _probe(url, **kwargs):
        assert url == manager.socks5_url
        assert kwargs["egress_ip"] == "203.0.113.99"
        return ProbeOutcome(status=200, body='{"ok":true}')

    monkeypatch.setattr("free.egress_probes.probe_proxy_free_gate", _probe)
    import opencode as oc

    outcome = asyncio.run(
        oc._catalog_nordvpn_probe(
            types.SimpleNamespace(station="station-1", egress_ip="203.0.113.99")
        )
    )
    assert outcome is not None and outcome.status == 200
    assert pool.model_bucket_usable(DEFAULT_PROBE_MODEL, manager) is True


