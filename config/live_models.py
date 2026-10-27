"""config.live_models — état vivant du catalogue modèles.

**Le défaut.** ``MODELS`` était construit au démarrage depuis ``config.yaml``
et complété par une découverte *add-only* : un modèle déprécié restait annoncé
et routable jusqu'à une purge manuelle. Rien ne reliait la liste servie au
client à ce que l'amont annonce réellement.

**La réparation.** Ce module tient un instantané du dernier relevé réussi des
deux jambes opencode (``/models``) et du catalogue officiel
(``models.opencode.ai/api.json``). Il en déduit :

* les ids **réellement servis** (présents sur au moins une jambe, non
  dépréciés sur cette jambe) ;
* les ids **retirés** (absents des jambes ou dépréciés partout) ;
* les **niveaux d'effort** autorisés par modèle, issus de
  ``reasoning_options`` du catalogue ;
* les **notices** à destination des clients qui utilisaient un modèle devenu
  déprécié.

Le module est **pur** : aucun import projet au chargement (les fonctions qui
ont besoin de ``config.models_catalog`` l'importent paresseusement), aucune I/O
réseau, aucun accès à ``config.settings``. L'hôte injecte les dictionnaires à
muter et les callbacks de routage. C'est ce qui permet de le tester sans
réseau et de l'importer depuis ``config.effort_caps`` sans cycle.

Vocabulaire des jambes :

    ``opencode-go``  jambe payée   (zen/go)
    ``opencode``     jambe anonyme (inference)

Une entrée de ``reasoning_options`` de type ``effort`` porte les niveaux que
l'amont accepte. Une liste vide (ou un catalogue muet) n'est **pas** une
interdiction : c'est « aucune information », et l'appelant retombe alors sur
la configuration locale historique (``thinking.effort_caps``).
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("config.settings")

PAID_LEG = "opencode-go"
FREE_LEG = "opencode"
LEGS = (PAID_LEG, FREE_LEG)

#: Ordre total de repli, identique aux modules d'effort. Sert uniquement à
#: choisir le maximum d'une liste déjà validée par le catalogue.
EFFORT_ORDER_DEFAULT = ["minimal", "low", "medium", "high", "xhigh", "max"]

#: Niveaux qui ne sont pas des efforts (désactivation).
_DISABLED_LEVELS = ("", "none", "off", "disabled", "null")

#: Durée de conservation d'une notice client (7 jours).
NOTICE_TTL_S = 7 * 24 * 3600

#: Nombre maximal d'événements conservés pour le dashboard.
MAX_EVENTS = 30

#: Nombre maximal de notices par client (borne mémoire).
MAX_NOTICES_PER_CLIENT = 20

#: Nombre maximal de clients notifiés par modèle (borne mémoire).
MAX_CLIENTS_PER_MODEL = 500

#: Nombre maximal d'appels de catalogue conservés en mémoire dans l'API.
MAX_API_MODELS = 2000


# ─────────────────────────── État global ────────────────────────────────────


def _new_state() -> dict[str, Any]:
    return {
        "revision": 0,
        "ready": False,
        "partial": False,
        "guarded": False,
        "last_refresh": None,
        "last_catalog_refresh": None,
        "next_refresh": None,
        "source": "none",
        "error": None,
        "available": {},
        "deprecated": {},
        "available_ids": set(),
        "served": set(),
        "initialized": False,
        "by_leg": {leg: set() for leg in LEGS},
        "added": [],
        "removed": [],
        "migrated": [],
        "retired": set(),
        "events": [],
        "notices": {},
        "announced": set(),
    }


_STATE: dict[str, Any] = _new_state()
_LOCK = threading.RLock()


def reset_state() -> None:
    """Réinitialise l'état (tests uniquement)."""
    global _STATE
    with _LOCK:
        _STATE = _new_state()


def get_state() -> dict[str, Any]:
    """Référence à l'état vivant (lecture seule pour les appelants)."""
    return _STATE


def load_persisted(
    *,
    announced=None,
    events=None,
    revision: int = 0,
    available_ids=None,
) -> None:
    """Précharge les éléments durables (appelé par l'hôte au démarrage).

    Seuls les éléments utiles au dashboard et à la déduplication des notices
    sont persistés : l'inventaire lui-même est reconstruit par le prochain
    relevé réseau. ``available_ids`` sert à ne pas rejouer un pop-up quand le
    relevé est identique au démarrage précédent.
    """
    with _LOCK:
        if announced:
            _STATE["announced"] = {str(x).strip().lower() for x in announced if str(x).strip()}
        if events:
            _STATE["events"] = [e for e in events if isinstance(e, dict)][-MAX_EVENTS:]
        if available_ids is not None:
            _STATE["available_ids"] = {
                str(x).strip().lower() for x in available_ids if str(x).strip()
            }
            _STATE["initialized"] = True
        try:
            _STATE["revision"] = max(int(revision or 0), _STATE["revision"])
        except (TypeError, ValueError):
            pass


