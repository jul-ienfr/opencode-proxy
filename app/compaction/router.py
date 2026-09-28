"""app.compaction.router — point d'entrée unique du compactage (pur, DI).

Unifie les 3 portes (``/v1/messages``, ``/v1/chat/completions``, ``/v1/responses``)
et les 3 natures (résumé client, condense-and-retry serveur sur 400-overflow,
passthrough natif provider) sous un seul contrat :

  detect → route → plan → transport → truncate/retry

- ``detect`` : ``is_compaction_shape`` (officielle + marqueurs) OU
  ``is_native_compaction`` (provider natif : ``context_management`` Anthropic,
  ``compact_threshold`` / item ``compaction`` / ``/responses/compact`` OpenAI).
  Tout compactage détecté → exclusion cache + relais intact + log, quelle que
  soit la porte.
- ``route`` : résolution injectée (``route_fn``) → ``model_id`` ;
- ``plan`` : ``summarizer_plan`` — MÊME classe free/payante que la conversation,
  même endpoint, mêmes stations/VPN/geo (``forced_pool`` propagé) ;
- ``transport`` : ``summarizer_headers`` / ``summarizer_request`` — Bearer public
  en free, clé payante en paid, conversions chat↔responses selon l'endpoint ;
- natif provider : pas de résumé client — forward verbatim (l'item/bloc opaque
  est relayé tel quel, jamais interprété ni élagué).

Aucun import projet. Ne lève jamais (tout input inattendu → passthrough).
"""

from __future__ import annotations

from app.compaction.plan import summarizer_plan
from app.compaction.shapes import is_compaction_shape, is_native_compaction


def detect_compaction(body, *, min_chars_implicit=1000):
    """(is_compaction, is_native, kind) — kind: official|marker|native-*|none."""
    try:
        if not isinstance(body, dict):
            return False, False, "none"
        native_kind = None
        try:
            if bool(is_native_compaction(body)):
                if isinstance(body.get("context_management"), dict):
                    native_kind = "native-anthropic"
                elif "compact_threshold" in str(body):
                    native_kind = "native-responses-threshold"
                else:
                    native_kind = "native"
        except Exception:
            native_kind = None
        if native_kind:
            return True, True, native_kind
        try:
            if bool(is_compaction_shape(body, min_chars_implicit=min_chars_implicit)):
                # Distingue marqueur vs officielle pour le log (coût nul).
                try:
                    from app.compaction.shapes import _MARKERS

                    texts = []
                    msgs = body.get("messages")
                    if isinstance(msgs, list):
                        for m in msgs:
                            if isinstance(m, dict):
                                c = m.get("content")
                                texts.append(c if isinstance(c, str) else str(c))
                    low = "\n".join(texts).lower()
                    if any(mk in low for mk in _MARKERS):
                        return True, False, "marker"
                except Exception:
                    pass
                return True, False, "official"
        except Exception:
            pass
        return False, False, "none"
    except Exception:
        return False, False, "none"


def plan_for_conversation(
    model_id,
    *,
    endpoint=None,
    protocol=None,
    api=None,
    override=None,
    route_fn=None,
    model_config_fn=None,
    resolve_free_fn=None,
    free_endpoint_fn=None,
    default_target="",
    is_free_fn=None,
):
    """Délègue à ``summarizer_plan`` : (model, endpoint, protocol, api, is_free, seed)."""
    try:
        return summarizer_plan(
            model_id,
            override,
            endpoint=endpoint,
            protocol=protocol,
            api=api,
            route_fn=route_fn,
            model_config_fn=model_config_fn,
            resolve_free_fn=resolve_free_fn,
            free_endpoint_fn=free_endpoint_fn,
            default_target=default_target,
            is_free_fn=is_free_fn,
        )
    except Exception:
        return model_id, endpoint, protocol, api, False, None


__all__ = ["detect_compaction", "plan_for_conversation"]
