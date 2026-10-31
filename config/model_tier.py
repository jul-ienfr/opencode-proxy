"""Classification Gratuit / Payant d'un modèle — pure, sans I/O.

Trois notions étaient mélangées dans le code historique :

* la **découverte** (``config/discovery.py::is_free_model``) : pricing 0,
  flags ``is_free``/``free``/``capabilities.free`` ou suffixe ``-free`` ;
* le **routage** (``opencode.py::_is_free_model_route``) : suffixe, présence
  dans ``FREE_MODELS`` **ou** mapping payant → gratuit autorisé ;
* le **catalogue vivant** (``config/live_models.py``) : jambe ``opencode``
  (anonyme) ou ``opencode-go`` (clé API).

Un même id peut donc être « free » pour l'une et « paid » pour l'autre. Ce
module rend un verdict explicite, avec sa provenance, et laisse séparées les
deux questions qui doivent le rester :

* ``tier`` — le modèle est-il gratuit ou payant ?
* ``free_first`` — le proxy va-t-il tenter la jambe gratuite d'abord ?

Aucune I/O, aucun accès aux globales : tous les signaux sont injectés, ce qui
rend les règles testables et reproductibles.
"""

from __future__ import annotations

from typing import Any

FREE = "free"
PAID = "paid"
UNKNOWN = "unknown"

ROUTE_FREE = "free"
ROUTE_PAID = "paid"
ROUTE_INTERNAL = "internal"
ROUTE_UNKNOWN = "unknown"

#: Jambes du catalogue vivant → tiers. Accepte aussi les tiers déjà
#: normalisés : les réponses API exposent ``["free", "paid"]`` et doivent
#: pouvoir être reclassées sans ambiguïté.
_CATALOG_LEG_TO_TIER = {
    "opencode": FREE,
    "opencode-go": PAID,
    "free": FREE,
    "paid": PAID,
}

_PAID_ENDPOINT_MARKERS = ("/zen/", "/inference/go/")
_FREE_ENDPOINT_MARKERS = ("opencode.ai/inference/",)

TIER_SOURCES = ("route", "suffix", "discovery", "mapped", "endpoint", "unproven", "catalog", "unknown")


def route_leg_for_endpoint(endpoint: str | None) -> str:
    """Jambe réellement visée par un endpoint.

    L'ordre compte : ``https://opencode.ai/inference/go/v1/...`` contient à la
    fois ``/inference/`` et ``/inference/go/`` ; c'est la jambe **payante**.
    """
    low = str(endpoint or "").strip().lower()
    if not low:
        return ROUTE_UNKNOWN
    if any(marker in low for marker in _PAID_ENDPOINT_MARKERS):
        return ROUTE_PAID
    if any(marker in low for marker in _FREE_ENDPOINT_MARKERS):
        return ROUTE_FREE
    return ROUTE_INTERNAL


def catalog_legs_from_entry(entry: dict[str, Any] | None) -> list[str]:
    """``entry["legs"]`` (live catalog) → ``["free", "paid"]`` dédupliqué."""
    raw = (entry or {}).get("legs")
    if not isinstance(raw, (list, tuple, set)):
        raw = [(entry or {}).get("leg")]
    out: list[str] = []
    for leg in raw:
        tier = _CATALOG_LEG_TO_TIER.get(str(leg or "").strip().lower())
        if tier and tier not in out:
            out.append(tier)
    return out


def route_leg_for_cfg(model_id: str, cfg: dict[str, Any] | None = None) -> str:
    """Jambe de la route configurée, sans importer ``config.settings``.

    ``cfg`` doit être le dict renvoyé par ``get_model_config()`` (il porte déjà
    l'``endpoint`` résolu). On retombe sur ``cfg["endpoint"]`` uniquement : un
    appelant sans cfg obtient ``unknown``, jamais un « paid » inventé.
    """
    data = cfg if isinstance(cfg, dict) else {}
    return route_leg_for_endpoint(data.get("endpoint"))


def _normalise_free_models(free_models: Any) -> set[str]:
    try:
        return {str(x).strip().lower() for x in (free_models or ()) if str(x).strip()}
    except TypeError:
        return set()


def _normalise_free_map(free_model_map: Any) -> dict[str, str]:
    if not isinstance(free_model_map, dict):
        return {}
    return {
        str(key).strip().lower(): str(value).strip().lower()
        for key, value in free_model_map.items()
        if str(key).strip() and str(value).strip()
    }


