"""[plan v10 §4 Lot 3 / §3.6] Filet rotation latence-adaptive — mock pur.

Couvre : décisions IpLatencyTracker (Lot 3), cooldowns soft/hard + escalation,
anti-flapping, global_degraded, rotation_paused, GARANTIE LRU, intégration
pool (_station_usable hard-cool + fallback LRU pick_candidates), et les fixes
audit 14.1.11 (sonde half-open unique), 14.3.6 (backoff off-by-one),
14.1.15 (constante 401), 14.6 stride pays.
"""

from types import SimpleNamespace

import pytest

from ip_latency import IpLatencyTracker
from latency_rotation import COOLDOWN_HARD, COOLDOWN_SOFT, LatencyRotationEngine


class FakeClock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def engine(clock, monkeypatch):
    eng = LatencyRotationEngine()
    monkeypatch.setattr(eng, "_now", clock)
    return eng


def _slow_requests(eng, sid, ip, model="default", n=8, dur=12000):
    for _ in range(n):
        dec = eng.record_request(sid, ip, dur, model, 200)
    return dec


# ── IpLatencyTracker : décisions §3.6.1 ──────────────────────────────────


def test_tracker_min_requests_gate():
    tr = IpLatencyTracker(station=1, ip="1.1.1.1")
    # alternance lent/rapide : consecutive ne monte pas, ewma tirée par lentes
    for d in (20000, 300, 20000, 300):
        tr.record(d, slow_threshold_ms=8000)
    assert tr.request_count == 4
    assert not tr.should_soft_rotate(8000, 9000), "count<5 : ewma seule ne déclenche pas"


def test_tracker_consecutive_slow_fires_before_min():
    # [v6 warm-up] la 1ʳᵉ requête sur IP neuve est exclue du comptage slow →
    # il faut 4 consécutives post-neuve pour atteindre consecutive=3.
    tr = IpLatencyTracker(station=1, ip="1.1.1.1", consecutive_slow_limit=3)
    for _ in range(4):
        tr.record(15000, slow_threshold_ms=8000)
    assert tr.should_soft_rotate(8000, 9000), "3 lentes comptées = signal fort"


def test_tracker_ewma_and_p95_paths():
    tr = IpLatencyTracker(station=1, ip="1.1.1.1")
    # 5 requêtes: 2 très lentes + 3 rapides → ewma tirée par les lentes (log-space lisse)
    for d in (500, 600, 700, 30000, 30000):
        tr.record(d, slow_threshold_ms=8000)
    assert isinstance(tr.p95_ms(), float)


def test_warmup_first_request_not_slow():
    tr = IpLatencyTracker(station=1, ip="2.2.2.2")
    tr.record(20000, slow_threshold_ms=1000)  # handshake payé → ne compte pas
    assert tr.consecutive_slow == 0, "request_count==0 avant record = warm-up v6"


def test_reset_on_finalize():
    tr = IpLatencyTracker(station=1, ip="2.2.2.2")
    tr.record(20000, slow_threshold_ms=1000)
    tr.record(20000, slow_threshold_ms=1000)
    tr.reset_consecutive_slow()
    assert tr.consecutive_slow == 0


# ── moteur : cooldowns / garde-fous ──────────────────────────────────────


def test_engine_soft_then_hard_escalation(engine, clock):
    sid, ip = 1, "3.3.3.3"
    engine.cfg.enabled = True
    for _ in range(6):
        engine.record_request(sid, ip, 20000, "glm", 200)
    assert engine.cooldown_kind(sid, ip) == COOLDOWN_SOFT
    # cooldown soft actif → une NOUVELLE série lente sur la même IP escalade hard
    clock.advance(601)
    for _ in range(6):
        engine.record_request(sid, ip, 25000, "glm", 200)
    kind = engine.cooldown_kind(sid, ip)
    assert kind in (COOLDOWN_HARD,), "récidive post-soft → hard"
    assert engine.ip_hard_cooled(sid, ip)


def test_engine_http_error_never_marks_cool(engine):
    sid, ip = 1, "4.4.4.4"
    for _ in range(10):
        engine.record_request(sid, ip, 30000, "glm", 429)
    assert engine.cooldown_kind(sid, ip) is None, "429 géré par bad_until pool, pas latence"