def persisted() -> dict[str, Any]:
    """Instantané minimal à écrire sur disque par l'hôte."""
    with _LOCK:
        return {
            "revision": int(_STATE.get("revision") or 0),
            "announced": sorted(_STATE.get("announced") or ()),
            "available_ids": sorted(_STATE.get("available_ids") or ()),
            "events": list(_STATE.get("events") or [])[-MAX_EVENTS:],
            "last_refresh": _STATE.get("last_refresh"),
        }


# ─────────────────────────── Catalogue ─────────────────────────────────────


def _mc():
    """``config.models_catalog`` importé paresseusement (zéro side-effect)."""
    try:
        from config import models_catalog as mc

        return mc
    except Exception:  # pragma: no cover — config absente (tests isolés)
        return None


def _entry(catalog: dict | None, leg: str, model_id: str) -> dict:
    if not isinstance(catalog, dict):
        return {}
    try:
        row = ((catalog.get(leg) or {}).get("models") or {}).get(model_id)
    except Exception:
        return {}
    return row if isinstance(row, dict) else {}


def _status(entry: dict) -> str:
    return str(entry.get("status") or "active").strip().lower() or "active"


def _is_deprecated_entry(entry: dict) -> bool:
    return _status(entry) == "deprecated"


def _family_for(catalog: dict | None, leg: str, model_id: str) -> str | None:
    mc = _mc()
    if mc is None or not catalog:
        return None
    try:
        return mc.api_family_for_model(catalog, leg, model_id)
    except Exception:
        return None


def _base_url_for(catalog: dict | None, leg: str, model_id: str) -> str | None:
    mc = _mc()
    if mc is None or not catalog:
        return None
    try:
        return mc.model_api(catalog, leg, model_id)
    except Exception:
        return None


def _serving_provider(catalog: dict | None, model_id: str) -> str | None:
    mc = _mc()
    if mc is None or not catalog:
        return None
    try:
        return mc.serving_provider(model_id, catalog)
    except Exception:
        return None


# ─────────────────────── Reasoning options / effort ─────────────────────────


def _reasoning_options(entry: dict) -> list | None:
    """Liste brute ``reasoning_options`` ; ``None`` si le champ est absent.

    ``[]`` est une information : le modèle n'expose aucun paramètre d'effort.
    """
    if not isinstance(entry, dict):
        return None
    raw = entry.get("reasoning_options")
    if raw is None:
        return None
    return raw if isinstance(raw, list) else None


def effort_levels_from_options(options: list | None) -> list | None:
    """Niveaux d'effort autorisés, dans l'ordre du catalogue.

    * ``None``  : aucune information (champ absent) ;
    * ``[]``    : le catalogue déclare explicitement qu'il n'y a pas d'effort ;
    * ``[...]`` : niveaux autorisés (``none``/``off`` exclus, ce sont des
      désactivations, pas des efforts).
    """
    if options is None:
        return None
    levels: list[str] = []
    for option in options:
        if not isinstance(option, dict) or str(option.get("type") or "").lower() != "effort":
            continue
        values = option.get("values")
        if not isinstance(values, (list, tuple)):
            continue
        for value in values:
            name = str(value).strip().lower() if isinstance(value, str) else ""
            if name and name not in _DISABLED_LEVELS and name not in levels:
                levels.append(name)
    return levels


def _thinking_capability(entry: dict) -> dict | None:
    """Types de raisonnement que le catalogue déclare, ou ``None`` si muet."""
    if not isinstance(entry, dict):
        return None
    options = _reasoning_options(entry)
    reasoning = entry.get("reasoning")
    if options is None and reasoning is None:
        return None
    has_toggle = any(
        isinstance(o, dict) and str(o.get("type") or "").lower() == "toggle" for o in (options or [])
    )
    has_budget = any(
        isinstance(o, dict) and str(o.get("type") or "").lower() == "budget_tokens"
        for o in (options or [])
    )
    has_effort = bool(effort_levels_from_options(options))
    supported = bool(reasoning) if reasoning is not None else bool(options)
    return {
        "supported": supported,
        "types": {
            "adaptive": bool(supported and (has_effort or has_toggle)),
            "enabled": bool(supported and (has_budget or has_toggle or has_effort)),
            "disabled": bool(supported and has_toggle),
            "between_tools": bool(supported),
        },
    }


