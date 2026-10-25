"""Préchauffage TLS : garder la session chaude sans consommer de quota.

Mesuré le 2026-10-25 : handshake à travers le tunnel = ~600 ms, reuse = ~160 ms.
Le pool évince une session après 10 min d'inactivité (TTL 600 s) ; sans
préchauffage, la première requête qui suit repaie le handshake.
"""

import pytest

import opencode as oc


class _Resp:
    async def aclose(self):
        return None


class _Sess:
    def __init__(self):
        self.gets = []

    async def get(self, url, timeout=None):
        self.gets.append(url)
        return _Resp()


class _Slot:
    def __init__(self, sess):
        self.sess = sess


class _Pool:
    def __init__(self):
        self.checked_in = 0

    async def checkin(self, slot):
        self.checked_in += 1


class _Station:
    current_ip = "10.0.0.1"


class _FreePool:
    def __init__(self, proxy="socks5://127.0.0.1:1080", enabled=True):
        self.enabled = enabled
        self.proxy_url = proxy
        self.active_station = _Station()


@pytest.fixture()
def wiring(monkeypatch):
    sess = _Sess()
    pool = _Pool()

    async def _get(proxy_url, impersonate, fp=None, egress_ip=None):
        return pool, _Slot(sess)

    monkeypatch.setattr(oc, "_get_pooled_curl_session", _get)
    return sess, pool


@pytest.mark.asyncio
async def test_prewarm_fait_un_get_statique_et_rend_la_session(monkeypatch, wiring):
    sess, pool = wiring
    monkeypatch.setattr(oc, "_free_ip_pool", _FreePool())
    assert await oc._free_prewarm_once() is True
    assert len(sess.gets) == 1
    assert sess.gets[0].endswith("/robots.txt")
    assert pool.checked_in == 1


@pytest.mark.asyncio
async def test_prewarm_ne_touche_jamais_l_endpoint_inference(monkeypatch, wiring):
    sess, _ = wiring
    monkeypatch.setattr(oc, "_free_ip_pool", _FreePool())
    await oc._free_prewarm_once()
    assert all("inference" not in u for u in sess.gets)
    assert all("chat/completions" not in u for u in sess.gets)


@pytest.mark.asyncio
async def test_pool_desactive_ne_fait_rien(monkeypatch, wiring):
    sess, _ = wiring
    monkeypatch.setattr(oc, "_free_ip_pool", _FreePool(enabled=False))
    assert await oc._free_prewarm_once() is False
    assert sess.gets == []


@pytest.mark.asyncio
async def test_pas_de_proxy_ne_fait_rien(monkeypatch, wiring):
    sess, _ = wiring
    monkeypatch.setattr(oc, "_free_ip_pool", _FreePool(proxy=None))
    assert await oc._free_prewarm_once() is False
    assert sess.gets == []


@pytest.mark.asyncio
async def test_erreur_reseau_reste_fail_soft_et_rend_la_session(monkeypatch):
    pool = _Pool()

    class _Boom:
        async def get(self, url, timeout=None):
            raise RuntimeError("boom")

    async def _get(proxy_url, impersonate, fp=None, egress_ip=None):
        return pool, _Slot(_Boom())

    monkeypatch.setattr(oc, "_get_pooled_curl_session", _get)
    monkeypatch.setattr(oc, "_free_ip_pool", _FreePool())
    assert await oc._free_prewarm_once() is False
    assert pool.checked_in == 1
