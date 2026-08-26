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
