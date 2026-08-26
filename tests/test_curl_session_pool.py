"""[A1 audit vitesse] Pool de M sessions curl par (proxy, impersonate).

Gate Phase 2 du plan « Audit vitesse complet » :
  - les POST non-streaming d'une même station ne sont plus sérialisés
    derrière le téléchargement complet (head-of-line blocking) ;
  - une session n'est JAMAIS partagée concurrentment (zéro réponse
    entrelacée/corrompue possible) ;
  - l'éviction d'une session fautive libère sa place sans tuer le pool ;
  - la taille M est bornée (checkout en attente au-delà de M).
"""

import asyncio
import time

import pytest

import opencode as oc


class _FakeCurlSession:
    """Session factice : trace le chevauchement des requêtes par session."""

    def __init__(self, log: list, delay: float, error=None):
        self._log = log
        self._delay = delay
        self._error = error
        self.inflight = 0
        self.max_inflight = 0
        self.closed = False

    async def post(self, *args, **kwargs):
        if self._error is not None:
            raise self._error
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        self._log.append(("enter", id(self)))
        try:
            await asyncio.sleep(self._delay)
        finally:
            self._log.append(("exit", id(self)))
            self.inflight -= 1
        return {"fake": "response"}

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_non_stream_requests_overlap(monkeypatch):
    """6 emprunts concurrents, M=3, post 0.12 s : le mur total prouve le
    parallélisme (≈2 vagues ≈0.25 s) au lieu de 6×0.12=0.72 s sérialisé."""
    log: list = []
    created: list = []

    def _factory(**kw):
        sess = _FakeCurlSession(log, 0.12)
        created.append(sess)
        return sess

    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _factory)

    async def _one():
        pool, slot = await oc._get_pooled_curl_session("socks5://t:1080", "chrome131")
        try:
            await asyncio.sleep(0)  # cède : force l'entrelacement réel
            await slot.sess.post("http://x")
        except BaseException:
            await pool.evict(slot)
            raise
        else:
            await pool.checkin(slot)

    t0 = time.monotonic()
    await asyncio.gather(*(_one() for _ in range(6)))
    elapsed = time.monotonic() - t0

    assert len(created) <= 3, "M sessions max pour une même clé"
    assert elapsed < 0.55, f"sérialisation détectée ({elapsed:.2f}s ≥ 6×0.12)"
    # zéro partage intra-session : chaque session n'a jamais >1 requête
    assert all(s.max_inflight == 1 for s in created)


@pytest.mark.asyncio
async def test_checkout_waits_beyond_max_size(monkeypatch):
    """Au-delà de M emprunts simultanés, le surplus ATTEND une restitution
    (aucune création infinie, aucun partage)."""
    created: list = []

    def _factory(**kw):
        sess = _FakeCurlSession([], 0.05)
        created.append(sess)
        return sess

    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _factory)

    pool = oc._CurlSessionPool(2)
    slots = [await pool.checkout(lambda: _factory()) for _ in range(2)]
    assert all(s.busy for s in pool.slots)
    assert len(pool.slots) == 2

    started = asyncio.Event()

    async def _waiter():
        started.set()
        return await pool.checkout(lambda: _factory())

    waiter = asyncio.ensure_future(_waiter())
    await started.wait()
    await asyncio.sleep(0.02)
    assert not waiter.done(), "le 3e emprunt doit attendre (pool saturé)"
    await pool.checkin(slots[0])
    got = await asyncio.wait_for(waiter, 1.0)
    assert not got.busy or got is slots[0]
    assert len(created) == 2, "aucune session créée au-delà de M"


@pytest.mark.asyncio
async def test_eviction_removes_only_faulty_session(monkeypatch):
    """Une session fautive est évictée ; le pool continue de servir avec
    les sessions saines restantes."""
    import curl_cffi.requests.errors as curl_err

    bad = _FakeCurlSession([], 0, error=curl_err.RequestsError("connect failed", 7))
    good = _FakeCurlSession([], 0)

    seq = [bad, good]

    def _factory(**kw):
        return seq.pop(0)

    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _factory)
    monkeypatch.setattr(
        oc, "_curl_proxy_url", lambda p: p
    )  # neutralise la résolution proxy

    pool, slot = await oc._get_pooled_curl_session("socks5://e:1080", "chrome131")
    assert slot.sess is bad
    with pytest.raises(curl_err.RequestsError):
        await slot.sess.post("http://x")
    await pool.evict(slot)
    assert bad.closed and all(s is not bad for s in pool.slots)

    pool2, slot2 = await oc._get_pooled_curl_session("socks5://e:1080", "chrome131")
    assert slot2.sess is good, "l'emprunt suivant doit tomber sur la session saine"
    await pool2.checkin(slot2)


@pytest.mark.asyncio
async def test_pooled_sessions_inherit_bounded_timeout(monkeypatch):
    """[P1.1 perf] La factory de session poolée pose timeout=(10, 600) :
    tout POST qui n'en passe pas (streams free, geo) hérite d'une borne
    connect/read au lieu du défaut curl_cffi (illimité)."""
    captured_kwargs: list[dict] = []

    def _factory(**kw):
        captured_kwargs.append(kw)
        return _FakeCurlSession([], 0)

    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _factory)
    monkeypatch.setattr(oc, "_curl_proxy_url", lambda p: p)

    pool, slot = await oc._get_pooled_curl_session("socks5://t:1080", "chrome131")
    await pool.checkin(slot)
    assert captured_kwargs, "la factory doit être invoquée"
    assert captured_kwargs[0].get("timeout") == (10, 600), (
        "session poolée sans timeout borné — POST stream/geo peut pendre indéfiniment"
    )
    assert captured_kwargs[0].get("impersonate") == "chrome131"