def classify(
    model_id: str,
    *,
    endpoint: str | None = None,
    catalog_legs: list[str] | tuple[str, ...] | None = None,
    free_models: set[str] | frozenset[str] | None = None,
    free_model_map: dict[str, str] | None = None,
    policy: str = "homonym",
    free_verified: bool = False,
) -> dict[str, Any]:
    """Verdict complet pour un id.

    Précédence (volontaire, elle encode les leçons du terrain) :

    1. suffixe ``-free`` → gratuit ;
    2. mapping **explicite** vers un free → payant avec repli gratuit (un id
       payant ne devient pas gratuit parce qu'il peut être substitué). Une
       entrée de ``free_model_map`` est une décision humaine : elle est
       toujours active ; ``policy`` ne filtre que les candidats automatiques
       (``X-free`` / ``free_model_candidates``) ;
    3. id présent dans ``FREE_MODELS`` → gratuit ``discovery``. Ce registre
       n'est plus alimenté par le simple listing de la jambe anonyme : il ne
       contient que les suffixes, les preuves ``free_verified`` et les ids
       promus par la sonde free-leg automatique ;
    4. preuve explicite ``free_verified`` hors registre (ex. ``big-pickle``
       au premier boot) → gratuit, même si la route est momentanément
       payante (``route_mismatch``) ;
    5. endpoint réellement payant → payant, même si l'id figure sur la jambe
       anonyme du catalogue ;
    6. endpoint free non vérifié → payant ``unproven`` (le proxy peut tenter
       le free d'abord via ``free_first``) ;
    7. catalogue seul → dernier recours ;
    8. sinon ``unknown`` (jamais « payant par défaut » silencieux).

    Une inscription sur la jambe anonyme (``/inference/v1/models``) n'est pas
    une preuve de gratuité : le catalogue y liste aussi des modèles payants
    (``gpt-5.5``, ``claude-opus-5-5``…). Le relevé vivant ne les ajoute plus
    à ``FREE_MODELS`` : il les passe à la sonde free-leg, et seuls les
    verdicts ``alive``/``limited``/``not_probeable`` sont promus. Sans ce
    garde-fou, le GUI affichait « 45 free » alors que la majorité étaient des
    modèles Go.
    """
    mid = str(model_id or "").strip().lower()
    free_set = _normalise_free_models(free_models)
    free_map = _normalise_free_map(free_model_map)
    catalog = [
        tier
        for tier in (_CATALOG_LEG_TO_TIER.get(str(leg or "").strip().lower()) for leg in (catalog_legs or ()))
        if tier
    ]
    catalog = list(dict.fromkeys(catalog))
    route = route_leg_for_endpoint(endpoint)

    suffix = bool(mid) and mid.endswith("-free")
    discovered = bool(mid) and mid in free_set
    mapping = free_map.get(mid)
    # Entrée explicite de free_model_map = décision humaine, toujours honorée.
    # La politique de substitution ne concerne que les candidats automatiques.
    mapping_active = bool(mapping)

    if suffix:
        tier, source = FREE, "suffix"
    elif mapping_active:
        tier, source = PAID, "mapped"
    elif discovered:
        # ``FREE_MODELS`` est désormais un registre vérifié (suffixe,
        # free_verified ou sonde free-leg), plus le listing brut de la jambe
        # anonyme. Un id qui s'y trouve a donc une preuve de gratuité.
        tier, source = FREE, "discovery"
    elif free_verified:
        # Preuve explicite (``source=free_verified``, ex. big-pickle) : elle
        # prime sur l'endpoint, même si la route est momentanément payante ;
        # ``route_mismatch`` signale alors l'incohérence.
        tier, source = FREE, "endpoint"
    elif route == ROUTE_PAID:
        tier, source = PAID, "endpoint"
    elif route == ROUTE_FREE:
        tier, source = PAID, "unproven"
    elif catalog:
        tier = FREE if FREE in catalog and PAID not in catalog else PAID
        source = "catalog"
    else:
        tier, source = UNKNOWN, "unknown"

    free_first = bool(suffix or discovered or mapping_active)
    free_target = None
    if tier == PAID and mapping_active:
        free_target = mapping

    route_mismatch = (tier == FREE and route == ROUTE_PAID) or (
        tier == PAID and route == ROUTE_FREE and source != "unproven"
    )
    catalog_mismatch = bool(catalog and route in (ROUTE_FREE, ROUTE_PAID) and route not in catalog)

    return {
        "id": model_id,
        "tier": tier,
        "tier_source": source,
        "route_leg": route,
        "catalog_legs": catalog,
        "free_first": free_first,
        "free_target": free_target,
        "free_mapping": mapping,
        "free_mapping_active": mapping_active,
        "route_mismatch": route_mismatch,
        "catalog_mismatch": catalog_mismatch,
        "route_target": None,
    }


