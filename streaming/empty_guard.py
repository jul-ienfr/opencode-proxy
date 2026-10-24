"""Garde de flux : une reponse 200 dont le PREMIER contenu est une erreur
devient un 5xx honnete (V5.1).

**Le probleme, mesure.** Un amont qui repond 200 puis ne produit aucun octet (ou
seulement une trame d'erreur) etait rendu au client en **HTTP 200**. Le client
lisait le statut comme un succes. Le proxy comptait au moins cinq emetteurs
d'erreur distincts sur ce chemin : corriger l'un revelait le suivant, chacun
etant protege par un `except Exception` qui transformait la levee en trame SSE.

**La reponse architecturale.** Ne plus chasser les sites. Ce middleware est un
point d'etranglement unique, place devant TOUTES les reponses : il retarde
l'envoi des en-tetes jusqu'au premier octet de corps et decide a ce moment-la.
  * premier octet = contenu normal  -> on relaie tel quel (aucune difference
    visible, un seul chunk de latence) ;
  * premier octet = trame d'erreur, ou flux vide -> on emet un **5xx** dans le
    protocole du client, et la trame d'erreur du 200 n'est jamais envoyee.

**Pourquoi c'est sur.** Le middleware ne touche jamais a un flux qui a commence
a livrer du contenu : des qu'un chunk non-erreur passe, il bascule en relais
transparent pour le reste de la reponse. Il ne reecrit pas non plus les statuts
non-200 (un 429, un 403 restent intacts).

**Pourquoi ca ne peut pas casser un client.** Le seul changement visible est
« 200 + corps d'erreur » -> « 5xx + corps d'erreur de meme forme ». Un client qui
savait lire l'erreur la lit encore ; un client qui ne lisait que le statut voit
enfin la verite.
"""

from __future__ import annotations

import json

__all__ = [
    "EmptyStreamGuardMiddleware",
    "looks_like_error_frame",
    "is_empty_stream_error_frame",
    "is_stream_content_type",
]


#: Marqueurs de premiere trame d'erreur, par protocole.
#: Anthropic : ``event: error`` puis ``data: {...}``.
#: OpenAI    : ``data: {"error": ...}``.
_ANTHROPIC_PREFIXES = (b"event: error", b"event:error")
_OPENAI_PREFIXES = (b'data: {"error"', b'data:{"error"', b'data: {"type":"error"', b'data:{"type":"error"')


def looks_like_error_frame(chunk: bytes | bytearray | memoryview | None) -> bool:
    """La premiere trame est-elle une erreur protocolaire ? (pur, testable)

    Volontairement strict : on ne cherche QUE les formes exactes de premiere
    trame d'erreur. Une reponse qui commence par un `message_start` ou un
    `delta` n'est jamais classee en erreur, meme si une erreur vient plus tard —
    auquel cas le flux a deja commence et le middleware ne doit pas intervenir.
    """
    if not chunk:
        return False
    try:
        head = bytes(chunk).lstrip()
    except Exception:
        return False
    if not head:
        return False
    return head.startswith(_ANTHROPIC_PREFIXES) or head.startswith(_OPENAI_PREFIXES)


#: Marqueurs d'un amont qui n'a RIEN produit. C'est la seule famille d'erreurs
#: que le garde convertit en 5xx.
#:
#: Le reste — un 400 « context too long », un 500 porteur d'un message utile —
#: doit continuer d'être relayé **tel quel** : le convertir en 502 générique
#: effacerait l'information dont le client a besoin. La distinction est le cœur
#: du correctif : on ne corrige pas « une erreur est arrivée », on corrige
#: « le client a reçu 200 alors que rien n'a été produit ».
_EMPTY_MARKERS = (
    b"produced no output",
    b"empty stream",
    b"empty_response",
    b"stream aborted",
)


def is_empty_stream_error_frame(chunk: bytes | bytearray | memoryview | None) -> bool:
    """Erreur de *flux vide* (et non une erreur amont porteuse d'information)."""
    if not looks_like_error_frame(chunk):
        return False
    try:
        low = bytes(chunk).lower()
    except Exception:
        return False
    return any(marker in low for marker in _EMPTY_MARKERS)


def is_stream_content_type(headers: list[tuple[bytes, bytes]] | None) -> bool:
    """La reponse est-elle un flux SSE ? (pur, testable)"""
    for name, value in headers or []:
        try:
            if name.lower() == b"content-type" and b"event-stream" in value.lower():
                return True
        except Exception:
            continue
    return False


