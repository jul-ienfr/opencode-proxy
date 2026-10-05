"""test_websearch_cache.py — P2-12 : singleflight + TTL du fetch + regex précompilées.

- Sans cache/locks : comportement historique (1 HTTP par appel).
- Avec cache/locks : 2e appel servi du cache (0 HTTP supp.), N appels
  concurrents = 1 seul HTTP (singleflight), succès seuls cachés (les
  erreurs SSRF/HTTP ne polluent pas le cache).
- normalize_query : espaces/Unicode normalisés (non-régression précompilation).
"""

import asyncio
from collections import OrderedDict

import pytest

from server import websearch as ws


class _Resp:
    def __init__(self, text="hello world", status_code=200, headers=None):
        self.text = text
        self.content = text.encode()
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _Client:
    def __init__(self, resp):
        self.resp = resp
        self.calls = 0

    async def get(self, *a, **k):
        self.calls += 1
        await asyncio.sleep(0.01)  # fenêtre de concurrence pour le singleflight
        return self.resp


def _deps(client):
    return {
        "role_client_fn": lambda _role: client,
        "safe_fn": _allow,
        "sem": asyncio.Semaphore(5),
    }


async def _allow(url):
    return True


@pytest.mark.asyncio
async def test_fetch_without_cache_hits_http_twice():
    c = _Client(_Resp())
    kw = _deps(c)
    await ws.execute_web_fetch("https://example.com/x", **kw)
    await ws.execute_web_fetch("https://example.com/x", **kw)
    assert c.calls == 2


@pytest.mark.asyncio
async def test_fetch_cache_second_call_free():
    c = _Client(_Resp())
    kw = _deps(c)
    kw.update(cache=OrderedDict(), locks={})
    r1 = await ws.execute_web_fetch("https://example.com/x", **kw)
    r2 = await ws.execute_web_fetch("https://example.com/x", **kw)
    assert r1 == r2
    assert c.calls == 1


@pytest.mark.asyncio
async def test_fetch_singleflight_concurrent_one_http():
    c = _Client(_Resp())
    kw = _deps(c)
    kw.update(cache=OrderedDict(), locks={})
    rs = await asyncio.gather(*[ws.execute_web_fetch("https://example.com/x", **kw) for _ in range(8)])
    assert len(set(rs)) == 1
    assert c.calls == 1


@pytest.mark.asyncio
async def test_fetch_errors_not_cached():
    c = _Client(_Resp(status_code=500))
    kw = _deps(c)
    kw.update(cache=OrderedDict(), locks={})
    with pytest.raises(RuntimeError):
        await ws.execute_web_fetch("https://example.com/x", **kw)
    assert kw["cache"] == {}, "un échec ne doit pas empoisonner le cache"
    with pytest.raises(RuntimeError):
        await ws.execute_web_fetch("https://example.com/x", **kw)
    assert c.calls == 2


@pytest.mark.asyncio
async def test_fetch_ssrf_rejected_not_cached():
    c = _Client(_Resp())
    kw = _deps(c)
    kw.update(cache=OrderedDict(), locks={})

    async def _deny(url):
        return False

    kw["safe_fn"] = _deny
    with pytest.raises(ValueError):
        await ws.execute_web_fetch("https://example.com/x", **kw)
    assert c.calls == 0
    assert kw["cache"] == {}


def test_normalize_query_precompiled():
    assert ws.normalize_query("  Foo\tBAR\nbaz  ") == "foo bar baz"
    assert len(ws.normalize_query("x" * 600)) == 500
    assert ws._WS_RE.pattern == r"\s+"
    assert ws._TAG_RE.pattern == r"<[^>]+>"