def test_anti_flapping_cap(engine, clock):
    sid = 1
    for i in range(6):
        engine._note_rotation(sid)
        clock.advance(60)
    ok, why = engine.can_soft_rotate(sid, total_stations=4)
    assert not ok and why == "anti_flapping"
    clock.advance(3601)
    ok, why = engine.can_soft_rotate(sid, total_stations=4)
    assert ok, "fenêtre glissante expirée"


def test_global_degraded_pauses_all(engine, clock):
    # 2 stations sur 4 tournées <10min = 50% ≥ threshold → pause globale
    engine._note_rotation(1)
    engine._note_rotation(2)
    ok, why = engine.can_soft_rotate(3, total_stations=4)
    assert not ok and why == "global_degraded"


def test_paused_toggle(engine):
    engine.paused = True
    ok, why = engine.can_soft_rotate(1, total_stations=4)
    assert not ok and why == "rotation_paused"
    engine.paused = False
    ok, _ = engine.can_soft_rotate(1, total_stations=4)
    assert ok


def test_lru_guarantee_never_none(engine, clock):
    engine.mark(1, "5.5.5.5", COOLDOWN_SOFT)
    clock.advance(100)
    engine.mark(2, "6.6.6.6", COOLDOWN_HARD)
    pick = engine.lru_pick([(1, "5.5.5.5"), (2, "6.6.6.6")])
    assert pick is not None
    assert pick[1] == "5.5.5.5", "soft expire avant hard → LRU choisit le soft"
    fresh = engine.lru_pick([(3, "7.7.7.7"), (1, "5.5.5.5")])
    assert fresh == (3, "7.7.7.7"), "non-refroidi bat toujours un refroidi"


def test_disable_purges_cooldowns_and_soft_history(engine):
    """[GUI toggle « Cooldown latence »] update_config({"enabled": False}) =
    purge immédiate : tous les cooldowns actifs sautent, _soft_history est
    vidée (pas de ré-escalade fantôme à la réactivation), record_request
    court-circuite en "disabled", et la réactivation ne restaure rien."""
    sid, ip = 1, "8.8.8.8"
    engine.mark(sid, ip, COOLDOWN_HARD)
    engine.mark(2, "9.9.9.9", COOLDOWN_SOFT)
    engine._soft_history.update({(sid, ip), (2, "9.9.9.9")})
    assert engine.cooldown_kind(sid, ip) == COOLDOWN_HARD
    assert engine.cooldown_kind(2, "9.9.9.9") == COOLDOWN_SOFT

    engine.update_config({"enabled": False})
    assert engine.cfg.enabled is False
    assert not engine._cooldowns, "cooldowns purgés"
    assert not engine._soft_history, "soft_history purgé (pas d'escalade fantôme)"
    assert engine.cooldown_kind(sid, ip) is None
    assert engine.cooldown_kind(2, "9.9.9.9") is None
    dec = engine.record_request(sid, ip, 30000, "glm", 200)
    assert dec == {"action": "none", "reason": "disabled"}

    # réactivation : rien n'est hérité (cooldowns + historique vides)
    engine.update_config({"enabled": True})
    assert engine.cfg.enabled is True
    assert engine.cooldown_kind(sid, ip) is None
    assert not engine._soft_history

    # séquence no-op : déjà désactivé → pas d'effet, pas de crash
    engine.update_config({"enabled": False})
    assert not engine._cooldowns


def test_per_model_thresholds_hot_reload(engine):
    engine.update_config(
        {
            "enabled": True,
            "slow_threshold_ms_per_model": {"default": 8000, "glm": 4000},
            "min_requests_before_eval": 5,
            "consecutive_slow": 3,
        }
    )
    sid, ip = 9, "8.8.8.8"
    for _ in range(6):
        d1 = engine.record_request(sid, ip, 5500, "glm", 200)  # >4000 glm
        d2 = engine.record_request(sid, ip, 5500, "other", 200)  # <8000 default
        if d1["action"] != "none":
            break
    assert d1.get("action") in ("soft", "hard"), "glm: 5500ms > seuil 4000"
    assert d2["action"] == "none", "other: 5500ms < seuil 8000"


# ── intégration pool : hard-cool écarté, fallback LRU ───────────────────


