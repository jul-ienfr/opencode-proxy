"""app.compaction.classify — prédicat unique de classe free/payante (pur).

Extrait de ``opencode._has_free_leg`` / ``_compaction_is_free_class`` (déplacement
pur, même sémantique) : ce module ne connaît ni ``opencode`` ni ``config`` —
tout l'état est injecté par l'appelant (délégué ``opencode.py`` qui lit SES
globales À L'APPEL).

Règle unique :
- id déjà free (``-free`` / pool découvert, via ``is_free_route_fn``) → free ;
- nom payant mappé dans ``FREE_MODEL_MAP`` → free (la conversation est servie
  par la jambe free-first) ;
- valeur connue du pool free (``FREE_MODELS``) → free ;
- sinon → payant.

Ne lève jamais : tout input inattendu → False.
"""

from __future__ import annotations


def has_free_leg(
    model_id: str,
    *,
    is_free_route_fn=None,
    free_model_map=None,
    free_models=None,
) -> bool:
    """True si le modèle a une jambe free (conversation servie en free)."""
    if not model_id or not isinstance(model_id, str):
        return False
    try:
        if callable(is_free_route_fn):
            try:
                if bool(is_free_route_fn(model_id)):
                    return True
            except Exception:
                pass
        fmap = free_model_map if isinstance(free_model_map, dict) else {}
        if fmap.get(model_id):
            return True
        if free_models is not None:
            try:
                if model_id in free_models:
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False


def is_free_class(
    model_id: str,
    *,
    is_free_route_fn=None,
    free_model_map=None,
    free_models=None,
) -> bool:
    """Alias sémantique : la conversation est-elle de classe free ?

    Le résumeur de compaction appartient à la MÊME classe que la conversation
    active : free → résumeur free (aucune clé payante requise), payant →
    résumeur payant.
    """
    return bool(
        has_free_leg(
            model_id,
            is_free_route_fn=is_free_route_fn,
            free_model_map=free_model_map,
            free_models=free_models,
        )
    )


__all__ = ["has_free_leg", "is_free_class"]
