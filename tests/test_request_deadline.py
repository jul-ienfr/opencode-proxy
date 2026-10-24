"""V6.5.3 — budget de temps total : abandon propre, message explicite.

**Le defaut vise.** Une requete peut rester suspendue indefiniment quand l'amont
accepte la connexion puis se tait. Les timeouts existants bornent des *etapes*
(TTFB, inactivite SSE), pas la duree de vie totale. Le client n'obtient alors ni
statut, ni message, ni fin.

**Ce que ces tests verifient, en execution et non par lecture :**
  * un depassement avant tout envoi -> **504** avec un message qui nomme le budget ;
  * un depassement apres envoi des en-tetes -> une derniere trame `error` SSE, pas
    de silence (on ne peut plus changer le statut, mais on peut dire pourquoi) ;
  * chemin exempte -> jamais borne (le tableau de bord et `/health` doivent
    repondre meme quand tout le reste est bloque) ;
  * budget `0` -> desactive explicitement ;
  * une requete rapide -> **inchangee**.

`min_budget` est abaisse dans ces tests pour ne pas attendre cinq secondes ; en
production la valeur par defaut protege d'un budget absurde.
"""

import asyncio
import json

import pytest

from streaming.deadline import RequestDeadlineMiddleware, is_exempt_path, resolve_budget

SHORT_MIN = 0.05


def _run(app, path="/v1/messages", budget=0.15, min_budget=SHORT_MIN):
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    middleware = RequestDeadlineMiddleware(
        app, budget_getter=lambda: budget, min_budget=min_budget
    )
    asyncio.run(middleware({"type": "http", "path": path}, receive, send))
    return sent


def _start(status=200, headers=None):
    return {
        "type": "http.response.start",
        "status": status,
        "headers": headers or [(b"content-type", b"text/event-stream")],
    }


def _body(payload=b"", more=False):
    return {"type": "http.response.body", "body": payload, "more_body": more}


class TestExemption:
    @pytest.mark.parametrize("path", ["/health", "/api/vpn-status", "/static/app.js", "/dashboard", "/ws/x"])
    def test_dashboard_and_health_are_exempt(self, path):
        assert is_exempt_path(path)

    @pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions", "/v1/responses", "/"])
    def test_model_paths_are_bounded(self, path):
        assert not is_exempt_path(path)

    def test_an_exempt_path_is_never_aborted(self):
        async def slow(scope, receive, send):
            await asyncio.sleep(0.3)
            await send(_start(200, [(b"content-type", b"application/json")]))
            await send(_body(b'{"ok":true}'))

        sent = _run(slow, path="/health", budget=0.05)
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts and starts[0]["status"] == 200


class TestBudgetResolution:
    def test_zero_disables_the_budget(self):
        assert resolve_budget(0) == 0.0
        assert resolve_budget(-3) == 0.0
        assert resolve_budget("0") == 0.0

    def test_missing_value_uses_the_default(self):
        assert resolve_budget(None, 900) == 900.0

    def test_nonsense_value_uses_the_default(self):
        assert resolve_budget("beaucoup", 900) == 900.0

    def test_tiny_value_is_raised_to_the_floor(self):
        assert resolve_budget(0.001) >= 5.0

    def test_huge_value_is_capped(self):
        assert resolve_budget(10**9) <= 86_400.0


class TestAbortBeforeAnyOutput:
    def test_a_silent_upstream_yields_a_504(self):
        async def hang(scope, receive, send):
            await asyncio.sleep(5)

        sent = _run(hang, budget=0.1)
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts, "aucune reponse envoyée : le client attendrait encore"
        assert starts[0]["status"] == 504

    def test_the_message_names_the_budget(self):
        async def hang(scope, receive, send):
            await asyncio.sleep(5)

        sent = _run(hang, budget=0.1)
        body = b"".join(m.get("body") or b"" for m in sent if m.get("type") == "http.response.body")
        payload = json.loads(body)
        message = payload["error"]["message"]
        assert "budget" in message.lower()
        assert "0s" in message or "0.1" in message or "budget" in message

    def test_the_anthropic_shape_is_respected(self):
        async def hang(scope, receive, send):
            await asyncio.sleep(5)

        sent = _run(hang, path="/v1/messages", budget=0.1)
        body = b"".join(m.get("body") or b"" for m in sent if m.get("type") == "http.response.body")
        assert json.loads(body)["type"] == "error"


class TestAbortAfterOutput:
    def test_a_started_stream_gets_a_final_error_frame(self):
        """Le statut est deja parti : le minimum honnete est de dire pourquoi."""

        async def half_then_hang(scope, receive, send):
            await send(_start())
            await send(_body(b'event: message_start\ndata: {}\n\n', more=True))
            await asyncio.sleep(5)

        sent = _run(half_then_hang, budget=0.1)
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts[0]["status"] == 200, "on ne reecrit pas un statut deja emis"
        frames = [m.get("body") or b"" for m in sent if m.get("type") == "http.response.body"]
        assert any(b"event: error" in f for f in frames), f"aucune trame d'erreur : {frames}"

    def test_a_finished_response_is_not_touched(self):
        async def quick(scope, receive, send):
            await send(_start(200, [(b"content-type", b"application/json")]))
            await send(_body(b'{"ok":true}', more=False))
            await asyncio.sleep(0.3)  # depasse le budget, mais tout est deja envoye

        sent = _run(quick, budget=0.05)
        bodies = [m.get("body") for m in sent if m.get("type") == "http.response.body"]
        assert bodies == [b'{"ok":true}']


class TestNormalPath:
    def test_a_fast_request_is_unchanged(self):
        async def quick(scope, receive, send):
            await send(_start(200, [(b"content-type", b"application/json")]))
            await send(_body(b'{"ok":true}'))

        sent = _run(quick, budget=5.0)
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts[0]["status"] == 200
        bodies = [m.get("body") for m in sent if m.get("type") == "http.response.body"]
        assert bodies == [b'{"ok":true}']

    def test_budget_zero_disables_the_guard(self):
        async def slow(scope, receive, send):
            await asyncio.sleep(0.2)
            await send(_start(200, [(b"content-type", b"application/json")]))
            await send(_body(b'{"ok":true}'))

        sent = _run(slow, budget=0)
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts and starts[0]["status"] == 200
