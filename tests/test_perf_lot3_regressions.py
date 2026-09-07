"""[plan-perf-fiabilite-6stations Lot 3] Régression des corrections de
comportement (Lots 1-2).

Verrouille, sans réseau ni VPN ni DB :
  A. breaker `opencode._CircuitBreaker` : sonde half-open UNIQUE
     (l'existant `test_breaker_single_probe_half_open` ne couvre que
     `vpn_manager.CircuitBreaker`) + rollback `half_open_single_probe=false`
     + expiration de sonde ;
  B. `_key_failover_index` sticky : pause -> avance, sticky, levée de
     pause sans retour arrière, boucle bornée (AllKeysPausedError) ;
  C. watchdog TTFB paid, chemins stream (`_open_free_stream`,
     `use_free=False`, consommé en `async with` comme les tests
     existants) et non-stream (`_do_request_with_retry`) : timeout ->
     même-clé-connexion-neuve -> clé-alt (2 tentatives max, 504
     ensuite), sans clé alt -> 1 seule retentative, stream vivant
     jamais interrompu ;
  D. compteurs hit-rate du cache de conversion (lus par nothing d'autre
     que /metrics + bench — assertion directe ici).

Never touches the live system : doubles complets (clients/fakes),
timeouts watchdog réduits à ~20 ms, sleeps réels max ~30 ms.
"""

import asyncio
import time
from contextlib import asynccontextmanager

import pytest

import opencode as oc

# ── socle ──────────────────────────────────────────────────────────────


def _two_keys():
    return [
        {"api_key": "k0-lot3", "alias": "a0", "enabled": True},
        {"api_key": "k1-lot3", "alias": "a1", "enabled": True},
    ]


@pytest.fixture
def _iso_keys(monkeypatch):
    """API_KEYS + pauser + index + compteurs TTFB isolés du global réel."""
    monkeypatch.setattr(oc, "API_KEYS", _two_keys())
    monkeypatch.setattr(oc, "_key_pauser", oc._KeyPauser())
    monkeypatch.setattr(oc, "_key_failover_index", 0)
    monkeypatch.setattr(oc, "API_KEY_ROUTING", "failover")
    monkeypatch.setattr(oc, "_TTFB_FAILOVER_COUNTS", {})
    return oc


@pytest.fixture
def _ttfb_fast(monkeypatch):
    """Watchdog armé, seuil ~20 ms (pas d'attente 90 s en tests)."""
    monkeypatch.setattr(oc, "_ttfb_watchdog_enabled", lambda: True)
    monkeypatch.setattr(oc, "_ttfb_watchdog_timeout_s", lambda: 0.02)
    return oc


class _HangClient:
    """Fake client httpx : stream()/post() pendus (TTFB infini), construction
    comptée. Forme @asynccontextmanager comme les doubles établis
    (test_http_client_self_heal)."""

    def __init__(self, record, tag):
        self._record = record
        self._tag = tag
        self.calls = 0

    @asynccontextmanager
    async def stream(self, method, url, **kw):
        self.calls += 1
        self._record.append((self._tag, "stream"))
        await asyncio.sleep(30.0)
        yield None  # unreachable — wait_for coupe avant

    async def post(self, *a, **k):
        self.calls += 1
        self._record.append((self._tag, "post"))
        await asyncio.sleep(30.0)
        raise AssertionError("unreachable — wait_for coupe avant")

    async def aclose(self):
        self._record.append((self._tag, "aclose"))


class _OkResp:
    """Headers immédiatement disponibles (TTFB ~0) — stream vivant."""

    status_code = 200
    headers = {"content-type": "text/event-stream"}


class _OkClient:
    """Headers immédiatement disponibles (TTFB ~0) — stream vivant."""

    def __init__(self, record, tag, resp):
        self._record = record
        self._tag = tag
        self._resp = resp
        self.calls = 0

    @asynccontextmanager
    async def stream(self, method, url, **kw):
        self.calls += 1
        self._record.append((self._tag, "stream"))
        yield self._resp

    async def aclose(self):
        self._record.append((self._tag, "aclose"))


# ── A. sonde half-open unique ──────────────────────────────────────────


def _open_breaker():
    cb = oc._CircuitBreaker()
    cb.state = "open"
    cb.opened_at = time.monotonic() - 3600.0  # cooldown expiré quoi qu'il arrive
    return cb


def test_half_open_single_probe(monkeypatch):
    """Exactement UNE requête passe en half_open, les concurrentes sont
    rejetées comme open ; le succès referme."""
    monkeypatch.setattr(oc, "_cb_half_open_probe_enabled", lambda: True)
    cb = _open_breaker()
    assert cb.should_allow() is True  # la sonde
    assert cb.should_allow() is False  # concurrentes rejetées
    assert cb.should_allow() is False
    cb.record_success()
    assert cb.state == "closed"
    assert cb.should_allow() is True


