"""test_free_nonstream_status.py — le non-stream ne doit pas enterrer les erreurs upstream.

Bug : _do_free_request_curl_cffi forçait stream:true sur le wire puis
collectait le SSE SANS regarder le statut. Un refus upstream (429 JSON
brut, pas SSE) devenait UpstreamError("free: empty SSE collection", 502) :
le vrai quota (cooldown + rotation) était perdu et le client recevait un
503 mensonger au lieu du 429 honnête.
"""

import pytest

import opencode as oc


class _FakeCurlResp:
    def __init__(self, status_code, body: bytes, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {"content-type": "application/json"}

    async def aiter_lines(self):
        for ln in self._body.split(b"\n"):
            yield ln

    async def aclose(self):
        pass


class _FakeSlot:
    def __init__(self, resp):
        self.sess = self
        self._resp = resp

    async def post(self, *a, **k):
        return self._resp


class _FakeCurlPool:
    def __init__(self, slot):
        self._slot = slot

    async def checkin(self, s):
        pass

    async def evict(self, s):
        pass


def _patch_curl(monkeypatch, resp):
    slot = _FakeSlot(resp)
    pool = _FakeCurlPool(slot)

    async def _fake_pooled(proxy_url, impersonate, fp, egress_ip=None):
        return pool, slot

    monkeypatch.setattr(oc, "_get_pooled_curl_session", _fake_pooled)
    monkeypatch.setattr(oc, "_ensure_curl_cffi", lambda: True)


def _body():
    return {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.asyncio
async def test_nonstream_429_passes_through_with_real_status(monkeypatch):
    """429 JSON brut → réponse 429 (jamais 'empty SSE')."""
    raw = b'{"type":"error","error":{"type":"FreeUsageLimitError","message":"Free usage limit reached"}}'
    _patch_curl(monkeypatch, _FakeCurlResp(429, raw))

    resp = await oc._do_free_request_curl_cffi(_body(), {}, None, station=None)

    assert resp.status_code == 429
    assert "FreeUsageLimitError" in resp.text


@pytest.mark.asyncio
async def test_nonstream_400_passes_through(monkeypatch):
    raw = b'{"error":{"message":"bad request"}}'
    _patch_curl(monkeypatch, _FakeCurlResp(400, raw))

    resp = await oc._do_free_request_curl_cffi(_body(), {}, None, station=None)

    assert resp.status_code == 400
    assert "bad request" in resp.text


@pytest.mark.asyncio
async def test_nonstream_200_sse_still_collected(monkeypatch):
    """200 SSE → collecté comme avant (non-régression)."""
    raw = b'data: {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n'
    _patch_curl(monkeypatch, _FakeCurlResp(200, raw))

    resp = await oc._do_free_request_curl_cffi(_body(), {}, None, station=None)

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "hi"


@pytest.mark.asyncio
async def test_nonstream_200_truly_empty_still_502(monkeypatch):
    """200 VRAIMENT vide → toujours UpstreamError 502 (pas de faux succès)."""
    _patch_curl(monkeypatch, _FakeCurlResp(200, b""))

    with pytest.raises(oc.UpstreamError):
        await oc._do_free_request_curl_cffi(_body(), {}, None, station=None)
