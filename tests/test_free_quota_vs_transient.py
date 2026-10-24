"""[fix faux-429 + rotation-storm] Un 429 amont « Endpoint is unavailable » (surcharge
serveur transitoire) ne doit NI affirmer « Free quota exhausted », NI déclencher
une rotation d'IP (l'IP neuve serait rejetée pareil — rafale → throttling compte
NordVPN → auto-destruction).

Couverture :

1. ``core.errors.is_free_quota_failure`` :
   - corps « Endpoint is unavailable » → False (transitoire) ;
   - corps quota explicite (rate limit / usage_not_included / insufficient_quota)
     → True (vrai quota, rotation utile) ;
   - statut non-429 / corps vide / statut invalide → False.
2. ``core.errors.free_refusal_response`` :
   - FreeRefusal(429, corps indisponible) → message véridique
     « Free model request failed with status 429: ... » (type overloaded_error),
     PAS « Free quota exhausted » ;
   - FreeRefusal(429, corps quota) → message historique « Free quota exhausted ».
3. ``FreeIPPool.on_quota_exhausted(quota=False)`` : pas de rotation lancée
   (guard ``action in ("rotate","both") and quota``), pas de bad-mark côté socks5,
   mais le compteur corrélé ``_note_upstream_429`` reste alimenté (storm-guard
   partagé intact).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.errors import (  # noqa: E402
    FreeRefusal,
    free_refusal_response,
    is_free_quota_failure,
)

UNAVAILABLE = '{"error":{"type":"server_error","message":"Endpoint is unavailable. Please try again later."}}'
QUOTA_RATE_LIMIT = '{"error":{"message":"Rate limit exceeded for free tier"}}'
QUOTA_USAGE = '{"error":{"message":"usage_not_included: your plan does not include this usage"}}'
QUOTA_INSUFFICIENT = '{"error":{"message":"insufficient_quota: you exceeded your quota"}}'


# ── 1. is_free_quota_failure ─────────────────────────────────────────────


def test_unavailable_is_transient_not_quota():
    assert is_free_quota_failure(429, UNAVAILABLE) is False


def test_unavailable_motif_seul_is_transient():
    assert is_free_quota_failure(429, "Endpoint is unavailable") is False


def test_rate_limit_body_is_quota():
    assert is_free_quota_failure(429, QUOTA_RATE_LIMIT) is True


def test_usage_not_included_is_quota():
    assert is_free_quota_failure(429, QUOTA_USAGE) is True


def test_insufficient_quota_is_quota():
    assert is_free_quota_failure(429, QUOTA_INSUFFICIENT) is True


def test_non_429_never_quota():
    assert is_free_quota_failure(503, QUOTA_RATE_LIMIT) is False
    assert is_free_quota_failure(503, UNAVAILABLE) is False


def test_empty_or_missing_body_never_quota():
    assert is_free_quota_failure(429, "") is False
    assert is_free_quota_failure(429, None) is False
    assert is_free_quota_failure(429, {"error": "x"}) is False


def test_invalid_status_never_quota():
    assert is_free_quota_failure("nan", QUOTA_RATE_LIMIT) is False
    assert is_free_quota_failure(None, QUOTA_RATE_LIMIT) is False


# ── 2. free_refusal_response : message véridique ─────────────────────────


def _noop_redact(body, _n):
    return (body or "")[:300]


def test_transient_429_does_not_claim_quota_exhausted():
    resp = free_refusal_response(
        FreeRefusal(status=429, body=UNAVAILABLE),
        "anthropic",
        redact_fn=_noop_redact,
    )
    assert resp.status_code == 429
    import json

    payload = json.loads(bytes(resp.body).decode())
    msg = payload["error"]["message"]
    assert "Free quota exhausted" not in msg
    assert "Endpoint is unavailable" in msg


def test_transient_429_type_is_overloaded_not_rate_limit():
    resp = free_refusal_response(
        FreeRefusal(status=429, body=UNAVAILABLE),
        "anthropic",
        redact_fn=_noop_redact,
    )
    import json

    payload = json.loads(bytes(resp.body).decode())
    assert payload["error"]["type"] == "overloaded_error"


def test_real_quota_429_keeps_historic_message():
    resp = free_refusal_response(
        FreeRefusal(status=429, body=QUOTA_RATE_LIMIT, retry_after="45"),
        "anthropic",
        redact_fn=_noop_redact,
    )
    assert resp.status_code == 429
    import json

    payload = json.loads(bytes(resp.body).decode())
    assert payload["error"]["message"] == "Free quota exhausted on all VPN stations. Retry after 45s."
    assert payload["error"]["type"] == "rate_limit_error"
    assert resp.headers["Retry-After"] == "45"


# ── 3. on_quota_exhausted(quota=False) : pas de rotation ─────────────────


def test_quota_false_skips_rotation_but_keeps_correlated_counter(monkeypatch):
    import free.pool as pool_mod

    noted = []
    launched = []

    class _FakeVPN:
        enabled = True
        proxy_mode = "vpn"

    class _FakeStation:
        _station = 1

    pool = pool_mod.FreeIPPool.__new__(pool_mod.FreeIPPool)
    pool._vpn = _FakeVPN()
    pool._stations = [object()]  # dual_station=False (1 station)
    pool._on_429_action = "both"
    pool._note_upstream_429 = lambda station: noted.append(station)
    pool._launch_rotation = lambda station, forced_pool=None: launched.append(station)
    pool._any_other_usable = lambda station: False

    st = _FakeStation()
    pool.on_quota_exhausted(st, quota=False)

    assert noted == [st]  # compteur corrélé toujours alimenté
    assert launched == []  # ... mais AUCUNE rotation lancée


def test_quota_default_still_rotates(monkeypatch):
    import free.pool as pool_mod

    launched = []

    class _FakeVPN:
        enabled = True
        proxy_mode = "vpn"

    class _FakeStation:
        _station = 1

    pool = pool_mod.FreeIPPool.__new__(pool_mod.FreeIPPool)
    pool._vpn = _FakeVPN()
    pool._stations = [object()]  # dual_station=False (1 station)
    pool._on_429_action = "both"
    pool._note_upstream_429 = lambda station: None
    pool._launch_rotation = lambda station, forced_pool=None: launched.append(station)
    pool._upstream_event_active = lambda: False
    pool._any_other_usable = lambda station: False

    st = _FakeStation()
    pool.on_quota_exhausted(st)  # quota=True par défaut → comportement historique

    assert launched == [st]


# ── 4. Isolation single-station : un quota sur N ne bouge que N ──────
#
# Exigence : « si une station a une IP dépassée, les autres ne doivent pas
# changer ; la station qui a l'IP hors quota doit changer d'IP mais les
# autres IP des autres stations doivent rester intactes ».
#
# Ces tests prouvent (hermétique, 0 quota brûlé, aucun docker) que :
#   a) on_quota_exhausted(stN) ne lance une rotation QUE pour stN
#      (ni _launch_rotation sur une autre station, ni bad-mark ailleurs) ;
#   b) _launch_rotation est single-flight PAR station : N 429 concurrents
#      sur la même station = 1 seule rotation enfilée (pas de storm) ;
#      et deux stations en 429 enfilent 2 rotations DISTINCTES, une par
#      station (pas de fusion, pas de contamination) ;
#   c) _launch_rotation ignore une station inconnue (retirée par
#      downscale) : aucune entrée enfilée, aucun docker touché.


class _IsoVPN:
    enabled = True
    proxy_mode = "vpn"


class _IsoStation:
    """Station fake : up, IP stable, utilisable par _station_usable."""

    def __init__(self, sid, ip):
        self._station = sid
        self.status = "connected"
        self.current_ip = ip
        self._config = {}
        self._current_country = "Switzerland"


def _iso_pool(nstations=3):
    import free.pool as pool_mod

    stations = [_IsoStation(i + 1, f"10.0.0.{i + 1}") for i in range(nstations)]
    pool = pool_mod.FreeIPPool.__new__(pool_mod.FreeIPPool)
    pool._vpn = _IsoVPN()
    pool._stations = list(stations)
    pool._station_ids = {s._station for s in stations}
    pool._on_429_action = "both"
    pool._per = {}
    pool._connect_retry_interval = 300.0
    pool._bad_ttl = 60.0
    pool._bad_ttl_by_cause = {}
    pool._free_parallel_enabled = False
    pool._free_parallel_routing = "round-robin"
    pool._rotation_wait_timeout = 5.0
    pool.latency_engine = None
    pool._failover_exhausted_cb = None
    pool._upstream_429_log = __import__("collections").deque()
    pool._upstream_event_until = 0.0
    pool._upstream_event_threshold = 10**9  # storm-guard désactivé ici
    pool._upstream_event_window_s = 60.0
    pool._upstream_event_cool_s = 180.0
    pool._rotation_tasks = {}
    pool._pending = set()
    import itertools as _it
    from asyncio import PriorityQueue as _PQ

    pool._rotation_queue = _PQ()
    pool._rotation_seq = _it.count()
    pool._worker_tasks = []
    pool._ensure_workers = lambda: None  # pas de loop nécessaire : la file suffit
    return pool, stations


def test_quota_station_N_rotates_only_N():
    """429 quota (vrai motif) sur st2 → rotation enfilée pour st2 SEULE,
    bad-mark posé sur st2 SEULE, st1/st3 intacts (pas de bad_until)."""
    pool, stations = _iso_pool(3)
    st1, st2, st3 = stations

    launched = []
    pool._launch_rotation = lambda station, forced_pool=None: launched.append(station)

    pool.on_quota_exhausted(st2, quota=True)

    assert launched == [st2]
    assert pool._per_station(st2).get("bad_until") is not None
    assert pool._per_station(st2).get("bad_cause") == "rate_limit"
    assert pool._per_station(st1).get("bad_until") is None
    assert pool._per_station(st3).get("bad_until") is None
    assert st1.current_ip == "10.0.0.1"
    assert st3.current_ip == "10.0.0.3"


def test_quota_single_flight_per_station():
    """N 429 concurrents sur la MÊME station = 1 seule rotation enfilée."""
    pool, stations = _iso_pool(2)
    pool.on_quota_exhausted(stations[0], quota=True)
    pool.on_quota_exhausted(stations[0], quota=True)
    pool.on_quota_exhausted(stations[0], quota=True)

    assert pool._pending == {1}
    queued = []
    while not pool._rotation_queue.empty():
        queued.append(pool._rotation_queue.get_nowait()[2])
    assert queued == [stations[0]]


def test_quota_two_stations_two_distinct_rotations():
    """429 sur st1 PUIS st2 → 2 entrées DISTINCTES (une par station),
    jamais une rotation « globale » qui toucherait les deux."""
    pool, stations = _iso_pool(3)
    pool.on_quota_exhausted(stations[0], quota=True)
    pool.on_quota_exhausted(stations[1], quota=True)

    assert pool._pending == {1, 2}
    queued = []
    while not pool._rotation_queue.empty():
        queued.append(pool._rotation_queue.get_nowait()[2])
    assert [s._station for s in queued] == [1, 2]
    assert pool._per_station(stations[2]).get("bad_until") is None


def test_launch_rotation_ignores_unknown_station():
    """Station retirée (downscale) : _launch_rotation = no-op,
    rien enfilé, rien touché."""
    pool, stations = _iso_pool(2)
    ghost = _IsoStation(9, "10.9.9.9")
    pool._launch_rotation(ghost)
    assert pool._pending == set()
    assert pool._rotation_queue.empty()


# ── 5. _mark_free_stations_429 : quota ≠ signal amont, transitoire = signal ──


def test_mark_quota_429_rotates_without_feeding_breaker(monkeypatch):
    """[fix quota-vs-breaker] Un VRAI quota per-IP (FreeUsageLimitError /
    rate limit) rotationne chaque station mais n'alimente jamais le log
    corrélé : 3 stations épuisées ne doivent pas déclarer un événement amont
    ni geler les rotations suivantes."""
    import opencode as oc

    pool, stations = _iso_pool(3)
    launched = []
    pool._launch_rotation = lambda station, forced_pool=None, **kw: launched.append(station)
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    monkeypatch.setattr(oc, "_record_global_429", lambda: None)
    monkeypatch.setattr(oc, "_note_free_429", lambda model: None)
    monkeypatch.setattr(oc, "_set_free_cooldown", lambda *a, **k: None)
    monkeypatch.setattr(oc._cfg_settings, "get_429_action", lambda: "both")

    for st in stations:
        oc._mark_free_stations_429("m", "30", stations=[st], body=QUOTA_RATE_LIMIT)

    assert launched == stations, "chaque quota per-IP doit rotationner sa station"
    assert len(pool._upstream_429_log) == 0, "un quota per-IP n'est pas un signal amont"
    assert pool._upstream_event_active() is False


def test_mark_transient_429_feeds_breaker_without_rotation(monkeypatch):
    """[fix quota-vs-breaker] Symétrique : un 429 transitoire (Endpoint is
    unavailable) alimente le log corrélé — 3 couples distincts déclarent
    l'événement — sans rotation ni bad-mark de station."""
    import opencode as oc

    pool, stations = _iso_pool(3)
    pool._upstream_event_threshold = 3
    launched = []
    pool._launch_rotation = lambda station, forced_pool=None, **kw: launched.append(station)
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    monkeypatch.setattr(oc, "_record_global_429", lambda: None)
    monkeypatch.setattr(oc, "_note_free_429", lambda model: None)
    monkeypatch.setattr(oc, "_set_free_cooldown", lambda *a, **k: None)
    monkeypatch.setattr(oc._cfg_settings, "get_429_action", lambda: "both")

    for st in stations:
        oc._mark_free_stations_429("m", "30", stations=[st], body=UNAVAILABLE)

    assert launched == [], "un 429 transitoire ne rotationne pas"
    assert len(pool._upstream_429_log) == 3
    assert pool._upstream_event_active() is True
    assert all(pool._per_station(st).get("bad_until") is None for st in stations), (
        "une surcharge amont ne doit pas bad-mark les stations"
    )