def test_half_open_rollback_allows_all(monkeypatch):
    """`half_open_single_probe: false` = comportement historique (tout passe)."""
    monkeypatch.setattr(oc, "_cb_half_open_probe_enabled", lambda: False)
    cb = _open_breaker()
    assert cb.should_allow() is True
    assert cb.should_allow() is True
    assert cb.should_allow() is True


def test_half_open_probe_expiry(monkeypatch):
    """Sonde présumée morte (>2×cooldown sans conclusion) : reprise autorisée."""
    monkeypatch.setattr(oc, "_cb_half_open_probe_enabled", lambda: True)
    cb = _open_breaker()
    assert cb.should_allow() is True
    assert cb.should_allow() is False
    cb.half_open_since = time.monotonic() - 10 * oc._CB_RECOVERY_TIMEOUT
    assert cb.should_allow() is True


# ── B. index failover sticky ───────────────────────────────────────────


def test_failover_index_advances_and_sticks(_iso_keys):
    m = _iso_keys
    assert m.get_next_api_key()["api_key"] == "k0-lot3"
    assert m._key_failover_index == 0
    m._key_pauser.pause_key("k0-lot3", 3600, "test-429")
    assert m.get_next_api_key()["api_key"] == "k1-lot3"  # avance
    assert m._key_failover_index == 1
    assert m.get_next_api_key()["api_key"] == "k1-lot3"  # sticky
    assert m._key_failover_index == 1
    m._key_pauser.unpause_if_paused("k0-lot3")
    assert m.get_next_api_key()["api_key"] == "k1-lot3"  # pas de retour arrière
    assert m._key_failover_index == 1


def test_failover_all_paused_raises_bounded(_iso_keys):
    """Toutes pausées -> AllKeysPausedError (boucle bornée, jamais de hang)."""
    m = _iso_keys
    m._key_pauser.pause_key("k0-lot3", 3600, "t")
    m._key_pauser.pause_key("k1-lot3", 3600, "t")
    with pytest.raises(oc.AllKeysPausedError):
        m.get_next_api_key()


def test_failover_single_key_served(_iso_keys, monkeypatch):
    """Une seule clé configured -> servie (pas de shortcut cassé)."""
    m = _iso_keys
    monkeypatch.setattr(oc, "API_KEYS", [_two_keys()[0]])
    assert m.get_next_api_key()["api_key"] == "k0-lot3"


# ── C. watchdog TTFB ───────────────────────────────────────────────────


def _stream_headers():
    return {"x-api-key": "k0-lot3", "Content-Type": "application/json"}


