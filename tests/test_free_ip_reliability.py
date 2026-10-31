"""Contrats P0 fiabilité : santé (modèle, IP), seuils et concurrence.

On teste le pool sans Docker/réseau : les objets station sont des doubles
minimaux et ``_yaml_get`` est injecté. Aucun modèle n'est substitué.
"""

from __future__ import annotations

import threading

import free.pool as fp


class _Station:
    def __init__(self, sid: int = 1, ip: str = "10.0.0.1"):
        self._station = sid
        self.current_ip = ip
        self.pid = None
        self._quota_per_ip = 500
        self.socks5_url = f"socks5://127.0.0.1:{1080 + sid}"


def _pool(monkeypatch, *, profile=None, inflight=2, slot_wait=0.05):
    values = {
        "quota_per_ip_by_model": profile or {},
        "bad_ip_ttl_s": 1800,
        "quota_ip_block_s": 120,
        "model_global_429_window_s": 60,
        "model_global_429_min_ips": 3,
        "model_global_cooldown_s": 60,
        "max_inflight_per_model_ip": inflight,
        "model_slot_wait_s": slot_wait,
    }

    def fake_yaml(section, key, default=None):
        if section == "ip_rotation":
            return values.get(key, default)
        return default

    monkeypatch.setattr(fp, "_yaml_get", fake_yaml)
    pool = object.__new__(fp.FreeIPPool)
    pool._model_ip_health = {}
    pool._model_429_events = {}
    pool._model_global_until = {}
    pool._model_ip_lock = threading.Lock()
    pool._model_slots = {}
    pool._quality_preselection = {"enabled": False}
    pool.quality = None
    pool._rotation_stagger = 10
    return pool


def test_bad_ip_without_success_is_quarantined(monkeypatch):
    pool = _pool(monkeypatch)
    st = _Station()
    pool.note_model_ip("deepseek-v4.1-flash-free", st, 429)
    assert pool.model_ip_bad("deepseek-v4.1-flash-free", st) is True
    assert pool.model_ip_block_remaining("deepseek-v4.1-flash-free", st) > 0
    # Un succès sur la même IP lève la quarantaine (early recovery).
    pool.note_model_ip("deepseek-v4.1-flash-free", st, 200)
    assert pool.model_ip_bad("deepseek-v4.1-flash-free", st) is False


def test_new_ip_gets_a_fresh_bucket(monkeypatch):
    pool = _pool(monkeypatch)
    st = _Station(ip="10.0.0.1")
    pool.note_model_ip("m", st, 429)
    assert pool.model_ip_bad("m", st) is True
    st.current_ip = "10.0.0.2"
    assert pool.model_ip_bad("m", st) is False


def test_three_distinct_ips_open_model_global_backoff(monkeypatch):
    pool = _pool(monkeypatch)
    for i in range(3):
        pool.note_model_ip("m", _Station(sid=i + 1, ip=f"10.0.0.{i + 1}"), 429)
    assert pool.model_global_remaining("m") > 0
    # Un succès prouve que le modèle répond : le backoff global tombe.
    pool.note_model_ip("m", _Station(sid=4, ip="10.0.0.4"), 200)
    assert pool.model_global_remaining("m") == 0


def test_rotation_threshold_uses_model_profile(monkeypatch):
    pool = _pool(monkeypatch, profile={"deepseek-v4.1-flash-free": 150})
    st = _Station(sid=1)
    # 150 - 10 - 0 = 140 ; sans profil, le fallback reste 500 - 10 = 490.
    assert pool._rotation_threshold(st, "deepseek-v4.1-flash-free") == 140
    assert pool._rotation_threshold(st) == 490


async def test_model_slot_limits_concurrency_and_releases(monkeypatch):
    pool = _pool(monkeypatch, inflight=1, slot_wait=0.05)
    st = _Station()
    first = await pool.acquire_model_slot("m", st)
    assert first is not None
    second = await pool.acquire_model_slot("m", st)
    assert second is None
    pool.release_model_slot(first)
    third = await pool.acquire_model_slot("m", st)
    assert third is not None
    pool.release_model_slot(third)
