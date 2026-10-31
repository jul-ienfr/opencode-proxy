"""Tests unitaires du garde de flux (V5.1) — sans l'application complete.

Le middleware est un point d'etranglement unique : il retarde les en-tetes d'une
reponse 200 en flux jusqu'au premier octet reel, puis decide. Ces tests
l'exercent sur une application factice, donc sans charger les 20 000 lignes du
proxy — ce qui permet de verifier finement les cas limites.

Ce qui est verrouille ici, et pourquoi chacun compte :

* **erreur en premier** -> 5xx (le defaut d'origine : 200 + trame d'erreur) ;
* **flux vide** -> 5xx (rien n'a jamais ete envoye) ;
* **contenu normal** -> 200 inchange, et les octets arrivent intacts (aucune
  regression sur les flux qui marchent) ;
* **statut non-200** -> jamais touche (429, 403 doivent passer tels quels) ;
* **erreur APRES du contenu** -> jamais touche : un flux commence ne se coupe
  pas, meme si une erreur survient ensuite.
"""

import asyncio

from streaming.empty_guard import (
    EmptyStreamGuardMiddleware,
    is_stream_content_type,
    looks_like_error_frame,
)

SSE = [(b"content-type", b"text/event-stream")]


def _run(app, path="/v1/messages", scope_type="http"):
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    middleware = EmptyStreamGuardMiddleware(app, on_empty=lambda reason: sent.append({"noted": reason}))
    asyncio.run(middleware({"type": scope_type, "path": path}, receive, send))
    return sent


def _app(*messages):
    """Application factice qui envoie exactement ces messages ASGI."""

    async def app(scope, receive, send):
        for message in messages:
            await send(message)

    return app


def _start(status=200, headers=SSE):
    return {"type": "http.response.start", "status": status, "headers": headers}


def _body(payload, more=False):
    return {"type": "http.response.body", "body": payload, "more_body": more}


class TestDetection:
    def test_anthropic_error_frame(self):
        assert looks_like_error_frame(b'event: error\ndata: {"type":"error"}\n\n')

    def test_openai_error_frame(self):
        assert looks_like_error_frame(b'data: {"error":{"message":"x"}}\n\n')
        assert looks_like_error_frame(b'data:{"error":{"message":"x"}}\n\n')

    def test_content_is_not_an_error_frame(self):
        for payload in (
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b'data: {"choices":[{"delta":{"content":"bonjour"}}]}\n\n',
            b"",
            None,
        ):
            assert not looks_like_error_frame(payload), payload

    def test_stream_content_type(self):
        assert is_stream_content_type(SSE)
        assert not is_stream_content_type([(b"content-type", b"application/json")])
        assert not is_stream_content_type(None)


