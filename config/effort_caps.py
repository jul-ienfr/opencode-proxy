"""config.effort_caps — plafonds d'effort de raisonnement par modèle (config-driven).

Lit en live ``thinking.effort_order`` / ``thinking.effort_caps`` depuis
``config.yaml`` via ``yaml_get`` (lookup dict mémoire, pas d'IO) : aucune
snapshot à l'import, le hot-reload existant (throttle 5 s sur mtime de
``config.yaml``) s'applique sans toucher aux pollers.

Sémantique : ``clamp(demandé, modèle) = min(demandé, cap(modèle))`` sur
l'ordre total ``effort_order``. Si un client demande au-delà du plafond
du modèle (ex. ``max`` sur un modèle plafonné à ``high``), l'effort est
relegué au plafond du modèle.

AUCUN import du projet hormis ``yaml_get`` (même pattern que
``app/protocol/mapping.py``). ``config/settings.py`` ne doit JAMAIS
importer ce module au top (règle anti-cycles ``config/*``).
``logger`` = ``"config.settings"`` (nom historique préservé).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("config.settings")

EFFORT_ORDER_DEFAULT = ["minimal", "low", "medium", "high", "xhigh", "max"]

#: Comportement pré-patch si la config est absente ou cassée (branche défaut historique).
DEFAULT_CAP_FALLBACK = "high"

#: Niveaux qui désactivent le raisonnement — jamais clampés, passthrough None.
_DISABLED = ("", "none")


def _yaml_get(*keys, default=None):
    # Import tardif : évite tout cycle si config.settings venait un jour
    # à importer ce module (interdit au top, toléré via appel).
    try:
        from config import yaml_get
    except ImportError:  # pragma: no cover
        return default
    try:
        return yaml_get(*keys, default=default)
    except TypeError:  # signature yaml_get inattendue
        return default


def get_effort_order() -> list:
    """Ordre total des niveaux (croissant), lu en live, validé."""
    raw = _yaml_get("thinking", "effort_order", default=None)
    if not isinstance(raw, list) or not raw:
        return list(EFFORT_ORDER_DEFAULT)
    order = []
    seen = set()
    for item in raw:
        name = str(item).strip().lower()
        if name and name not in seen:
            seen.add(name)
            order.append(name)
    # Garde-fou : un ordre custom doit au moins porter low/medium/high.
    if not {"low", "medium", "high"} <= seen:
        logger.warning(
            "thinking.effort_order invalide (%r), ordre par défaut utilisé", raw
        )
        return list(EFFORT_ORDER_DEFAULT)
    return order


def get_effort_caps() -> dict:
    """Dict brut ``thinking.effort_caps`` lu en live (clés/valeurs brutes)."""
    raw = _yaml_get("thinking", "effort_caps", default=None)
    return dict(raw) if isinstance(raw, dict) else {}


def get_max_effort_for_model(model) -> str:
    """Plafond d'effort pour un modèle (matching longest-prefix, insensible casse)."""
    order = get_effort_order()
    caps = get_effort_caps()

    def _valid_cap(value) -> str | None:
        name = str(value).strip().lower() if value is not None else ""
        return name if name in order else None

    default_cap = _valid_cap(caps.get("default")) or DEFAULT_CAP_FALLBACK

    name = str(model or "").strip().lower()
    if not name:
        return default_cap
    # Longest-prefix d'abord : indépendant de l'ordre YAML, un préfixe
    # court ne masque jamais un long ("muse-spark-..." ne matche pas "spark").
    prefixes = sorted(
        (str(k).strip().lower() for k in caps if str(k).strip().lower() != "default"),
        key=len,
        reverse=True,
    )
    for prefix in prefixes:
        if prefix and name.startswith(prefix):
            cap = _valid_cap(caps.get(prefix))
            if cap is None:
                logger.warning(
                    "thinking.effort_caps[%r]=%r invalide, fallback default (%s)",
                    prefix,
                    caps.get(prefix),
                    default_cap,
                )
                return default_cap
            return cap
    return default_cap


def clamp_effort(effort, model):
    """Relegue l'effort demandé au plafond du modèle. Retourne None si désactivé.

    - ``None``/``""``/``"none"`` (insensible casse) → ``None`` (raisonnement désactivé).
    - Niveau inconnu (hors ``effort_order``) → inchangé (robustesse forward).
    - Sinon ``min(demandé, cap(modèle))`` sur l'ordre ; en-dessous du cap → inchangé.
    """
    if effort is None:
        return None
    level = str(effort).strip().lower()
    if level in _DISABLED:
        return None
    order = get_effort_order()
    if level not in order:
        return effort
    cap = get_max_effort_for_model(model)
    if cap not in order:
        cap = DEFAULT_CAP_FALLBACK
    if order.index(level) > order.index(cap):
        return cap
    return level