@pytest.mark.asyncio
async def test_cancelled_post_evicts_and_closes_session(monkeypatch):
    """[P2.1] Un POST annulé (stop client) doit FERMER la session via
    _evict_later — discard() seul la retirait du tracking sans close
    (fuite socket à chaque stop Claude Code pendant un POST)."""
    created: list = []

    def _factory(**kw):
        sess = _FakeCurlSession([], 0.5)  # POST long → annulé en cours
        created.append(sess)
        return sess

    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _factory)

    pool, slot = await oc._get_pooled_curl_session("socks5://c:1080", "chrome131")

    async def _cancelled_request():
        try:
            await slot.sess.post("http://x")
        except asyncio.CancelledError:
            oc._evict_later(pool, slot)
            raise

    task = asyncio.ensure_future(_cancelled_request())
    await asyncio.sleep(0.05)  # laisse le POST entrer
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)  # laisse tourner la tâche d'éviction différée

    assert created[0].closed, "session fermée après annulation (pas de fuite socket)"
    assert all(s is not slot for s in pool.slots), "slot évicté du pool"


@pytest.mark.asyncio
async def test_idle_orphan_pool_evicted_by_sweep(monkeypatch):
    """[P2.2] Un pool sans slot emprunté, idle > TTL, est pop du dict PUIS
    fermé ; un pool avec un slot busy est JAMAIS touché (garde vital :
    close_all() toucherait les slots empruntés restés listés)."""
    created: list = []

    def _factory(**kw):
        sess = _FakeCurlSession([], 0)
        created.append(sess)
        return sess

    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _factory)

    # Pool A : idle depuis > TTL
    pool_a, slot_a = await oc._get_pooled_curl_session("socks5://a:1080", "chrome131")
    await pool_a.checkin(slot_a)
    pool_a.last_used -= oc._CURL_POOL_IDLE_TTL + 1.0

    # Pool B : idle > TTL MAIS un slot emprunté (busy)
    pool_b, slot_b = await oc._get_pooled_curl_session("socks5://b:1080", "chrome131")
    pool_b.last_used -= oc._CURL_POOL_IDLE_TTL + 1.0

    # Pool C : récent (idle < TTL) → intouché
    pool_c, slot_c = await oc._get_pooled_curl_session("socks5://c:1080", "chrome131")
    await pool_c.checkin(slot_c)

    key_a = "socks5://a:1080|chrome131"
    key_b = "socks5://b:1080|chrome131"
    key_c = "socks5://c:1080|chrome131"
    assert key_a in oc._curl_pool and key_b in oc._curl_pool and key_c in oc._curl_pool

    freed = await oc._evict_idle_curl_pools()
    assert freed == 1
    assert key_a not in oc._curl_pool, "pool orphelin idle évité"
    assert created[0].closed, "sessions du pool évité fermées"
    assert key_b in oc._curl_pool, "pool avec slot busy JAMAIS évité"
    assert not created[1].closed
    assert key_c in oc._curl_pool, "pool récent conservé"


@pytest.mark.asyncio
async def test_busy_count_property_tracks_checkout():
    """[P2.2] busy_count reflète les slots empruntés (les busy restent listés)."""
    pool = oc._CurlSessionPool(3)
    s1 = await pool.checkout(lambda: _FakeCurlSession([], 0))
    assert pool.busy_count == 1 and len(pool.slots) == 1
    s2 = await pool.checkout(lambda: _FakeCurlSession([], 0))
    assert pool.busy_count == 2 and len(pool.slots) == 2
    await pool.checkin(s1)
    assert pool.busy_count == 1
    await pool.checkin(s2)
    assert pool.busy_count == 0


@pytest.mark.asyncio
async def test_do_free_request_parallel_via_helper(monkeypatch):
    """Bout-en-bout _do_free_request_curl_cffi : 4 appels concurrents vers la
    même station se chevauchent (fin du head-of-line blocking) via le chemin
    réel checkout→POST→checkin."""
    import curl_cffi.requests.errors as _unused_err  # noqa: F401

    log: list = []
    created: list = []

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"ok": true}'

    def _factory(**kw):
        sess = _FakeCurlSession(log, 0.10)
        sess.post = _make_post(sess)  # type: ignore[method-assign]
        created.append(sess)
        return sess

    def _make_post(sess):
        async def post(*a, **kw):
            sess.inflight += 1
            sess.max_inflight = max(sess.max_inflight, sess.inflight)
            await asyncio.sleep(0.10)
            sess.inflight -= 1
            return _Resp()

        return post

    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _factory)
    monkeypatch.setattr(
        oc,
        "_current_free_identity",
        lambda station=None: {
            "impersonate": "chrome131",
            "user_agent": None,
            "extra_headers": {},
        },
    )
    monkeypatch.setattr(oc, "_free_ip_pool", None)

    t0 = time.monotonic()
    results = await asyncio.gather(
        *(
            oc._do_free_request_curl_cffi({}, {}, "socks5://p:1080", None)
            for _ in range(4)
        )
    )
    elapsed = time.monotonic() - t0

    assert all(r.status_code == 200 for r in results)
    assert len(created) <= 3
    assert elapsed < 0.32, f"head-of-line blocking encore présent ({elapsed:.2f}s)"
    assert all(s.max_inflight == 1 for s in created), "session partagée détectée"
