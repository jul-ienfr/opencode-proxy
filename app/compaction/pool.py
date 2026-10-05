"""app.compaction.pool — jambe free du résumeur via la machinerie pool (DI pure).

Extraction P1-6 depuis ``opencode.py`` (``_compaction_free_pool_request`` +
``_compaction_summarizer_request``). AUCUN import projet :

* la machinerie free (``try_free_fn`` ≈ ``_try_free_model_first``), le
  transport payant (``do_paid_fn``) et le direct free (``do_free_direct_fn``)
  sont injectés ;
* les classes d'erreur sont injectées (``upstream_error_cls``,
  ``refusal_types``) — ce module ne dépend ni de ``core.errors`` ni de
  l'hôte ;
* l'envoi wire reste délégué à ``transport.summarizer_request`` (injecté
  en ``transport_request_fn`` pour les tests).

L'hôte (``opencode.py``) conserve des wrappers fins
``_compaction_free_pool_request`` / ``_compaction_summarizer_request``
(compat ``oc.*`` pour les tests) qui injectent les dépendances réelles.
"""

from __future__ import annotations


async def free_pool_request(
    body,
    headers,
    protocol,
    seed,
    *,
    forced_pool=None,
    req_id=None,
    try_free_fn,
    refusal_types,
    upstream_error_cls,
):
    """Résumeur free via la machinerie pool (stations/VPN/hedge/cooldown), fail-open.

    Même jambe que la conversation : ``seed`` (nom payant d'origine → même
    résolution free) ou, si ``seed`` est vide, le modèle du body lui-même.
    Budget épuisé / refus free (``None`` / ``refusal_types``) → lève
    ``upstream_error_cls`` 502 (l'appelant convertit en relais intact).
    Ne lève que ``upstream_error_cls``.
    """
    try:
        _model_key = seed
        if not _model_key and isinstance(body, dict):
            try:
                _model_key = body.get("model") or ""
            except Exception:
                _model_key = ""
        if not _model_key:
            raise upstream_error_cls("free summarizer pool: no model key", status_code=502)
        try:
            result = await try_free_fn(
                body if isinstance(body, dict) else {},
                dict(headers) if isinstance(headers, dict) else {},
                protocol,
                _model_key,
                forced_pool=forced_pool,
                req_id=req_id,
            )
        except refusal_types as e:
            raise upstream_error_cls(f"free summarizer pool refused: {e}", status_code=502, original=e) from e
        if result is None:
            raise upstream_error_cls("free summarizer pool exhausted (no paid fallback)", status_code=502)
        try:
            resp, resp_headers, _actual_model, _actual_ip = result
        except Exception as e:
            raise upstream_error_cls(
                f"free summarizer pool bad result: {e}", status_code=502, original=e
            ) from e
        return resp, resp_headers
    except Exception as e:
        if isinstance(e, upstream_error_cls):
            raise
        raise upstream_error_cls(
            f"free summarizer pool failed: {type(e).__name__}: {e}", status_code=502, original=e
        ) from e


async def summarizer_dispatch(
    is_free: bool,
    endpoint,
    body,
    headers,
    protocol,
    *,
    seed=None,
    forced_pool=None,
    req_id=None,
    free_pool_fn,
    do_paid_fn,
    do_free_direct_fn,
    transport_request_fn,
    chat_to_responses_fn,
    normalize_response_fn,
):
    """do_request_fn du résumeur : jambe free (pool) ou payante.

    ``is_free`` → ``free_pool_fn`` (même machinerie que la conversation) ;
    sinon transport payant. Jamais de bascule silencieuse (échec pool →
    fail-open, pas de payant).
    """

    async def _pool_free(_endpoint_ignored, wire_body, wire_headers):
        return await free_pool_fn(
            wire_body,
            wire_headers,
            protocol,
            seed,
            forced_pool=forced_pool,
            req_id=req_id,
        )

    if is_free:
        return await transport_request_fn(
            True,
            endpoint,
            body,
            headers,
            protocol,
            do_free_fn=_pool_free,
            do_paid_fn=do_paid_fn,
            chat_to_responses_fn=chat_to_responses_fn,
            normalize_response_fn=normalize_response_fn,
        )
    return await transport_request_fn(
        False,
        endpoint,
        body,
        headers,
        protocol,
        do_free_fn=do_free_direct_fn,
        do_paid_fn=do_paid_fn,
        chat_to_responses_fn=chat_to_responses_fn,
        normalize_response_fn=normalize_response_fn,
    )


__all__ = ["free_pool_request", "summarizer_dispatch"]