@pytest.mark.asyncio
async def test_watchdog_stream_no_alt_single_retry_then_504(_iso_keys, _ttfb_fast, monkeypatch):
    """TTFB infini, sans clé alt : 1 retentative même-clé puis 504."""
    m = _iso_keys
    monkeypatch.setattr(m, "API_KEYS", [_two_keys()[0]])  # une seule clé -> pas d'alt
    monkeypatch.setattr(m, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(m, "_free_ip_pool", None)
    record = []
    shared, fresh = _HangClient(record, "shared"), _HangClient(record, "fresh")
    monkeypatch.setattr(m, "_ensure_http_client", lambda: shared)
    monkeypatch.setattr(m, "_fresh_http_client", lambda: fresh)
    with pytest.raises(m.UpstreamError) as ei:
        async with m._open_free_stream(
            "https://up.example/v1", {"model": "x"}, _stream_headers(), use_free=False
        ):
            pass
    assert ei.value.status_code == 504
    assert shared.calls == 1, "stage 0 = client partagé"
    assert fresh.calls == 1, "stage 1 = connexion neuve, pas de stage 2 sans clé alt"
    counts = m._TTFB_FAILOVER_COUNTS
    assert counts.get(("a0", "same_key_new_conn"), 0) == 1
    assert counts.get(("a0", "no_alt_504"), 0) == 1


@pytest.mark.asyncio
async def test_watchdog_stream_alt_key_then_abort(_iso_keys, _ttfb_fast, monkeypatch):
    """TTFB infini, clé alt dispo : même-clé puis clé-alt (headers re-signés),
    ensuite 504 (borne 2 tentatives)."""
    m = _iso_keys
    monkeypatch.setattr(m, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(m, "_free_ip_pool", None)
    record = []
    shared, fresh = _HangClient(record, "shared"), _HangClient(record, "fresh")
    monkeypatch.setattr(m, "_ensure_http_client", lambda: shared)
    monkeypatch.setattr(m, "_fresh_http_client", lambda: fresh)
    seen_headers = []
    orig_get_auth = m._get_auth_headers

    def spy_auth(protocol, entry=None):
        h = orig_get_auth(protocol, entry=entry)
        seen_headers.append(dict(h))
        return h

    monkeypatch.setattr(m, "_get_auth_headers", spy_auth)
    with pytest.raises(m.UpstreamError) as ei:
        async with m._open_free_stream(
            "https://up.example/v1", {"model": "x"}, _stream_headers(), use_free=False
        ):
            pass
    assert ei.value.status_code == 504
    assert shared.calls == 1 and fresh.calls == 2, "stage 0 partagé, stages 1-2 jetables"
    assert seen_headers and seen_headers[-1].get("x-api-key") == "k1-lot3", "stage 2 re-signé k1"
    counts = m._TTFB_FAILOVER_COUNTS
    assert counts.get(("a0", "same_key_new_conn"), 0) == 1
    assert counts.get(("a1", "alt_key"), 0) == 1
    assert counts.get(("exhausted", "abort_504"), 0) == 1


@pytest.mark.asyncio
async def test_watchdog_stream_alive_never_interrupted(_iso_keys, _ttfb_fast, monkeypatch):
    """Headers reçus tout de suite : le watchdog est désarmé — la réponse
    passe, aucun failover, client jetable jamais construit (un silence
    intra-stream de 120 s ne déclencherait rien : le watchdog ne couvre
    que l'obtention des headers)."""
    m = _iso_keys
    monkeypatch.setattr(m, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(m, "_free_ip_pool", None)
    record = []
    resp = _OkResp()
    shared = _OkClient(record, "shared", resp)

    def boom_fresh():
        raise AssertionError("client jetable inutilisé sur stream vivant")

    monkeypatch.setattr(m, "_ensure_http_client", lambda: shared)
    monkeypatch.setattr(m, "_fresh_http_client", boom_fresh)
    async with m._open_free_stream(
        "https://up.example/v1", {"model": "x"}, _stream_headers(), use_free=False
    ) as got:
        assert got is resp
    assert shared.calls == 1
    assert m._TTFB_FAILOVER_COUNTS == {}


@pytest.mark.asyncio
async def test_watchdog_disabled_is_exact_rollback(_iso_keys, monkeypatch):
    """`ttfb_watchdog.enabled=false` = comportement historique exact : pas
    de wait_for, pas de client jetable, pas de métrique (l'invariant
    rollback du plan, régression du bug yaml_get 4-args du Lot 0)."""
    m = _iso_keys
    monkeypatch.setattr(m, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(m, "_free_ip_pool", None)
    monkeypatch.setattr(m, "_ttfb_watchdog_enabled", lambda: False)
    record = []
    resp = _OkResp()
    shared = _OkClient(record, "shared", resp)

    def boom_fresh():
        raise AssertionError("aucun client jetable quand le watchdog est off")

    def boom_timeout():
        raise AssertionError("aucun seuil lu quand le watchdog est off")

    monkeypatch.setattr(m, "_ensure_http_client", lambda: shared)
    monkeypatch.setattr(m, "_fresh_http_client", boom_fresh)
    monkeypatch.setattr(m, "_ttfb_watchdog_timeout_s", boom_timeout)
    async with m._open_free_stream(
        "https://up.example/v1", {"model": "x"}, _stream_headers(), use_free=False
    ) as got:
        assert got is resp
    assert shared.calls == 1
    assert m._TTFB_FAILOVER_COUNTS == {}


@pytest.mark.asyncio
async def test_watchdog_nonstream_no_alt_504(_iso_keys, _ttfb_fast, monkeypatch):
    """Miroir non-stream (`_do_request_with_retry`) : même séquence, 504."""
    m = _iso_keys
    monkeypatch.setattr(m, "API_KEYS", [_two_keys()[0]])  # une seule clé -> pas d'alt
    monkeypatch.setattr(m, "_debug", lambda *a, **k: None)
    record = []
    shared, fresh = _HangClient(record, "shared"), _HangClient(record, "fresh")
    monkeypatch.setattr(m, "_ensure_http_client", lambda: shared)
    monkeypatch.setattr(m, "_fresh_http_client", lambda: fresh)
    with pytest.raises(m.UpstreamError) as ei:
        await m._do_request_with_retry(
            "https://up.example/v1", {"model": "x"}, _stream_headers(), "anthropic"
        )
    assert ei.value.status_code == 504
    counts = m._TTFB_FAILOVER_COUNTS
    assert counts.get(("a0", "same_key_new_conn"), 0) == 1
    assert counts.get(("a0", "no_alt_504"), 0) == 1


# ── D. hit-rate conversion ─────────────────────────────────────────────


def test_conversion_cache_counters():
    """Les compteurs hit/miss lus par /metrics + bench bougent vraiment :
    1er appel = miss, 2e identique = hit (raw stable)."""
    import protocol_mapping as pm

    body = {
        "model": "glm-5.1",
        "system": "sys",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "ping-lot3"}]}],
        "max_tokens": 8,
    }
    raw = b"lot3-stable-bytes"
    before = pm.conversion_cache_stats()
    pm.anthropic_to_openai(dict(body), "glm-5.1", raw=raw)
    mid = pm.conversion_cache_stats()
    assert mid["miss"] == before["miss"] + 1
    pm.anthropic_to_openai(dict(body), "glm-5.1", raw=raw)
    after = pm.conversion_cache_stats()
    assert after["hit"] == mid["hit"] + 1
    assert after["miss"] == mid["miss"]