def _metadata(entry: dict, model_id: str, family: str | None, base_url: str | None) -> dict:
    limit = entry.get("limit") if isinstance(entry.get("limit"), dict) else {}
    modalities = entry.get("modalities") if isinstance(entry.get("modalities"), dict) else {}
    return {
        "id": model_id,
        "name": entry.get("name") or model_id,
        "family": entry.get("family") or family,
        "release_date": entry.get("release_date"),
        "limit": {
            "context": limit.get("context"),
            "input": limit.get("input"),
            "output": limit.get("output"),
        },
        "modalities": {
            "input": list(modalities.get("input") or []),
            "output": list(modalities.get("output") or []),
        },
        "reasoning": entry.get("reasoning"),
        "structured_output": bool(entry.get("structured_output")),
        "interleaved": entry.get("interleaved"),
        "family_api": family,
        "base_url": base_url,
    }


# ─────────────────────────── Construction du snapshot ──────────────────────


def _preferred_leg(
    model_id: str,
    live_legs: list[str],
    catalog: dict | None,
    current_leg: str | None,
) -> tuple[str, bool]:
    """Jambe retenue et drapeau « déprécié partout ».

    Un id actif sur une jambe et déprécié sur l'autre migre vers la jambe
    active. S'il est déprécié sur toutes les jambes où il est annoncé, il est
    retiré. La jambe courante gagne quand elle est active, pour ne pas déplacer
    un modèle qui fonctionne sans raison.
    """
    statuses = {leg: _status(_entry(catalog, leg, model_id)) for leg in live_legs}
    active = [leg for leg in live_legs if statuses.get(leg) != "deprecated"]
    if not active:
        return (live_legs[0] if live_legs else PAID_LEG), True
    if len(active) == 1:
        return active[0], False
    if current_leg in active:
        return str(current_leg), False
    low = str(model_id or "").lower()
    if low.endswith("-free") and FREE_LEG in active:
        return FREE_LEG, False
    serving = _serving_provider(catalog, model_id)
    if serving in active:
        return str(serving), False
    return (PAID_LEG if PAID_LEG in active else active[0]), False


def _effort_payload(entry: dict) -> tuple[list | None, list | None, dict | None]:
    options = _reasoning_options(entry)
    return options, effort_levels_from_options(options), _thinking_capability(entry)


