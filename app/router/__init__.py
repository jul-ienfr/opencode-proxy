"""
app.router — résolution de route (_route_for) : logique pure, DI.

[P5 tranche 3] Extraction depuis opencode.py. Ce module est PUR (aucun
import projet) : toutes les entrées sont injectées par le délégué
``opencode._route_for``, qui lit SES globales À L'APPEL — les seams de
test ``oc._route_cache.clear()`` / ``oc._seen_route_version = -1`` /
``monkeypatch.setattr(oc, "DISABLE_MAPPING", ...)`` continuent de couler.

Contrats (test_hot_reload.py / test_proxy.py) :
  * cache invalidé quand ROUTE_VERSION change (hot-reload) ;
  * None JAMAIS caché (une route ajoutée plus tard doit être détectée) ;
  * DISABLE_MAPPING : custom routes + alias manuels opus/sonnet/haiku
    seulement, sinon direct MODELS ;
  * wildcard catch-all "*" (legacy "") en dernier recours.
"""

from typing import Any

from config import get_model_config as get_model_config
from config.settings import SORTED_CUSTOM_ROUTES as SORTED_CUSTOM_ROUTES
from config.settings import SORTED_ROUTES as SORTED_ROUTES


def route_for(
    model_name: str,
    *,
    models: dict,
    custom_routes: dict,
    disable_mapping: bool,
    route_cache: dict,
    route_cache_max: int,
    seen_version: int,
    current_version: int,
    sorted_routes: list | None = None,
    sorted_custom_routes: list | None = None,
    reload_fn=None,
    snapshot_fn=None,
    debug_fn=None,
) -> tuple[dict | None, int]:
    """Résout un nom de modèle → route dict (ou None). Retourne aussi la
    nouvelle valeur de seen_version (invalidation par ROUTE_VERSION).

    ``route_cache`` est muté EN PLACE (référence partagée avec l'hôte).

    ``snapshot_fn()`` : appelé APRÈS ``reload_fn()`` — retourne
    ``(sorted_routes, sorted_custom_routes, current_version)`` LUS À FRAIS.
    Indispensable : passer ces valeurs en kwargs depuis l'hôte les figeait
    AVANT le reload (régression test_external_edit_detected…).
    """
    if reload_fn is not None:
        # Check reload AVANT le lookup cache — il est throttlé à 5 s via
        # _custom_routes_last_check (≈ 2 stat() par fenêtre), donc le coût est
        # négligeable ; sans lui, un cache chaud ne verrait jamais les éditions
        # externes de config.yaml.
        reload_fn()
    if snapshot_fn is not None:
        sorted_routes, sorted_custom_routes, current_version = snapshot_fn()
    if sorted_routes is None or sorted_custom_routes is None:
        raise ValueError("sorted_routes/sorted_custom_routes requis (kwargs ou snapshot_fn)")
    if current_version != seen_version:
        seen_version = current_version
        if route_cache:
            route_cache.clear()
    cache_key = model_name.lower().strip()
    if cache_key in route_cache:
        return route_cache[cache_key], seen_version
    name = model_name.lower().strip()
    if not name:
        return None, seen_version

    def _cache_put(res: dict | None) -> dict | None:
        if res is not None:
            if len(route_cache) >= route_cache_max:
                route_cache.clear()
            route_cache[cache_key] = res
        return res

    # When DISABLE_MAPPING, only check custom routes (not auto-generated aliases)
    # but keep manual opus/sonnet/haiku aliases (they are defined in ROUTES even when auto-mapping is off)
    if disable_mapping:
        for r in sorted_custom_routes:
            if any(m in name for m in r.get("match", [])):
                if debug_fn:
                    debug_fn(f"  [route] DISABLE_MAPPING custom match: '{name}' → {r.get('model')}")
                return _cache_put(r), seen_version
        # Manual opus/sonnet/haiku routes must remain available even with DISABLE_MAPPING (Claude Code defaults)
        for r in sorted_routes:
            # Only the 3 manual aliases have match == opus/sonnet/haiku (checked via small set)
            mlist = [m.lower() for m in r.get("match", []) if isinstance(m, str)]
            if any(m in ("opus", "sonnet", "haiku") for m in mlist):
                if any(m in name for m in r.get("match", [])):
                    if debug_fn:
                        debug_fn(
                            f"  [route] DISABLE_MAPPING manual alias match: '{name}' → {r.get('model')}"
                        )
                    return _cache_put(r), seen_version
        # No custom route matched — check if the model exists directly
        if name in models:
            res: dict[str, Any] = {"match": [name], "model": model_name}
            if debug_fn:
                debug_fn(f"  [route] DISABLE_MAPPING direct model: '{name}'")
            return _cache_put(res), seen_version
        if debug_fn:
            debug_fn(f"  [route] DISABLE_MAPPING no match for '{name}'")
        # None NON caché : une route ajoutée plus tard doit être détectée.
        return None, seen_version
    # 0. Exact MODELS lookup first (fastest, O(1) — covers 90% of prod traffic)
    if name in models:
        res = {"match": [name], "model": name}
        for r_custom in sorted_custom_routes:
            if r_custom.get("enabled") is False:
                continue
            if any(m == name for m in r_custom.get("match", [])):
                res = r_custom
                break
        if debug_fn:
            debug_fn(f"  [route] exact MODELS hit: '{name}' → {res.get('model')}")
        return _cache_put(res), seen_version
    # 1. Model-based routing (sorted by longest match first)
    for r in sorted_routes:
        if r.get("enabled") is False:
            continue
        if any(m in name for m in r.get("match", [])):
            if debug_fn:
                debug_fn(f"  [route] model match: {r.get('model')} (pattern in '{name}')")
            return _cache_put(r), seen_version
    # 3. Wildcard catch-all: if a custom route "*" (or legacy "") exists, use it
    wildcard = custom_routes.get("*") or custom_routes.get("")
    if (
        wildcard
        and isinstance(wildcard, dict)
        and wildcard.get("model")
        and wildcard.get("enabled") is not False
    ):
        if debug_fn:
            debug_fn(f"  [route] wildcard catch-all: {wildcard.get('model')}")
        return _cache_put(wildcard), seen_version
    # 5. No match found
    if debug_fn:
        debug_fn(f"  [route] no match for '{name}'")
    # None NON caché : une route ajoutée plus tard pour ce modèle doit être
    # détectée au prochain passage.
    return None, seen_version


__all__ = [
    "SORTED_CUSTOM_ROUTES",
    "SORTED_ROUTES",
    "get_model_config",
    "route_for",
]
