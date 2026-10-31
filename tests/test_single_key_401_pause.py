"""Régression 2026-10-29 — une clé unique morte doit être mise en pause.

Avant : le 401 n'était traité (pause + failover) que si `len(API_KEYS) > 1`.
Avec l'installation mono-clé réelle (`.env`), la clé morte n'était donc
jamais marquée : `_has_usable_paid_key()` restait vrai, chaque requête
rejouait le 401, et la garde « aucune clé utilisable → jambe gratuite » ne
s'activait jamais.

Après : un 401 pause la clé, même unique ; le prochain `_get_auth_headers`
lève `AllKeysPausedError` et les handlers basculent sur le free (ou rendent
un refus strict explicite, jamais un 401 amont répété).
"""

import pytest

import opencode as oc

K1 = "sk-ant-single-key-AAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture
def single_key(monkeypatch, tmp_path):
    monkeypatch.setattr(oc._KeyPauser, "_PAUSED_FILE", str(tmp_path / "paused_keys.yaml"))
    monkeypatch.setattr(oc, "API_KEYS", [{"api_key": K1, "alias": "single", "enabled": True}])
    monkeypatch.setattr(oc, "API_KEY", "")
    monkeypatch.setattr(oc, "_key_pauser", oc._KeyPauser())
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    oc._rebuild_key_cache()
    return oc


class _Resp401:
    status_code = 401
    headers = {"content-type": "application/json"}
    text = '{"error":{"message":"Invalid API key"}}'
    content = text.encode()

    def json(self):
        return {"error": {"message": "Invalid API key"}}


class _Client401:
    def __init__(self):
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        return _Resp401()


@pytest.mark.asyncio
async def test_single_key_401_is_paused_and_guard_closes(single_key, monkeypatch):
    client = _Client401()
    monkeypatch.setattr(oc, "_ensure_http_client", lambda: client)

    headers = oc._get_auth_headers("anthropic")
    resp, _ = await oc._do_request_with_retry("https://paid.invalid/v1/messages", {"model": "x"}, headers, "anthropic")

    assert resp.status_code == 401
    assert client.calls == 1
    assert oc._key_pauser.is_paused(K1), "la clé unique morte doit être mise en pause"
    assert oc._has_usable_paid_key() is False
    with pytest.raises(oc.AllKeysPausedError):
        oc._get_auth_headers("anthropic")


@pytest.mark.asyncio
async def test_single_key_401_stream_loop_pauses_without_failover(single_key):
    loop = oc._make_stream_retry_loop("anthropic")
    headers = oc._get_auth_headers("anthropic", entry={"api_key": K1})

    out_headers, retry = await loop(headers, 401, 0)

    assert retry is False, "aucune clé alternative : pas de retry même-clé"
    assert out_headers is headers
    assert oc._key_pauser.is_paused(K1)
    assert oc._has_usable_paid_key() is False


class _Resp429:
    status_code = 429
    headers = {"content-type": "application/json", "retry-after": "2"}
    text = '{"error":{"message":"quota exceeded"}}'
    content = text.encode()

    def json(self):
        return {"error": {"message": "quota exceeded"}}


class _Client429:
    def __init__(self):
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        return _Resp429()


@pytest.mark.asyncio
async def test_single_key_429_is_paused_without_same_key_retry(single_key, monkeypatch):
    """[audit 2026-10-30] Un 429 quota ?puise la cl? m?me unique. L'ancien
    code ne pausait que si len(API_KEYS) > 1, puis rejouait la m?me cl?."""
    client = _Client429()
    monkeypatch.setattr(oc, "_ensure_http_client", lambda: client)

    async def _fake_pause(_key):
        oc._key_pauser.pause_key(_key, 60, "429 test")

    monkeypatch.setattr(oc, "_pause_key_for_quota_reset", _fake_pause)

    headers = oc._get_auth_headers("anthropic")
    resp, _ = await oc._do_request_with_retry(
        "https://paid.invalid/v1/messages", {"model": "x"}, headers, "anthropic"
    )

    assert resp.status_code == 429
    assert client.calls == 1, "la m?me cl? paus?e ne doit pas ?tre rejou?e"
    assert oc._key_pauser.is_paused(K1)
    assert oc._has_usable_paid_key() is False


@pytest.mark.asyncio
async def test_single_key_429_stream_loop_pauses(single_key, monkeypatch):
    async def _fake_pause(_key):
        oc._key_pauser.pause_key(_key, 60, "429 test")

    monkeypatch.setattr(oc, "_pause_key_for_quota_reset", _fake_pause)
    loop = oc._make_stream_retry_loop("anthropic")
    headers = oc._get_auth_headers("anthropic", entry={"api_key": K1})

    out_headers, retry = await loop(headers, 429, 0)

    assert retry is False
    assert out_headers is headers
    assert oc._key_pauser.is_paused(K1)