def build_snapshot(
    *,
    live_by_leg: dict[str, set[str]],
    current_models: dict[str, dict],
    current_legs: dict[str, str | None] | None = None,
    catalog: dict | None = None,
    fetched_legs: set[str] | None = None,
    previous_by_leg: dict[str, set[str]] | None = None,
    purged_ids: set[str] | frozenset = frozenset(),
    ignored_ids: set[str] | frozenset = frozenset(),
    is_internal_fn: Callable[[str, dict], bool] | None = None,
    source: str = "live",
    now: float | None = None,
) -> dict[str, Any]:
    """Calcule l'instantané sans muter ``MODELS``.

    ``fetched_legs`` distingue un relevé complet d'un relevé partiel : si une
    jambe a échoué, l'appelant passe l'ancien ensemble pour cette jambe et
    marque le relevé partiel. Un relevé partiel ne supprime **jamais** un
    modèle : on ne confond pas « pas encore vu » et « disparu ».
    """
    fetched = set(fetched_legs) if fetched_legs is not None else set(live_by_leg)
    previous = previous_by_leg or {}
    merged: dict[str, set[str]] = {}
    for leg in LEGS:
        if leg in fetched:
            merged[leg] = set(live_by_leg.get(leg) or ())
        else:
            merged[leg] = set(previous.get(leg) or ())
    partial = bool(fetched) and fetched != set(LEGS)

    purged = {str(x).strip().lower() for x in purged_ids if str(x).strip()}
    ignored = {str(x).strip().lower() for x in ignored_ids if str(x).strip()}
    current_legs = current_legs or {}
    is_internal = is_internal_fn or (lambda _mid, _cfg: False)

    available: dict[str, dict] = {}
    deprecated: dict[str, dict] = {}
    all_live = set().union(*merged.values()) if merged else set()

    for model_id in sorted(all_live):
        low = model_id.strip().lower()
        if not low or low in purged or low in ignored:
            continue
        live_legs = [leg for leg in LEGS if low in {x.strip().lower() for x in merged.get(leg, ())}]
        if not live_legs:
            continue
        current_leg = current_legs.get(model_id) or current_legs.get(low)
        leg, deprecated_everywhere = _preferred_leg(low, live_legs, catalog, current_leg)
        entry = _entry(catalog, leg, low) or _entry(catalog, live_legs[0], low)
        options, levels, thinking = _effort_payload(entry)
        family = _family_for(catalog, leg, low)
        base_url = _base_url_for(catalog, leg, low)
        row = {
            "id": low,
            "leg": leg,
            "legs": live_legs,
            "status": "deprecated" if deprecated_everywhere else _status(entry),
            "deprecated": deprecated_everywhere,
            "reasoning_options": options,
            "effort_levels": levels,
            "thinking": thinking,
            "metadata": _metadata(entry, low, family, base_url),
        }
        if deprecated_everywhere:
            deprecated[low] = row
        else:
            available[low] = row

    added_ids: list[str] = []
    migrated_ids: list[str] = []
    removed_ids: list[str] = []
    removed_deprecated: list[str] = []
    removed_unavailable: list[str] = []
    kept_internal: list[str] = []

    for model_id in sorted(current_models):
        low = model_id.strip().lower()
        cfg = current_models.get(model_id) if isinstance(current_models.get(model_id), dict) else {}
        if is_internal(model_id, cfg):
            kept_internal.append(model_id)
            continue
        if low in available:
            current_leg = current_legs.get(model_id) or current_legs.get(low)
            if current_leg and current_leg != available[low]["leg"]:
                migrated_ids.append(model_id)
            continue
        # Absent de l'inventaire vivant : le catalogue peut encore dire
        # « deprecated » sur la jambe historiquement routée. C'est une
        # dépréciation, pas une simple disparition silencieuse.
        if low not in deprecated:
            current_leg = current_legs.get(model_id) or current_legs.get(low)
            cat_entry: dict = {}
            if current_leg:
                cat_entry = _entry(catalog, current_leg, low)
            if not cat_entry:
                for _leg in LEGS:
                    candidate = _entry(catalog, _leg, low)
                    if candidate:
                        cat_entry = candidate
                        current_leg = current_leg or _leg
                        break
            if _is_deprecated_entry(cat_entry):
                options, levels, thinking = _effort_payload(cat_entry)
                family = _family_for(catalog, current_leg, low) if current_leg else None
                base_url = _base_url_for(catalog, current_leg, low) if current_leg else None
                deprecated[low] = {
                    "id": low,
                    "leg": current_leg,
                    "legs": [current_leg] if current_leg else [],
                    "status": "deprecated",
                    "deprecated": True,
                    "reasoning_options": options,
                    "effort_levels": levels,
                    "thinking": thinking,
                    "metadata": _metadata(cat_entry, low, family, base_url),
                }
        # Absent de l'inventaire vivant.
        if low in deprecated:
            removed_ids.append(model_id)
            removed_deprecated.append(model_id)
            continue
        if partial:
            # Une jambe manque : impossible de distinguer « disparu » d'un
            # relevé incomplet. On conserve, le prochain cycle tranchera.
            continue
        if low in ignored or low in purged:
            continue
        removed_ids.append(model_id)
        removed_unavailable.append(model_id)

    for model_id in sorted(available):
        if model_id not in {m.strip().lower() for m in current_models}:
            added_ids.append(model_id)

    # Garde-fou anti-relevé tronqué : si une jambe répond 200 avec une liste
    # incomplète (maintenance, incident), retirer 80 % du catalogue d'un coup
    # serait pire que de conserver une entrée morte un cycle de plus. On
    # marque le relevé partiel et on ne supprime rien.
    guarded = False
    if not partial and removed_ids:
        counted = [
            m for m in current_models
            if not is_internal(m, current_models.get(m) or {})
        ]
        if len(counted) >= 10 and len(removed_ids) / max(1, len(counted)) > 0.5:
            guarded = True
            partial = True
            removed_ids = []
            removed_deprecated = []
            removed_unavailable = []

    # Relevé partiel sans inventaire précédent (premier boot, une jambe en
    # échec) : conserver les modèles configurés comme « disponibilité
    # inconnue » plutôt que de les masquer de /v1/models sans les retirer.
    if partial:
        for model_id, cfg in current_models.items():
            low = str(model_id or "").strip().lower()
            if not low or low in available or low in deprecated:
                continue
            if is_internal(model_id, cfg if isinstance(cfg, dict) else {}):
                continue
            current_leg = current_legs.get(model_id) or current_legs.get(low)
            available[low] = {
                "id": low,
                "leg": current_leg,
                "legs": [current_leg] if current_leg else [],
                "status": "unknown",
                "deprecated": False,
                "reasoning_options": None,
                "effort_levels": None,
                "thinking": None,
                "metadata": _metadata({}, low, None, None),
            }

    iso = _dt.datetime.now(_dt.UTC).isoformat()
    return {
        "ready": True,
        "partial": partial,
        "guarded": guarded,
        "fetched_legs": sorted(fetched),
        "source": source,
        "generated_at": iso,
        "by_leg": {leg: sorted(merged.get(leg, ())) for leg in LEGS},
        "available": available,
        "deprecated": deprecated,
        "added_ids": added_ids,
        "migrated_ids": migrated_ids,
        "removed_ids": removed_ids,
        "removed_deprecated": removed_deprecated,
        "removed_unavailable": removed_unavailable,
        "kept_internal": kept_internal,
    }