def classify_model(
    model_id: str,
    cfg: dict[str, Any] | None = None,
    entry: dict[str, Any] | None = None,
    *,
    free_models: Any = None,
    free_model_map: Any = None,
    policy: str = "homonym",
    endpoint: str | None = None,
    free_verified: bool | None = None,
) -> dict[str, Any]:
    """Variante pratique : ``cfg`` = ``get_model_config()``, ``entry`` = live."""
    data = cfg if isinstance(cfg, dict) else {}
    if free_verified is None:
        free_verified = str(data.get("source") or "").strip().lower() == "free_verified"
    return classify(
        model_id,
        endpoint=endpoint if endpoint is not None else data.get("endpoint"),
        catalog_legs=catalog_legs_from_entry(entry),
        free_models=free_models,
        free_model_map=free_model_map,
        policy=policy,
        free_verified=bool(free_verified),
    )


def classify_route_alias(
    model_id: str,
    target_id: str,
    *,
    cfg: dict[str, Any] | None = None,
    entry: dict[str, Any] | None = None,
    target_cfg: dict[str, Any] | None = None,
    target_entry: dict[str, Any] | None = None,
    free_models: Any = None,
    free_model_map: Any = None,
    policy: str = "homonym",
) -> dict[str, Any]:
    """Classe un alias de route comme le proxy le sert réellement.

    * cible payante → verdict de la cible, ``tier_source="route"`` ;
    * cible gratuite → l'alias reste **payant** avec ``free_target`` : c'est
      une substitution gratuite, pas un modèle gratuit (même règle que
      ``mimo-v2.6-flash``). Un alias dont l'id est lui-même explicitement
      free (suffixe/``free_verified``) reste free.
    """
    kwargs = {
        "free_models": free_models,
        "free_model_map": free_model_map,
        "policy": policy,
    }
    target = classify_model(target_id, target_cfg, target_entry, **kwargs)
    target["tier_source"] = "route"
    target["route_target"] = target_id
    if target.get("tier") != FREE:
        return target

    base = classify_model(model_id, cfg, entry, **kwargs)
    if base.get("tier") == FREE:
        return target
    base["tier"] = PAID
    base["tier_source"] = "route"
    base["free_first"] = True
    base["free_target"] = target_id
    base["route_target"] = target_id
    return base


def tier_counts(rows: Any) -> dict[str, int]:
    """Compte les catégories visibles GUI/API pour une liste de verdicts."""
    counts = {
        "total": 0,
        "free": 0,
        "paid": 0,
        "paid_unproven": 0,
        "paid_free_fallback": 0,
        "paid_free_ignored": 0,
        "unknown": 0,
        "route_mismatch": 0,
        "catalog_mismatch": 0,
    }
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        counts["total"] += 1
        tier = row.get("tier")
        if tier == FREE:
            counts["free"] += 1
        elif tier == PAID:
            counts["paid"] += 1
            if row.get("tier_source") == "unproven":
                counts["paid_unproven"] += 1
            if row.get("free_target") or row.get("free_mapping_active"):
                counts["paid_free_fallback"] += 1
            elif row.get("free_mapping"):
                counts["paid_free_ignored"] += 1
        else:
            counts["unknown"] += 1
        if row.get("route_mismatch"):
            counts["route_mismatch"] += 1
        if row.get("catalog_mismatch"):
            counts["catalog_mismatch"] += 1
    return counts


def public_tier_fields(verdict: dict[str, Any]) -> dict[str, Any]:
    """Sous-ensemble destiné aux réponses API (sans l'id redondant)."""
    keys = (
        "tier",
        "tier_source",
        "route_leg",
        "catalog_legs",
        "free_first",
        "free_target",
        "free_mapping",
        "free_mapping_active",
        "route_mismatch",
        "catalog_mismatch",
        "route_target",
    )
    return {key: verdict.get(key) for key in keys}


__all__ = [
    "FREE",
    "PAID",
    "UNKNOWN",
    "ROUTE_FREE",
    "ROUTE_PAID",
    "ROUTE_INTERNAL",
    "ROUTE_UNKNOWN",
    "TIER_SOURCES",
    "catalog_legs_from_entry",
    "classify",
    "classify_model",
    "classify_route_alias",
    "public_tier_fields",
    "route_leg_for_cfg",
    "route_leg_for_endpoint",
    "tier_counts",
]
