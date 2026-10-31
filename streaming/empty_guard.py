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
non-200 (un 429, un 403 restent intacts). Une trame d'erreur **rate-limit/quota**
est convertie en 429 avec son message d'origine, pas en 502 generique : un
quota epuise n'est pas une panne du proxy.

**Pourquoi ca ne peut pas casser un client.** Le seul changement visible est
« 200 + corps d'erreur » -> « 5xx + corps d'erreur de meme forme ». Un client qui
savait lire l'erreur la lit encore ; un client qui ne lisait que le statut voit
enfin la verite.

**Keepalive != contenu (mesure 2026-10-29, Hermes).** La pompe SSE emet un
`event: ping` apres 15 s sans octet amont. Si le premier chunk recu par ce garde
contient ce ping PUIS la trame d'erreur (cas reel : quota free atteint apres
l'attente), l'ancien test « premier octet = ping, donc contenu » rendait un 200
avec l'erreur a l'interieur. Le client Anthropic voyait un flux commence puis
une erreur, jamais un 429. Desormais les keepalives sont retenus : seul un
premier *contenu* reel fait basculer le statut ; un keepalive suivi d'une erreur
reste un 429.
"""

from __future__ import annotations

import json

__all__ = [
    "EmptyStreamGuardMiddleware",
    "looks_like_error_frame",
    "is_empty_stream_error_frame",
    "is_rate_limit_error_frame",
    "error_frame_message",
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

#: Une trame d'erreur qui porte un VRAI refus de quota doit rester un 429,
#: jamais devenir le 502 générique « empty stream ». Mesuré le 2026-10-29 :
#: `upstream_free_empty_response` (429 rate_limit_error) était réécrit en
#: `502 Upstream produced no output` côté client Hermes, qui concluait
#: « proxy temporairement indisponible » au lieu de « quota gratuit épuisé ».
_RATE_LIMIT_MARKERS = (
    b"rate_limit_error",
    b"rate-limited",
    b"rate limited",
    b"freeusagelimiterror",
    b"free usage limit",
)


def is_empty_stream_error_frame(chunk: bytes | bytearray | memoryview | None) -> bool:
    """Erreur de *flux vide* (et non une erreur amont porteuse d'information)."""
    if not looks_like_error_frame(chunk):
        return False
    if chunk is None:
        return False  # bytes(None) lèverait → même verdict que le except
    try:
        low = bytes(chunk).lower()
    except Exception:
        return False
    return any(marker in low for marker in _EMPTY_MARKERS)


def is_rate_limit_error_frame(chunk: bytes | bytearray | memoryview | None) -> bool:
    """La trame d'erreur est-elle un refus de quota/rate-limit (→ 429) ?"""
    if not looks_like_error_frame(chunk):
        return False
    try:
        low = bytes(chunk).lower()
    except Exception:
        return False
    return any(marker in low for marker in _RATE_LIMIT_MARKERS)


def error_frame_message(chunk: bytes | bytearray | memoryview | None) -> str:
    """Message porté par la première trame d'erreur SSE (best-effort).

    Un meme chunk reseau peut contenir un keepalive AVANT l'erreur
    (``event: ping`` + ``event: error``) : on avance donc d'objet JSON en objet
    JSON au lieu de ne lire que le premier ``{``. Mesure Hermes 2026-10-29 :
    le premier objet etait le ping, le message de quota etait perdu.
    """
    try:
        text = bytes(chunk).decode("utf-8", "replace")
    except Exception:
        return ""
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        start = text.find("{", pos)
        if start < 0:
            return ""
        try:
            data, end = decoder.raw_decode(text[start:])
        except Exception:
            pos = start + 1
            continue
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(data.get("message"), str):
                return str(data["message"])
        pos = start + max(1, end)


def is_stream_content_type(headers: list[tuple[bytes, bytes]] | None) -> bool:
    """La reponse est-elle un flux SSE ? (pur, testable)"""
    for name, value in headers or []:
        try:
            if name.lower() == b"content-type" and b"event-stream" in value.lower():
                return True
        except Exception:
            continue
    return False


def _split_complete_sse_events(raw: bytes) -> tuple[list[bytes], bytes]:
    """Separe les blocs SSE complets du reliquat (frontiere ``\\n\\n``).

    Un bloc est rendu avec son separateur : le remettre bout a bout restitue
    exactement les octets d'origine, ce qui permet de relayer sans reecriture.
    """
    events: list[bytes] = []
    rest = raw
    while True:
        lf = rest.find(b"\n\n")
        crlf = rest.find(b"\r\n\r\n")
        if crlf != -1 and (lf == -1 or crlf < lf):
            end, sep = crlf, 4
        elif lf != -1:
            end, sep = lf, 2
        else:
            break
        events.append(rest[: end + sep])
        rest = rest[end + sep :]
    return events, rest


def _event_first_line(event: bytes) -> bytes:
    return event.lstrip().split(b"\n", 1)[0].strip()


def _is_keepalive_event(event: bytes) -> bool:
    """Commentaire SSE, ``retry:`` ou ping Anthropic : pas du contenu."""
    first = _event_first_line(event)
    if not first:
        return True
    if first.startswith((b":", b"retry:")):
        return True
    if first in (b"event: ping", b"event:ping"):
        return True
    if first.startswith(b"data:"):
        payload = first[5:].strip().lower()
        if payload in (b'{"type":"ping"}', b'{"type": "ping"}'):
            return True
    return False


def _is_done_event(event: bytes) -> bool:
    return _event_first_line(event) in (b"data: [DONE]", b"data:[DONE]")


def _first_meaningful_event_kind(raw: bytes) -> str | None:
    """Nature du premier evenement SSE *complet* non-keepalive.

    ``content``     : le flux a reellement commence -> 200 (relais).
    ``empty``       : premiere trame = erreur de flux vide -> 502.
    ``rate_limit``  : premiere trame = erreur de quota -> 429.
    ``None``        : rien de complet encore, ou uniquement des keepalives.

    Une erreur amont *porteuse d'information* (400 « context too long », etc.)
    n'est pas convertie : elle est classee ``content`` et relayee telle quelle,
    contrat V5.1 (on ne masque pas un message dont le client a besoin).

    Mesure Hermes 2026-10-29 : le premier chunk etait
    ``event: ping`` + ``event: error`` dans le MEME bloc reseau. L'ancien test
    ne regardait que le prefixe du chunk, voyait ``event: ping`` et rendait 200.
    """
    events, _ = _split_complete_sse_events(raw)
    for event in events:
        if _is_keepalive_event(event) or _is_done_event(event):
            continue
        if looks_like_error_frame(event):
            if is_rate_limit_error_frame(event):
                return "rate_limit"
            if is_empty_stream_error_frame(event):
                return "empty"
            # Erreur amont actionnable : on la relaie, on ne la masque pas.
            return "content"
        return "content"
    return None


def _error_payload(path: str, message: str | None = None, error_type: str | None = None) -> bytes:
    """Corps d'erreur dans le protocole du client (jamais une trace interne)."""
    message = message or "Upstream produced no output (empty stream)"
    if "/messages" in path or path.startswith("/anthropic"):
        body = {"type": "error", "error": {"type": error_type or "api_error", "message": message}}
    else:
        body = {"error": {"message": message, "type": error_type or "api_error"}}
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
            "decided": False,  # on a tranche (contenu ou erreur)
            "hijacked": False,  # on a deja repondu : ignorer la suite de l'app
            "raw": b"",  # octets retenus tant que le premier contenu n'a pas tranche
        }

        async def _send_guarded_error(status: int, body: bytes) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": int(status),
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})

        async def _send_guarded_5xx() -> None:
            await _send_guarded_error(502, _error_payload(path))

        async def _send_guarded_rate_limit(frame: bytes) -> None:
            # Un quota/rate-limit est un 429, pas une panne proxy. On conserve
            # le message amont (actionnable) au lieu du 502 générique.
            self._note("rate_limit_error_frame")
            message = error_frame_message(frame) or "Free usage limit reached. Retry later."
            await _send_guarded_error(
                429, _error_payload(path, message=message, error_type="rate_limit_error")
            )

        async def _decide_error(kind: str) -> None:
            state["decided"] = True
            state["guarded"] = False
            state["hijacked"] = True
            if kind == "rate_limit":
                await _send_guarded_rate_limit(state["raw"])
            else:
                self._note("error_frame")
                await _send_guarded_5xx()

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
                if body:
                    state["raw"] += body

                kind = _first_meaningful_event_kind(state["raw"])
                if kind in ("empty", "rate_limit"):
                    await _decide_error(kind)
                    return

                if kind == "content":
                    # Contenu normal : on envoie enfin les en-tetes, puis on
                    # relaie TOUT ce qui etait retenu (keepalives compris).
                    state["decided"] = True
                    state["guarded"] = False
                    start = state["start"]
                    if start:
                        await send(start)
                    await send({**message, "body": state["raw"]})
                    state["raw"] = b""
                    return

                # Aucun evenement complet, ou uniquement des keepalives : rien
                # n'a ete envoye au client, le statut reste libre. On attend.
                if not more:
                    # Fin de flux sans le moindre contenu : c'est un flux vide,
                    # meme si des pings ont ete emis.
                    state["decided"] = True
                    state["guarded"] = False
                    state["hijacked"] = True
                    self._note("empty_stream")
                    await _send_guarded_5xx()
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
