"""app.compaction.transport — transport unifié free/payant du compactage (DI).

Un seul contrat pour tous les compactages (résumé client, condense-and-retry
serveur, passthrough natif) : **la même voie que la conversation**.

- classe free → jambe free : ``Bearer public`` (jamais de clé payante),
  grille wire officielle (shim tools + ``tool_choice:auto`` + ordre clés +
  ``stream:true`` forcé collecté), endpoint free résolu par le plan,
  stations/VPN/hedge/cooldown via la machinerie free injectée ;
- classe payante → jambe payante : clés du protocole, tunnel imposé si
  ``geo.require_vpn`` (via ``open_via_pool_fn`` injecté), sinon direct ;
- item ``compaction`` opaque (Responses) et bloc ``compaction`` (Anthropic) :
  relayés tels quels, jamais interprétés ;
- tout échec → ``None`` / réponse d'origine (fail-open), jamais d'exception.

Aucun import projet : les I/O (``do_free_fn``, ``do_paid_fn``,
``open_via_pool_fn``, ``official_headers_fn``, ``paid_headers_fn``,
conversions chat↔responses) sont injectés par le délégué ``opencode.py``.
"""

from __future__ import annotations


def summarizer_headers(is_free, protocol, endpoint="", *, official_headers_fn=None, paid_headers_fn=None):
    """Headers du résumeur : Bearer public (free) ou clé payante (paid)."""
    try:
        if is_free:
            if callable(official_headers_fn):
                try:
                    return official_headers_fn(endpoint or "")
                except Exception:
                    return {}
            return {}
        if callable(paid_headers_fn):
            return paid_headers_fn(protocol)
        return {}
    except Exception:
        return {}


async def summarizer_request(
    is_free,
    endpoint,
    body,
    headers,
    protocol,
    *,
    do_free_fn=None,
    do_paid_fn=None,
    chat_to_responses_fn=None,
    normalize_response_fn=None,
):
    """Envoi du résumeur : jambe free-only ou payante selon ``is_free``.

    Le corps résumeur est en forme **chat** ; sur endpoint ``/responses``
    (muse/spark) il est converti à l'aller et la réponse reconvertie en
    ``choices`` pour que ``_extract_text`` trouve le texte.
    """
    try:
        if not is_free:
            if not callable(do_paid_fn):
                raise RuntimeError("no paid transport injected")
            return await do_paid_fn(endpoint, body, headers, protocol)
        _wire = body
        if "/responses" in (endpoint or "") and isinstance(body, dict) and "messages" in body:
            if callable(chat_to_responses_fn):
                try:
                    _wire = chat_to_responses_fn(body)
                except Exception:
                    _wire = body
        if not callable(do_free_fn):
            raise RuntimeError("no free transport injected")
        _resp, _hdr = await do_free_fn(endpoint, _wire, headers)
        if callable(normalize_response_fn):
            try:
                _resp = await normalize_response_fn(_resp, endpoint, _wire)
            except Exception:
                pass
        return _resp, _hdr
    except Exception as exc:
        raise exc


async def normalize_free_responses(resp, endpoint, body, *, json_loads_fn=None, responses_to_chat_fn=None, response_cls=None):
    """Ramène une réponse free ``/responses`` en forme ``choices`` lisible.

    ``/responses`` rend ``output[]`` (blocs ``output_text``) que l'extracteur
    chat/anthropic ne sait pas lire ; on le convertit en
    ``choices[0].message.content``. Toute autre forme est rendue telle quelle.
    Jamais d'exception : échec → réponse d'origine.
    """
    try:
        if getattr(resp, "status_code", 0) != 200 or "/responses" not in (endpoint or ""):
            return resp
        _loads = json_loads_fn or (lambda b: __import__("json").loads(b))
        try:
            _data = _loads(resp.content)
        except Exception:
            return resp
        if not isinstance(_data, dict) or "output" not in _data:
            return resp
        _model = str(body.get("model", "")) if isinstance(body, dict) else ""
        if not callable(responses_to_chat_fn):
            return resp
        try:
            _chat = responses_to_chat_fn(_data, _model)
        except Exception:
            return resp
        if response_cls is None:
            return resp
        try:
            import json as _json

            return response_cls(
                200,
                headers={"content-type": "application/json"},
                content=_json.dumps(_chat).encode("utf-8"),
            )
        except Exception:
            return resp
    except Exception:
        return resp


def should_use_tunnel(*, is_free, geo_force_tunnel=False, geo_require_vpn=False, vpn_on=False):
    """True si l'envoi doit passer par le tunnel pool (parité geo/VPN).

    - classe free avec VPN actif → tunnel (stations) ;
    - classe payante avec ``geo.require_vpn`` → tunnel imposé (Axe A) ;
    - ``geo_force_tunnel`` explicite → tunnel.
    """
    try:
        if bool(geo_force_tunnel):
            return True
        if is_free and bool(vpn_on):
            return True
        if (not is_free) and bool(geo_require_vpn):
            return True
        return False
    except Exception:
        return False


__all__ = [
    "summarizer_headers",
    "summarizer_request",
    "normalize_free_responses",
    "should_use_tunnel",
]