# ─────────────────────────── Application ────────────────────────────────────


def build_route(
    model_id: str,
    entry: dict,
    *,
    api_base_free: str,
    api_base_openai: str,
    api_base_anthropic: str,
    responses_free: str,
    responses_paid: str,
    resolve_protocol_fn: Callable[[str], str],
) -> dict | None:
    """Route ``{endpoint, protocol, api, source}`` pour un modèle vivant.

    La famille vient du catalogue quand il la connaît ; sinon on retombe sur
    l'heuristique historique (``muse``/``spark`` → Responses, le reste →
    chat). Une famille connue sans convertisseur (``gemini``) rend ``None`` :
    on ne fabrique pas un endpoint qui parlerait le mauvais dialecte.
    """
    leg = str(entry.get("leg") or PAID_LEG)
    is_free = leg == FREE_LEG
    meta = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    family = str(meta.get("family_api") or "").strip().lower()
    if not family:
        low = str(model_id or "").lower()
        family = "responses" if ("muse" in low or "spark" in low) else "chat"
    if family == "gemini":
        return None
    if family == "messages":
        base = str(meta.get("base_url") or "").strip().rstrip("/")
        if base:
            endpoint = base + "/messages"
        elif not is_free:
            endpoint = api_base_anthropic
        else:
            return None  # pas de base anthropic free connue sans catalogue
        protocol = "anthropic"
    elif family == "responses":
        endpoint = responses_free if is_free else responses_paid
        protocol = "openai"
    else:
        endpoint = api_base_free if is_free else api_base_openai
        protocol = "openai"
    if not endpoint:
        return None
    try:
        fallback_protocol = resolve_protocol_fn(model_id)
    except Exception:
        fallback_protocol = protocol
    return {
        "endpoint": endpoint,
        "protocol": protocol or fallback_protocol or "openai",
        "api": family,
        "source": "live_catalog",
    }


def apply_snapshot(
    snapshot: dict,
    *,
    models: dict[str, dict],
    free_models: set | None = None,
    free_model_map: dict | None = None,
    free_model_pool: list | None = None,
    route_fn: Callable[[str, dict], dict | None],
    is_internal_fn: Callable[[str, dict], bool] | None = None,
    cache_clear_fn: Callable[[], None] | None = None,
    log=logger,
) -> dict[str, Any]:
    """Applique l'instantané à ``MODELS`` et aux registres free (en place)."""
    internal = is_internal_fn or (lambda _mid, _cfg: False)
    available = snapshot.get("available") or {}
    removed: list[str] = []
    added: list[str] = []
    migrated: list[str] = []

    for model_id in snapshot.get("removed_ids") or []:
        cfg = models.get(model_id)
        if isinstance(cfg, dict) and internal(model_id, cfg):
            continue
        if models.pop(model_id, None) is not None:
            removed.append(model_id)
        if isinstance(free_models, set):
            free_models.discard(model_id)

    if isinstance(free_model_map, dict):
        removed_set = set(removed)
        for key in list(free_model_map):
            target = free_model_map.get(key)
            if key in removed_set or target in removed_set:
                free_model_map.pop(key, None)

    for model_id in snapshot.get("added_ids") or []:
        entry = available.get(model_id)
        if not isinstance(entry, dict):
            continue
        route = route_fn(model_id, entry)
        if not route:
            continue
        models[model_id] = route
        added.append(model_id)
        if isinstance(free_models, set) and entry.get("leg") == FREE_LEG:
            free_models.add(model_id)

    for model_id in snapshot.get("migrated_ids") or []:
        entry = available.get(model_id)
        if not isinstance(entry, dict) or model_id not in models:
            continue
        route = route_fn(model_id, entry)
        if not route:
            continue
        old = models.get(model_id) if isinstance(models.get(model_id), dict) else {}
        if (
            old.get("endpoint") == route.get("endpoint")
            and old.get("protocol") == route.get("protocol")
        ):
            continue
        merged = {**old, **route}
        models[model_id] = merged
        migrated.append(model_id)
        if isinstance(free_models, set) and entry.get("leg") == FREE_LEG:
            free_models.add(model_id)

    # Les modèles de la jambe anonyme sont free par construction, même sans
    # suffixe historiquement reconnu (`big-pickle`, `beam-free`…).
    if isinstance(free_models, set):
        for model_id, entry in available.items():
            if (
                isinstance(entry, dict)
                and entry.get("leg") == FREE_LEG
                and model_id in models
            ):
                free_models.add(model_id)

    if isinstance(free_model_pool, list) and isinstance(free_models, set):
        free_model_pool[:] = sorted(free_models)

    if callable(cache_clear_fn):
        try:
            cache_clear_fn()
        except Exception:
            pass

    if log and (added or removed or migrated):
        try:
            log.info(
                "[live-models] applied: added=%d removed=%d migrated=%d",
                len(added),
                len(removed),
                len(migrated),
            )
        except Exception:
            pass
    return {"added": added, "removed": removed, "migrated": migrated}