def _error_payload(path: str) -> bytes:
    """Corps d'erreur dans le protocole du client (jamais une trace interne)."""
    message = "Upstream produced no output (empty stream)"
    if "/messages" in path or path.startswith("/anthropic"):
        body = {"type": "error", "error": {"type": "api_error", "message": message}}
    else:
        body = {"error": {"message": message, "type": "api_error"}}
    return json.dumps(body, ensure_ascii=False).encode()


class EmptyStreamGuardMiddleware:
    """Middleware ASGI pur : decide du statut au premier octet reel.

    ASGI pur (et non `BaseHTTPMiddleware`) parce qu'il faut voir les messages
    `http.response.body` un par un, sans les recopier ni les mettre en tampon
    au-dela du premier.
    """

    def __init__(self, app, on_empty=None):
        self.app = app
        #: Callback appelé quand le garde convertit une réponse en 5xx : l'hôte
        #: y branche son compteur. Au point d'étranglement le modèle n'est pas
        #: connu — un compteur grossier vaut mieux qu'un incident invisible une
        #: fois le symptôme corrigé.
        self.on_empty = on_empty

    def _note(self, reason: str) -> None:
        if self.on_empty is None:
            return
        try:
            self.on_empty(reason)
        except Exception:
            pass

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        state = {
            "start": None,  # message http.response.start retenu
            "guarded": False,  # on retarde les en-tetes
            "decided": False,  # un premier chunk a deja tranche
            "hijacked": False,  # on a deja repondu : ignorer la suite de l'app
        }

        async def _send_guarded_5xx() -> None:
            body = _error_payload(path)
            await send(
                {
                    "type": "http.response.start",
                    "status": 502,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})

        async def send_wrapper(message):
            # [V5.1] Apres avoir rendu notre 5xx, la reponse de l'application
            # continue d'arriver (trames d'erreur, [DONE], etc.). Les relayer
            # ferait un second envoi sur une reponse deja terminee — c'est
            # exactement l'assert de Starlette qu'on a vu en test. On les ignore.
            if state["hijacked"]:
                return
            mtype = message.get("type")

            if mtype == "http.response.start":
                status = int(message.get("status") or 200)
                headers = message.get("headers") or []
                # On ne garde que les 200 en flux : tout le reste (429, 403, 204,
                # non-stream) passe sans interception.
                if status == 200 and is_stream_content_type(headers):
                    state["start"] = message
                    state["guarded"] = True
                    return
                await send(message)
                return

            if mtype == "http.response.body" and state["guarded"] and not state["decided"]:
                body = message.get("body") or b""
                more = bool(message.get("more_body"))

                if not body:
                    if not more:
                        # Flux totalement vide : rien n'a jamais ete envoye.
                        state["decided"] = True
                        state["guarded"] = False
                        state["hijacked"] = True
                        self._note("empty_stream")
                        await _send_guarded_5xx()
                        return
                    # Corps vide mais flux en cours (keepalive) : on attend le
                    # premier octet REEL sans rien envoyer au client.
                    return

                if is_empty_stream_error_frame(body):
                    # Premier contenu = erreur : le 200 mentait.
                    state["decided"] = True
                    state["guarded"] = False
                    state["hijacked"] = True
                    self._note("error_frame")
                    await _send_guarded_5xx()
                    return

                # Contenu normal : on envoie enfin les en-tetes, puis on relaie.
                state["decided"] = True
                state["guarded"] = False
                start = state["start"]
                if start:
                    await send(start)
                await send(message)
                return

            if mtype == "http.response.body" and state["guarded"] and state["decided"]:
                await send(message)
                return

            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            # [V5.1] Une levée survenue PENDANT le flux, alors que rien n'a encore
            # été envoyé au client, est exactement le cas qu'on veut corriger.
            # Elle arrive en `BaseExceptionGroup` (anyio), donc `except Exception`
            # ne l'attrape pas — c'est pour ça que le handler applicatif ne la
            # voyait pas et que le client recevait un 200 avant ce middleware.
            if state["start"] is not None and not state["decided"]:
                state["decided"] = True
                state["guarded"] = False
                state["hijacked"] = True
                self._note("stream_raised")
                try:
                    await _send_guarded_5xx()
                except Exception:
                    raise
                return
            raise
        finally:
            # Si l'application s'est terminee sans jamais rien envoyer alors
            # qu'on retenait les en-tetes, le client n'a rien recu : c'est un
            # flux vide. On lui rend un 5xx plutot qu'un silence.
            if state["guarded"] and state["start"] is not None:
                state["guarded"] = False
                state["hijacked"] = True
                self._note("silent_end")
                try:
                    await _send_guarded_5xx()
                except Exception:
                    pass