def _pool_with_stations(monkeypatch, ips_by_sid):
    import free_ip_pool as fp

    pool = object.__new__(fp.FreeIPPool)
    stations = [
        SimpleNamespace(_station=sid, current_ip=ip, status="connected",
                        proxy_mode="vpn", socks5_url=f"socks5://h:{port}",
                        note_free_request=lambda: None,
                        _quota_per_ip=500,
                        current_server={"name": "?"})
        for sid, (ip, port) in ips_by_sid.items()
    ]
    pool._stations = stations
    pool._station_ids = set(ips_by_sid)
    pool._per = {}
    pool._pending = set()
    pool._rotation_tasks = {}
    pool._active_station = None
    pool.latency_engine = None
    pool._free_parallel_enabled = False
    monkeypatch.setattr(fp.FreeIPPool, "_rotation_threshold", lambda self, st: 500)
    return pool, stations


def test_pool_skips_hard_cooled_but_lru_fallback(monkeypatch, engine):
    from latency_rotation import LatencyRotationEngine as Eng

    pool, stations = _pool_with_stations(monkeypatch, {1: ("9.9.9.9", 1080), 2: ("8.8.8.8", 1079)})
    eng = Eng()
    monkeypatch.setattr(eng, "_now", FakeClock())
    eng.mark(1, "9.9.9.9", COOLDOWN_HARD)
    pool.latency_engine = eng

    usable = [st for st in pool._stations if pool._station_usable(st, exclude_approaching=False)]
    assert [st._station for st in usable] == [2], "hard-cooled écartée"

    # TOUTES refroidies → fallback LRU renvoie quand même la moins mauvaise
    eng.mark(2, "8.8.8.8", COOLDOWN_HARD)
    cands = pool.pick_candidates(None)
    assert len(cands) == 1, "garantie LRU : jamais vide"


def test_pool_no_latency_engine_still_works(monkeypatch):
    pool, stations = _pool_with_stations(monkeypatch, {1: ("9.9.9.9", 1080)})
    cands = pool.pick_candidates(None)
    assert [c._station for c in cands] == [1]


# ── fixes audit ──────────────────────────────────────────────────────────


def test_backoff_off_by_one_fixed():
    from vpn_manager import BackoffTimer

    bt = BackoffTimer(base_delay=5.0, max_delay=60.0, multiplier=2.0)
    bt.record_failure()
    assert bt.delay == 5.0, "1er échec = base (§14.3.6)"
    bt.record_failure()
    assert bt.delay == 10.0
    bt.record_failure()
    assert bt.delay == 20.0


def test_breaker_single_probe_half_open():
    from vpn_manager import CircuitBreaker

    cb = CircuitBreaker(failure_threshold=2, recovery_time=10.0)
    cb.record_failure("srv")
    cb.record_failure("srv")
    assert cb.is_available("srv") is False  # open, recovery pas écoulée

    # simule recovery écoulée
    cb._servers["srv"]["opened_at"] -= 11.0
    assert cb.is_available("srv") is True, "1re sonde passe (half-open)"
    assert cb.is_available("srv") is False, "§14.1.11 : 2e appelant bloqué pendant la sonde"
    cb.record_success("srv")
    assert cb.is_available("srv") is True, "succès sonde → closed"


def test_key_pause_401_constant_shared():
    import opencode as oc

    assert oc.KEY_PAUSE_401_SEC == 3600.0


# ── v6 : offset stride pays ──────────────────────────────────────────────


def test_country_offset_stride_changes_draw():
    from shared_rotation import SharedRotationState

    cfg_base = {"recent_ip_window": 20, "shared_rotation_file": "logs/_sr_stride_test.json"}

    sr_legacy = SharedRotationState({**cfg_base, "country_offset_stride": 0})
    sr_strided = SharedRotationState({**cfg_base, "country_offset_stride": 17})

    idx_legacy = sr_legacy.next_country(station=2, offset=14, n=29)
    idx_strided = sr_strided.next_country(station=2, offset=14, n=29)
    # legacy : (cursor+1) + 14*(2-1) ; strided : (cursor+1) + (14+17*1)
    assert idx_legacy != idx_strided, "stride change structurellement le tirage st2"

    # station 1 inchangée par le stride (facteur ×0)
    sr_a = SharedRotationState({**cfg_base, "country_offset_stride": 0})
    sr_b = SharedRotationState({**cfg_base, "country_offset_stride": 17})
    assert sr_a.next_country(1, 14, 29) == sr_b.next_country(1, 14, 29)

    import os

    for f in ("logs/_sr_stride_test.json",):
        if os.path.exists(f):
            os.remove(f)
