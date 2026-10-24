"""V5.1 — preuve comportementale : un amont vide ne sort plus en 200.

Les tests de `test_empty_stream_http_status.py` verifient la *structure* du
correctif (l'exception, la condition, le handler). Ce fichier-ci verifie le
**comportement** : une requete reelle traverse l'application ASGI, l'amont rend
200 avec un corps vide, et le client doit voir un **5xx** — pas un 200.

C'est la difference que cette session a payee trois fois : un correctif dont la
structure est juste peut echouer a l'execution. Ce test existe pour que V5.1 ne
soit pas dans ce cas.
"""

import asyncio

import pytest

import opencode as oc

httpx = pytest.importorskip("httpx")


class _EmptyUpstream:
    """Amont qui repond 200 et ne produit AUCUN octet (le defaut V5.1)."""

    status_code = 200
    headers = {"content-type": "text/event-stream"}

    async def aiter_bytes(self):
        if False:  # pragma: no cover - generatore vide par construction
            yield b""

    async def aiter_lines(self):
        if False:  # pragma: no cover
            yield ""


class _Ctx:
    """Remplace le context manager `_open_free_stream(...)`."""

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _Pool:
    enabled = True
    active_station = None
    _stations = []

    def _best_station(self):
        return None

    def update_config(self, *_a, **_k):
        return None


def _client():
    transport = httpx.ASGITransport(app=oc.app)
    return httpx.AsyncClient(transport=transport, base_url="http://proxy.test", timeout=30.0)


def _anthropic_body(model="big-pickle"):
    return {
        "model": model,
        "stream": True,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "dis bonjour"}],
    }


@pytest.mark.asyncio
# V5.1 corrige (middleware) : le test doit maintenant PASSER.
# "V5.1 NON CLOS : ce test reproduit le defaut et doit rester ROUGE jusqu'a sa correction. Ce qui a ete etabli : le chemin /v1/messages d'un modele a protocole openai passe par CINQ emetteurs d'erreur distincts, et corriger l'un revele le suivant (produit no output -> stream aborted -> upstream_free_empty_response -> ...). Les emetteurs connus : 13192, 14793, 14805, 14600/14651 (free), 16632. Prochain pas : un point d'etranglement unique (buffer du premier chunk avant d'envoyer les en-tetes) plutot qu'une chasse site par site.")
async def test_empty_upstream_yields_a_5xx_not_a_200(monkeypatch):
    """Le coeur de V5.1, verifie en execution."""
    monkeypatch.setattr(oc, "_free_ip_pool", _Pool())
    monkeypatch.setattr(oc, "_free_proxy_mode", lambda: "direct")
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_open_free_stream", lambda *a, **k: _Ctx(_EmptyUpstream()))
    monkeypatch.setattr(oc, "_open_via_pool", lambda *a, **k: _Ctx(_EmptyUpstream()))

    async with _client() as client:
        resp = await client.post("/v1/messages", json=_anthropic_body())

    assert resp.status_code != 200, (
        "V5.1 : un flux amont vide ne doit JAMAIS sortir en 200 "
        f"(recu {resp.status_code} : {resp.text[:200]})"
    )
    assert 500 <= resp.status_code < 600, resp.status_code


@pytest.mark.asyncio
# V5.1 corrige (middleware) : le test doit maintenant PASSER.
# "V5.1 NON CLOS : ce test reproduit le defaut et doit rester ROUGE jusqu'a sa correction. Ce qui a ete etabli : le chemin /v1/messages d'un modele a protocole openai passe par CINQ emetteurs d'erreur distincts, et corriger l'un revele le suivant (produit no output -> stream aborted -> upstream_free_empty_response -> ...). Les emetteurs connus : 13192, 14793, 14805, 14600/14651 (free), 16632. Prochain pas : un point d'etranglement unique (buffer du premier chunk avant d'envoyer les en-tetes) plutot qu'une chasse site par site.")
async def test_error_body_is_protocol_shaped(monkeypatch):
    """Le client Anthropic doit recevoir une erreur dans SON protocole."""
    monkeypatch.setattr(oc, "_free_ip_pool", _Pool())
    monkeypatch.setattr(oc, "_free_proxy_mode", lambda: "direct")
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_open_free_stream", lambda *a, **k: _Ctx(_EmptyUpstream()))
    monkeypatch.setattr(oc, "_open_via_pool", lambda *a, **k: _Ctx(_EmptyUpstream()))

    async with _client() as client:
        resp = await client.post("/v1/messages", json=_anthropic_body())

    payload = resp.json()
    assert "error" in payload, payload
    assert payload.get("type") == "error" or "message" in payload["error"], payload


@pytest.mark.asyncio
# V5.1 corrige (middleware) : le test doit maintenant PASSER.
# "V5.1 NON CLOS : ce test reproduit le defaut et doit rester ROUGE jusqu'a sa correction. Ce qui a ete etabli : le chemin /v1/messages d'un modele a protocole openai passe par CINQ emetteurs d'erreur distincts, et corriger l'un revele le suivant (produit no output -> stream aborted -> upstream_free_empty_response -> ...). Les emetteurs connus : 13192, 14793, 14805, 14600/14651 (free), 16632. Prochain pas : un point d'etranglement unique (buffer du premier chunk avant d'envoyer les en-tetes) plutot qu'une chasse site par site.")
async def test_incident_is_counted_even_when_the_status_is_honest(monkeypatch):
    """Corriger le symptome ne doit pas rendre l'incident invisible."""
    oc._EMPTY_STREAM_INCIDENTS.clear()
    monkeypatch.setattr(oc, "_free_ip_pool", _Pool())
    monkeypatch.setattr(oc, "_free_proxy_mode", lambda: "direct")
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_open_free_stream", lambda *a, **k: _Ctx(_EmptyUpstream()))
    monkeypatch.setattr(oc, "_open_via_pool", lambda *a, **k: _Ctx(_EmptyUpstream()))

    async with _client() as client:
        await client.post("/v1/messages", json=_anthropic_body())

    assert oc._EMPTY_STREAM_INCIDENTS, "l'incident doit rester compte"