class TestGuard:
    def test_error_frame_first_becomes_5xx(self):
        empty = b'event: error\ndata: {"type":"error","error":{"message":"upstream produced no output"}}\n\n'
        sent = _run(_app(_start(), _body(empty)))
        start = [m for m in sent if m.get("type") == "http.response.start"][0]
        assert start["status"] == 502

    def test_rate_limit_error_frame_becomes_429_with_message(self):
        """Un quota épuisé reste un 429, jamais un 502 « proxy indisponible ».

        Mesuré le 2026-10-29 côté Hermes : `upstream_free_empty_response`
        (rate_limit_error) était réécrit en `502 Upstream produced no output`
        et le client concluait à une panne du proxy.
        """
        frame = (
            b'event: error\ndata: {"type":"error","error":{"type":"rate_limit_error",'
            b'"message":"upstream_free_empty_response: free model produced no output on all stations. Retry later."}}\n\n'
        )
        sent = _run(_app(_start(), _body(frame)))
        start = [m for m in sent if m.get("type") == "http.response.start"][0]
        assert start["status"] == 429, start
        body = b"".join(m.get("body") or b"" for m in sent if m.get("type") == "http.response.body")
        assert b"rate_limit_error" in body
        assert b"free model produced no output on all stations" in body
        assert b"Upstream produced no output (empty stream)" not in body

    def test_ping_before_rate_limit_frame_becomes_429(self):
        """Mesure Hermes 2026-10-29 : ping + erreur dans le MEME premier chunk.

        L'ancien garde voyait le prefixe ``event: ping``, le prenait pour du
        contenu, rendait 200, puis relayait l'erreur de quota. Le client
        Anthropic ne voyait donc jamais le 429.
        """
        frame = (
            b'event: ping\ndata: {"type": "ping"}\n\n'
            b'event: error\ndata: {"type":"error","error":{"type":"rate_limit_error",'
            b'"message":"Free quota exhausted on all VPN stations. Retry after 4000s."}}\n\n'
        )
        sent = _run(_app(_start(), _body(frame)))
        start = [m for m in sent if m.get("type") == "http.response.start"][0]
        assert start["status"] == 429, start
        body = b"".join(m.get("body") or b"" for m in sent if m.get("type") == "http.response.body")
        assert b"Free quota exhausted" in body
        assert b"event: ping" not in body, "le keepalive ne doit pas etre envoye avec le 429"

    def test_ping_split_across_chunks_then_error_becomes_429(self):
        """Le keepalive peut arriver seul, l'erreur dans le chunk suivant."""
        ping = b'event: ping\ndata: {"type": "ping"}\n\n'
        frame = (
            b'event: error\ndata: {"type":"error","error":{"type":"rate_limit_error",'
            b'"message":"Free quota exhausted. Retry later."}}\n\n'
        )
        sent = _run(_app(_start(), _body(ping, more=True), _body(frame, more=False)))
        start = [m for m in sent if m.get("type") == "http.response.start"][0]
        assert start["status"] == 429, start

    def test_ping_before_real_content_passes_through(self):
        """Un ping suivi de contenu reel reste un 200, octets intacts."""
        payload = (
            b'event: ping\ndata: {"type": "ping"}\n\n'
            b'event: message_start\ndata: {"type":"message_start","message":{"id":"m"}}\n\n'
        )
        sent = _run(_app(_start(), _body(payload)))
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts[0]["status"] == 200
        bodies = b"".join(m.get("body") or b"" for m in sent if m.get("type") == "http.response.body")
        assert bodies == payload

    def test_ping_only_then_eof_becomes_5xx(self):
        """Un flux qui ne contient que des keepalives n'a rien produit."""
        ping = b'event: ping\ndata: {"type": "ping"}\n\n'
        sent = _run(_app(_start(), _body(ping, more=False)))
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts and starts[0]["status"] == 502

    def test_ping_after_content_is_relayed(self):
        """Une fois le contenu commence, keepalives et erreurs passent tels quels."""
        first = b'event: message_start\ndata: {"type":"message_start"}\n\n'
        ping = b'event: ping\ndata: {"type": "ping"}\n\n'
        err = b'event: error\ndata: {"type":"error"}\n\n'
        sent = _run(_app(_start(), _body(first, more=True), _body(ping, more=True), _body(err, more=False)))
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts[0]["status"] == 200
        bodies = b"".join(m.get("body") or b"" for m in sent if m.get("type") == "http.response.body")
        assert ping in bodies and err in bodies

    def test_openai_rate_limit_frame_becomes_429(self):
        frame = (
            b'data: {"error":{"type":"rate_limit_error",'
            b'"message":"FreeUsageLimitError: free usage limit reached"}}\n\n'
        )
        sent = _run(_app(_start(), _body(frame)))
        start = [m for m in sent if m.get("type") == "http.response.start"][0]
        assert start["status"] == 429, start
        body = b"".join(m.get("body") or b"" for m in sent if m.get("type") == "http.response.body")
        assert b"FreeUsageLimitError" in body

    def test_empty_stream_becomes_5xx(self):
        sent = _run(_app(_start(), _body(b"")))
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts and starts[0]["status"] == 502

    def test_normal_content_passes_through_untouched(self):
        payload = b'event: message_start\ndata: {"type":"message_start"}\n\n'
        sent = _run(_app(_start(), _body(payload)))
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts[0]["status"] == 200
        bodies = [m.get("body") for m in sent if m.get("type") == "http.response.body"]
        assert payload in bodies

    def test_error_after_content_is_not_hijacked(self):
        """Un flux qui a livre du contenu ne se coupe jamais retroactivement."""
        first = b'event: message_start\ndata: {"type":"message_start"}\n\n'
        err = b'event: error\ndata: {"type":"error"}\n\n'
        sent = _run(_app(_start(), _body(first, more=True), _body(err, more=False)))
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts[0]["status"] == 200, "le statut ne doit pas changer apres du contenu"
        bodies = [m.get("body") for m in sent if m.get("type") == "http.response.body"]
        assert err in bodies, "la trame d'erreur doit etre relayee telle quelle"

    def test_non_200_is_never_touched(self):
        sent = _run(
            _app(
                _start(status=429, headers=[(b"content-type", b"application/json")]), _body(b'{"error":"rate limited"}')
            )
        )
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts[0]["status"] == 429

    def test_non_stream_200_is_never_touched(self):
        sent = _run(_app(_start(headers=[(b"content-type", b"application/json")]), _body(b'{"ok":true}')))
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts[0]["status"] == 200

    def test_incident_is_notified(self):
        empty = b'event: error\ndata: {"type":"error","error":{"message":"upstream produced no output"}}\n\n'
        sent = _run(_app(_start(), _body(empty)))
        assert any(m.get("noted") for m in sent), "l'incident doit etre compte"

    def test_websocket_scope_is_ignored(self):
        sent = _run(_app(_start(), _body(b"")), scope_type="websocket")
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts[0]["status"] == 200

    def test_a_real_upstream_error_is_relayed_untouched(self):
        """Distinction centrale : une erreur amont porteuse d'information reste.

        Un 400 "context too long" relaye en trame SSE doit continuer d'arriver
        tel quel : le convertir en 502 generique effacerait le message dont le
        client a besoin. V5.1 ne corrige pas "une erreur est arrivee", il corrige
        "le client a recu 200 alors que rien n'a ete produit".
        """
        frame = b'data: {"error":{"message":"context too long","type":"invalid_request_error"}}\n\n'
        sent = _run(_app(_start(), _body(frame)))
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts[0]["status"] == 200, "une vraie erreur amont ne doit pas devenir un 502"
        bodies = [m.get("body") for m in sent if m.get("type") == "http.response.body"]
        assert frame in bodies
