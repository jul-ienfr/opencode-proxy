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
    pool._stations = [st_dummy := object()]  # dual_station=False (1 station)
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
