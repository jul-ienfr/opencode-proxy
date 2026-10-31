"""Politique de substitution payant → gratuit (pure, sans I/O).

Deux modes, choisis dans le GUI (``ip_rotation.free_substitution_policy``) :

* ``homonym`` (défaut) — un modèle payant n'est servi automatiquement que par
  son homonyme ``X-free``. Aucune autre substitution automatique.
* ``family`` — un free de la même famille est accepté automatiquement
  (``mimo-v2.5`` → ``mimo-v2.6-flash-free``, ``muse-spark-1.2-…`` →
  ``muse-spark-1.3-…-free``), jamais une autre famille (Mimo→DeepSeek).

Portée : la politique ne s'applique qu'aux **candidats automatiques**
(``X-free`` et ``ip_rotation.free_model_candidates``). Une entrée explicite de
``free_model_map`` est une décision humaine (comme ``custom_routes``) et reste
toujours souveraine, quel que soit le mode. Sans cette distinction, changer de
politique invalidait silencieusement des mappings déployés — mesuré le
2026-10-29 : ``glm-5.1 → glm-5.3-flash-free`` ignoré, les alias Claude
partaient sur la jambe payante et rendaient 401 avec une clé morte.

La famille est le préfixe avant le premier segment versionné (chiffre) :
``mimo-v2.5`` → ``mimo``, ``muse-spark-1.2-contributor`` → ``muse-spark``.
"""

from __future__ import annotations

import re

POLICIES = ("homonym", "family")
DEFAULT = "homonym"


def normalize_policy(raw, default: str = DEFAULT) -> str:
    value = str(raw or "").strip().lower()
    return value if value in POLICIES else default


def family_of(model_id) -> str:
    parts = [p for p in re.split(r"[-_]+", str(model_id or "").strip().lower()) if p]
    family = []
    for part in parts:
        if any(ch.isdigit() for ch in part):
            break
        family.append(part)
    return "-".join(family)


def target_allowed(paid_model, target, policy: str = DEFAULT) -> bool:
    """Candidat **automatique** autorisé par la politique.

    Ne pas utiliser pour une entrée explicite de ``free_model_map`` : celle-ci
    est toujours honorée (voir ``is_free_subject`` et la docstring du module).
    """
    paid = str(paid_model or "").strip().lower()
    tgt = str(target or "").strip().lower()
    if not paid or not tgt:
        return False
    # Un modèle free sans suffixe (ex. ``big-pickle``) est sa propre cible :
    # la chaîne de candidats ne doit pas être vide, sinon le garde strict_free
    # conclut « toutes les stations épuisées » sans tenter l'upstream (429
    # fabriqué mesuré le 2026-10-29).
    if paid == tgt:
        return True
    if paid.endswith("-free"):
        return tgt == paid
    if tgt == f"{paid}-free":
        return True
    if normalize_policy(policy) == "family":
        family = family_of(paid)
        return bool(family) and family == family_of(tgt)
    return False


def is_free_subject(model, free_models=None, free_model_map=None, policy: str = DEFAULT) -> bool:
    """Le modèle est-il servi par la voie « free » (tunnel obligatoire) ?

    Cœur pur de ``opencode._is_free_model_route`` : une entrée **explicite** de
    ``free_model_map`` est souveraine et rend ``True`` sans consulter la
    politique, puis suffixe ``-free``, puis présence dans le pool découvert.
    ``policy`` reste accepté pour compatibilité d'appel mais ne filtre jamais
    un mapping explicite. Un modèle inconnu/illisible rend ``False`` — on ne
    bloque jamais par défaut.
    """
    raw = str(model or "").strip()
    if not raw:
        return False
    try:
        mapping = (free_model_map or {}).get(raw)
        if mapping:
            return True
        if raw.endswith("-free"):
            return True
        return raw in {str(x).strip() for x in (free_models or ()) if str(x).strip()}
    except Exception:
        return raw.endswith("-free")


def effective_map(free_model_map=None, policy: str = DEFAULT) -> dict:
    """``FREE_MODEL_MAP`` **effective** : toutes les entrées explicites.

    ``policy`` n'entre plus en jeu ici — elle ne concerne que les candidats
    automatiques (``target_allowed``). Le paramètre reste accepté pour les
    appelants existants.
    """
    if not isinstance(free_model_map, dict):
        return {}
    return dict(free_model_map)


__all__ = [
    "DEFAULT",
    "POLICIES",
    "effective_map",
    "family_of",
    "is_free_subject",
    "normalize_policy",
    "target_allowed",
]