def commit_snapshot(
    snapshot: dict,
    apply_result: dict,
    *,
    source: str = "live",
    model_ids: set[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Met à jour l'état vivant, l'historique et les notices à créer.

    ``model_ids`` restreint l'inventaire « servi » aux ids réellement
    présents dans ``MODELS`` (les familles non converties comme ``gemini``
    restent dans le relevé mais ne doivent pas produire de pop-up).
    """
    now = time.time() if now is None else now
    iso = _dt.datetime.fromtimestamp(now, _dt.UTC).isoformat()
    with _LOCK:
        previous_live = set(_STATE.get("available_ids") or ())
        first_sync = not bool(_STATE.get("initialized")) and not previous_live
        available = snapshot.get("available") or {}
        deprecated = snapshot.get("deprecated") or {}
        partial = bool(snapshot.get("partial"))

        announced = _STATE.get("announced") or set()
        notified: list[str] = []
        if not partial:
            candidates = {
                str(x).strip().lower()
                for x in (set(deprecated) | set(apply_result.get("removed") or ()))
                if str(x).strip()
            }
            notified = sorted(
                model
                for model in candidates
                if model not in announced
            )
            announced.update(notified)
            _STATE["announced"] = announced

        # Ne rejoue pas un pop-up quand le relevé est identique à celui du
        # démarrage précédent (les ajouts/retraits réels sont comparés à
        # l'inventaire persisté, pas à l'état mémoire vide).
        current_live = (
            set(available) & {str(x).strip().lower() for x in model_ids}
            if model_ids is not None
            else set(available)
        )
        new_live = current_live - previous_live
        gone_live = previous_live - current_live
        added_now = [
            str(x).strip().lower()
            for x in (apply_result.get("added") or ())
            if str(x).strip().lower() not in previous_live
        ]
        removed_now = [
            str(x).strip().lower()
            for x in (apply_result.get("removed") or ())
            if str(x).strip().lower() in previous_live
        ]
        event_added = sorted(set(new_live) | set(added_now))
        event_removed = sorted(set(gone_live) | set(removed_now))
        changed = bool(event_added or event_removed) and not first_sync

        retired = set(_STATE.get("retired") or ())
        retired.update(str(x).strip().lower() for x in (apply_result.get("removed") or ()))
        retired.difference_update(set(available))
        _STATE["retired"] = retired

        _STATE.update(
            {
                "ready": True,
                "partial": partial,
                "guarded": bool(snapshot.get("guarded")),
                "last_refresh": iso,
                "source": source,
                "error": None,
                "available": available,
                "deprecated": deprecated,
                "available_ids": current_live,
                "served": current_live,
                "initialized": True,
                "by_leg": {
                    leg: set((snapshot.get("by_leg") or {}).get(leg) or ())
                    for leg in LEGS
                },
                "added": list(apply_result.get("added") or ()),
                "removed": list(apply_result.get("removed") or ()),
                "migrated": list(apply_result.get("migrated") or ()),
            }
        )

        event = None
        if changed:
            _STATE["revision"] = int(_STATE.get("revision") or 0) + 1
            dep_set = set(snapshot.get("removed_deprecated") or ())
            unavail_set = set(snapshot.get("removed_unavailable") or ())
            event = {
                "at": iso,
                "revision": int(_STATE["revision"]),
                "added": event_added,
                "removed": event_removed,
                "deprecated": [m for m in event_removed if m in dep_set],
                "unavailable": [m for m in event_removed if m in unavail_set],
                "migrated": list(apply_result.get("migrated") or ()),
                "notified": list(notified),
                "partial": partial,
            }
            events = list(_STATE.get("events") or ())
            events.append(event)
            _STATE["events"] = events[-MAX_EVENTS:]

        return {
            "revision": int(_STATE.get("revision") or 0),
            "notified_ids": notified,
            "event": event,
            "changed": changed,
        }


# ─────────────────────────── Notices clients ────────────────────────────────


def notice_message(model_id: str) -> str:
    return (
        f'Model "{model_id}" is deprecated or no longer served by OpenCodeProxy. '
        "Use GET /v1/models to choose an active model."
    )


def _client_keys(client_ip: str, account_alias: str) -> list[str]:
    keys: list[str] = []
    ip = str(client_ip or "").strip()
    alias = str(account_alias or "").strip()
    if ip:
        keys.append("ip:" + ip)
    if alias:
        keys.append("acct:" + alias)
    return keys


def record_deprecation_notices(
    clients_by_model: dict[str, list[dict]],
    *,
    now: float | None = None,
) -> list[dict]:
    """Enregistre une notice par couple (modèle, client) touché.

    ``clients_by_model`` : ``{model_id: [{client_ip, account_alias}, ...]}``.
    Les clés sont l'IP et, si connue, l'alias de compte. Une même notice est
    dédupliquée par modèle pour un client donné.
    """
    now = time.time() if now is None else now
    iso = _dt.datetime.fromtimestamp(now, _dt.UTC).isoformat()
    created: list[dict] = []
    with _LOCK:
        notices = _STATE.setdefault("notices", {})
        for model_id, clients in (clients_by_model or {}).items():
            model = str(model_id or "").strip().lower()
            if not model:
                continue
            msg = notice_message(model)
            seen_keys: set[str] = set()
            for client in list(clients or ())[:MAX_CLIENTS_PER_MODEL]:
                if not isinstance(client, dict):
                    continue
                keys = _client_keys(
                    client.get("client_ip") or "", client.get("account_alias") or ""
                )
                if not keys:
                    continue
                notice = {
                    "model": model,
                    "message": msg,
                    "created_at": iso,
                    "expires_at": now + NOTICE_TTL_S,
                }
                added_any = False
                for key in keys:
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    bucket = notices.setdefault(key, [])
                    if any(
                        isinstance(n, dict) and n.get("model") == model for n in bucket
                    ):
                        continue
                    # Le MÊME objet est référencé par `ip:` et `acct:` : le
                    # résumé dashboard peut le compter une seule fois.
                    bucket.append(notice)
                    added_any = True
                    if len(bucket) > MAX_NOTICES_PER_CLIENT:
                        del bucket[: len(bucket) - MAX_NOTICES_PER_CLIENT]
                if added_any:
                    created.append({"client_key": ",".join(keys), **notice})
        _prune_notices_locked(now)
    return created


def _prune_notices_locked(now: float) -> None:
    notices = _STATE.get("notices") or {}
    empty: list[str] = []
    for key, bucket in notices.items():
        kept = [
            n
            for n in (bucket or ())
            if isinstance(n, dict) and float(n.get("expires_at") or 0) > now
        ]
        if kept:
            notices[key] = kept
        else:
            empty.append(key)
    for key in empty:
        notices.pop(key, None)


def notice_for(client_ip: str, account_alias: str = "") -> dict | None:
    """Notice la plus récente pour ce client, ou ``None``."""
    if not _STATE.get("notices"):
        return None
    now = time.time()
    with _LOCK:
        _prune_notices_locked(now)
        notices = _STATE.get("notices") or {}
        for key in _client_keys(client_ip, account_alias):
            bucket = notices.get(key) or ()
            if bucket:
                # Le plus récent (les notices sont ajoutées dans l'ordre).
                return dict(bucket[-1])
    return None


def notices_summary() -> list[dict]:
    """Résumé sans IP ni alias, pour l'API dashboard."""
    now = time.time()
    with _LOCK:
        _prune_notices_locked(now)
        counts: dict[str, int] = {}
        seen: set[int] = set()
        for bucket in (_STATE.get("notices") or {}).values():
            for notice in bucket or ():
                if not isinstance(notice, dict):
                    continue
                model = str(notice.get("model") or "")
                if not model:
                    continue
                # Le même objet est partagé entre `ip:` et `acct:`.
                token = id(notice)
                if token in seen:
                    continue
                seen.add(token)
                counts[model] = counts.get(model, 0) + 1
    return [
        {"model": model, "clients": count, "message": notice_message(model)}
        for model, count in sorted(counts.items())
    ]


# ─────────────────────────── Lecture (effort/routage) ───────────────────────


def get_model_entry(model_id: str) -> dict | None:
    mid = str(model_id or "").strip().lower()
    with _LOCK:
        entry = (_STATE.get("available") or {}).get(mid)
        if entry is None:
            entry = (_STATE.get("deprecated") or {}).get(mid)
    return entry if isinstance(entry, dict) else None


def effort_levels(model_id: str) -> list | None:
    """Niveaux autorisés ; ``None`` = aucune information, ``[]`` = aucun effort."""
    entry = get_model_entry(model_id)
    if entry is None:
        return None
    levels = entry.get("effort_levels")
    return list(levels) if isinstance(levels, list) else None


def effort_supported(model_id: str) -> bool | None:
    levels = effort_levels(model_id)
    if levels is None:
        return None
    return bool(levels)


def max_effort(model_id: str, order: list | None = None) -> str | None:
    """Niveau maximal autorisé, ou ``None`` si aucun effort n'est exposé."""
    levels = effort_levels(model_id)
    if not levels:
        return None
    order = list(order or EFFORT_ORDER_DEFAULT)
    known = [level for level in levels if level in order]
    if not known:
        return None
    return max(known, key=order.index)


def reasoning_options(model_id: str) -> list | None:
    entry = get_model_entry(model_id)
    if entry is None:
        return None
    options = entry.get("reasoning_options")
    return list(options) if isinstance(options, list) else None


def thinking_capability(model_id: str) -> dict | None:
    entry = get_model_entry(model_id)
    if entry is None:
        return None
    value = entry.get("thinking")
    return dict(value) if isinstance(value, dict) else None


def metadata(model_id: str) -> dict | None:
    entry = get_model_entry(model_id)
    if entry is None:
        return None
    value = entry.get("metadata")
    return dict(value) if isinstance(value, dict) else None


def is_removed(model_id: str) -> bool:
    """Vrai si le dernier relevé a retiré ce modèle du routage."""
    mid = str(model_id or "").strip().lower()
    with _LOCK:
        return mid in (_STATE.get("retired") or set())


def is_ready() -> bool:
    return bool(_STATE.get("ready"))


def by_leg() -> dict[str, set]:
    with _LOCK:
        return {leg: set((_STATE.get("by_leg") or {}).get(leg) or ()) for leg in LEGS}


def blocked_ids() -> set[str]:
    """Ids qu'une découverte/ajout automatique ne doit pas ressusciter."""
    with _LOCK:
        out = {str(x).strip().lower() for x in (_STATE.get("retired") or ())}
        out.update(str(x).strip().lower() for x in (_STATE.get("deprecated") or ()))
        return out


def is_partial() -> bool:
    return bool(_STATE.get("partial"))


def set_next_refresh(value: str | None) -> None:
    with _LOCK:
        _STATE["next_refresh"] = value


def set_error(message: str | None) -> None:
    with _LOCK:
        _STATE["error"] = message


def snapshot_for_api() -> dict[str, Any]:
    """Vue JSON courte pour ``GET /api/live-models`` (sans secrets)."""
    with _LOCK:
        served = set(_STATE.get("served") or ())
        available = sorted(served)
        deprecated = sorted((_STATE.get("deprecated") or {}).keys())
        effort = {}
        for model_id in available[:MAX_API_MODELS]:
            entry = (_STATE.get("available") or {}).get(model_id) or {}
            levels = entry.get("effort_levels")
            if isinstance(levels, list):
                effort[model_id] = {
                    "levels": list(levels),
                    "max": max_effort(model_id),
                }
        return {
            "revision": int(_STATE.get("revision") or 0),
            "ready": bool(_STATE.get("ready")),
            "partial": bool(_STATE.get("partial")),
            "guarded": bool(_STATE.get("guarded")),
            "last_refresh": _STATE.get("last_refresh"),
            "last_catalog_refresh": _STATE.get("last_catalog_refresh"),
            "next_refresh": _STATE.get("next_refresh"),
            "source": _STATE.get("source"),
            "error": _STATE.get("error"),
            "counts": {
                "available": len(available),
                "detected": len(_STATE.get("available") or ()),
                "deprecated": len(deprecated),
                "added": len(_STATE.get("added") or ()),
                "removed": len(_STATE.get("removed") or ()),
                "migrated": len(_STATE.get("migrated") or ()),
            },
            "available": available,
            "deprecated": deprecated,
            "added": list(_STATE.get("added") or ()),
            "removed": list(_STATE.get("removed") or ()),
            "migrated": list(_STATE.get("migrated") or ()),
            "effort": effort,
            "events": [dict(e) for e in (_STATE.get("events") or ()) if isinstance(e, dict)],
            "notices": notices_summary(),
        }


__all__ = [
    "EFFORT_ORDER_DEFAULT",
    "FREE_LEG",
    "LEGS",
    "MAX_EVENTS",
    "NOTICE_TTL_S",
    "PAID_LEG",
    "apply_snapshot",
    "build_route",
    "build_snapshot",
    "blocked_ids",
    "by_leg",
    "commit_snapshot",
    "effort_levels",
    "effort_levels_from_options",
    "effort_supported",
    "get_model_entry",
    "get_state",
    "is_partial",
    "is_ready",
    "is_removed",
    "load_persisted",
    "max_effort",
    "metadata",
    "notice_for",
    "notice_message",
    "notices_summary",
    "persisted",
    "reasoning_options",
    "record_deprecation_notices",
    "reset_state",
    "set_error",
    "set_next_refresh",
    "snapshot_for_api",
    "thinking_capability",
]