# ── 6. _free_stream_refuse_bytes : parité véridique avec le non-stream ──
#
# Le chemin stream affirmait « Free quota exhausted on all VPN stations. »
# pour TOUT 429, y compris « Endpoint is unavailable » (surcharge serveur
# transitoire). Miroir de free_refusal_response :
#   - 429 + motif quota → message historique + rate_limit_error ;
#   - 429 transitoire → message véridique + type classé (overloaded_error),
#     PAS « quota épuisé », PAS rate_limit_error.


class _StreamFakeResp:
    """Réponse free minimale pour _free_stream_refuse_bytes."""

    def __init__(self, status_code, body: bytes, retry_after: str = ""):
        self.status_code = status_code
        self._body = body
        self.headers = {"retry-after": retry_after} if retry_after else {}

    async def aread(self):
        return self._body


def _run_refuse(monkeypatch, status_code, body: bytes, protocol="openai"):
    """Appelle _free_stream_refuse_bytes avec DB neutralisée, rend (octets, payload)."""
    import asyncio
    import json as _json

    import opencode as _oc

    async def _noop_save(*a, **k):
        return None

    monkeypatch.setattr(_oc, "_save_request", _noop_save)
    monkeypatch.setattr(_oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(_oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(_oc, "_log_free_model_usage", lambda *a, **k: None)

    # [incident st1 12/10] boucle robuste à l'ordre d'exécution : un test
    # async précédent (pytest-asyncio strict) peut avoir fermé la boucle.
    try:
        _loop = asyncio.get_event_loop()
    except RuntimeError:
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    out = _loop.run_until_complete(
        _oc._free_stream_refuse_bytes(
            _StreamFakeResp(status_code, body),
            "req-test",
            "mimo-v2.6-flash-free",
            "mimo",
            0.0,
            protocol,
            None,
            None,
            "127.0.0.1",
            [],
            {},
        )
    )
    assert out.endswith(b"data: [DONE]\n\n")
    first = out.split(b"\n")[0]
    assert first.startswith(b"data: ")
    return out, _json.loads(first[len(b"data: "):].decode())


def test_stream_transient_429_does_not_claim_quota_exhausted(monkeypatch):
    """429 « Endpoint is unavailable » en stream → message véridique,
    type overloaded_error, jamais « quota épuisé » / rate_limit_error."""
    _, payload = _run_refuse(monkeypatch, 429, UNAVAILABLE.encode())
    msg = payload["error"]["message"]
    assert "Free quota exhausted" not in msg
    assert "Endpoint is unavailable" in msg
    assert payload["error"]["type"] == "overloaded_error"


def test_stream_real_quota_429_keeps_historic_message(monkeypatch):
    """429 quota réel en stream → message historique + rate_limit_error."""
    _, payload = _run_refuse(monkeypatch, 429, QUOTA_RATE_LIMIT.encode())
    assert payload["error"]["message"] == "Free quota exhausted on all VPN stations."
    assert payload["error"]["type"] == "rate_limit_error"
