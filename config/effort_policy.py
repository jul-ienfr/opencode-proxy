"""config.effort_policy — SOURCE UNIQUE DE VÉRITÉ pour la notion d'effort.

[PLAN_AUDIT_CONVERSIONS Lot L2/L9/L10 — traite A1, A2, A4]

Avant ce module, la même notion d'effort était calculée par **quatre** codes
divergents :

1. ``mapping.py::_effort_to_reasoning`` + ``config.effort_caps.clamp_effort`` (P2)
2. ``mapping.py::openai_to_anthropic_request`` — dict codé en dur
   ``{low: 4096, medium: 10000, high: 16000}``, tout le reste replié sur 16000 (P4)
3. ``mapping.py::openai_responses_to_anthropic`` — 3ᵉ table (P6)
4. le handler ``/v1/chat/completions`` — 4ᵉ mapping **par famille de modèle**
   (``glm-5*``, ``deepseek-v4*``, sinon), **sans plafond modèle** (P3)

Un même ``effort: high`` ne produisait donc pas la même chose selon la porte
d'entrée. Ce module remplace les quatre par un seul raisonnement.

Vocabulaire d'entrée accepté (toutes les formes réellement observées) :

    output_config.effort   forme ACTUELLE documentée (Anthropic Messages)
    effort                 forme historique top-level
    reasoning_effort       nom OpenAI (client Chat direct)
    reasoning.effort       nom Responses API
    thinking.type          ``enabled`` | ``adaptive`` | ``disabled``
    thinking.budget_tokens forme legacy, convertie par la table unique

Politique de sortie : le niveau est **reconnu nommément** (``minimal``
compris — A2) puis **plafonné par modèle** via ``config.effort_caps``. Aucun
repli « famille de modèle » codé en dur : le plafond est de la config.

Contrainte de boot (plan §7.4) : **aucun import lourd**. Ce module n'importe
que ``config.effort_caps`` (lui-même réduit à ``yaml_get``) et la stdlib —
jamais httpx/rich/tiktoken/click.
"""

from __future__ import annotations

from typing import NamedTuple

__all__ = [
    "EffortDecision",
    "BUDGET_TO_LEVEL_TABLE",
    "DEFAULT_LEVEL",
    "MODEL_MAX",
    "budget_to_level",
    "extract_requested_level",
    "get_effort_order",
    "clamp_level",
    "max_level_for_model",
    "normalize_level",
    "resolve_effort",
]

#: Niveau retenu quand le client demande du raisonnement SANS préciser de
#: niveau (``thinking.adaptive`` sans budget, ``enabled`` sans budget_tokens) :
#: on vise le **plafond du modèle**, c'est-à-dire le maximum qu'il sait faire.
#: Voir ``MODEL_MAX``. ``DEFAULT_LEVEL`` n'est plus qu'un repli technique, utilisé
#: quand la configuration est illisible (jamais en fonctionnement normal).
DEFAULT_LEVEL = "high"

#: Sentinelle interne : « le client veut du raisonnement, sans nommer de
#: niveau ». ``resolve_effort`` la traduit en plafond du modèle cible — la
#: décision dépend donc du modèle (« adapté à chaque modèle »), et un modèle
#: plafonné à ``high`` reçoit ``high`` là où un modèle plafonné à ``max``
#: reçoit ``max``. Ne jamais la laisser fuir hors de ``resolve_effort``.
MODEL_MAX = "__model_max__"

#: Table UNIQUE budget ↔ niveau (A1). Ordre décroissant : première borne
#: satisfaite gagne. Le dernier palier couvre tout budget > 0.
#:
#: C'est la table de ``mapping.py`` (P2) qui fait foi : elle était la seule à
#: porter ``xhigh``. Les deux tables concurrentes (P4 : 4096/10000/16000 avec
#: repli global sur 16000 ; P6 : idem) sont supprimées.
BUDGET_TO_LEVEL_TABLE = (
    (16000, "xhigh"),
    (10000, "high"),
    (4000, "medium"),
    (1, "low"),
)

#: Champs d'entrée portant un niveau explicite, par ordre de priorité.
#: ``output_config.effort`` est la forme ACTUELLE documentée — elle prime.
_EXPLICIT_LEVEL_FIELDS = ("output_config.effort", "effort", "reasoning_effort", "reasoning.effort")


class EffortDecision(NamedTuple):
    """Décision d'effort résolue, identique pour les 6 chemins.

    - ``wants``  : le raisonnement est-il demandé du tout ?
    - ``level``  : niveau résolu et plafonné, ``None`` si désactivé.
    - ``source`` : d'où vient la décision (diagnostic/logs uniquement).
    - ``explicit`` : le client a-t-il nommé un niveau (vs dérivé d'un budget) ?
    """

    wants: bool
    level: str | None
    source: str
    explicit: bool


def _cfg():
    """Accès paresseux à ``config.effort_caps`` (jamais d'échec à l'import)."""
    try:
        from config import effort_caps
    except ImportError:  # pragma: no cover — config absente (tests isolés)
        return None
    return effort_caps


def normalize_level(value) -> str | None:
    """Normalise un niveau brut en minuscules ; ``None`` si vide/non-str."""
    if value is None or not isinstance(value, str):
        return None
    level = value.strip().lower()
    return level or None


def get_effort_order() -> list:
    """Ordre total des niveaux, lu en live (config), avec repli."""
    caps = _cfg()
    if caps is None:  # pragma: no cover
        return ["minimal", "low", "medium", "high", "xhigh", "max"]
    return caps.get_effort_order()


def budget_to_level(budget) -> str | None:
    """Table budget → niveau (UNIQUE). ``None`` si budget absent/nul/invalide."""
    try:
        value = int(budget)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    for threshold, level in BUDGET_TO_LEVEL_TABLE:
        if value >= threshold:
            return level
    return "low"


def _read_field(fields: dict, dotted: str):
    """Lit un champ, y compris imbriqué (``output_config.effort``)."""
    if not isinstance(fields, dict):
        return None
    if "." not in dotted:
        return fields.get(dotted)
    head, _, tail = dotted.partition(".")
    nested = fields.get(head)
    if isinstance(nested, dict):
        return nested.get(tail)
    return None


def extract_requested_level(fields: dict) -> tuple[str | None, str, bool]:
    """Extrait le niveau demandé de **n'importe quelle** forme cliente.

    Retourne ``(level, source, explicit)`` :
    - niveau explicite trouvé → ``(niveau, "output_config.effort", True)`` ;
    - sinon ``thinking`` → niveau dérivé du budget, sinon ``MODEL_MAX``
      (sentinelle : « le maximum du modèle », résolue par ``resolve_effort``) ;
    - sinon ``(None, "absent", False)``.

    ``"none"``/``"disabled"`` sont des demandes explicites de DÉSACTIVATION :
    retournés comme ``(None, "disabled", True)``.
    """
    for dotted in _EXPLICIT_LEVEL_FIELDS:
        level = normalize_level(_read_field(fields, dotted))
        if level:
            if level in ("none", "disabled"):
                return None, "disabled", True
            return level, dotted, True

    thinking = fields.get("thinking") if isinstance(fields, dict) else None
    if isinstance(thinking, dict):
        ttype = normalize_level(thinking.get("type")) or ""
        if ttype == "disabled":
            return None, "thinking.disabled", True
        derived = budget_to_level(thinking.get("budget_tokens"))
        if derived:
            return derived, "thinking.budget_tokens", False
        if ttype in ("enabled", "adaptive"):
            # Raisonnement demandé SANS niveau ni budget : le client veut « le
            # maximum que le modèle sait faire ». On ne peut pas le résoudre ici
            # (on ignore ``model``) → sentinelle traduite par ``resolve_effort``
            # en plafond du modèle cible.
            return MODEL_MAX, "thinking." + ttype, False

    return None, "absent", False


def max_level_for_model(model: str) -> str | None:
    """Plafond d'effort du modèle — le maximum qu'il sait faire.

    ``None`` si la configuration est illisible (l'appelant retombe alors sur
    ``DEFAULT_LEVEL``). Lecture live de ``config.effort_caps``.
    """
    caps = _cfg()
    if caps is None:  # pragma: no cover
        return None
    try:
        cap = caps.get_max_effort_for_model(model)
    except Exception:  # pragma: no cover — config cassée : jamais de 500
        return None
    cap = normalize_level(cap)
    return cap if cap in get_effort_order() else None


def clamp_level(level: str | None, model: str) -> str | None:
    """Plafonne un niveau au maximum autorisé pour ``model`` (config-driven).

    Même sémantique que ``effort_caps.clamp_effort`` : ``min(demandé, cap)``
    sur l'ordre total. ``None`` → ``None``. Niveau inconnu → inchangé
    (robustesse forward : un futur niveau n'est pas écrasé).
    """
    if level is None:
        return None
    caps = _cfg()
    if caps is None:  # pragma: no cover
        return level
    try:
        return caps.clamp_effort(level, model)
    except Exception:  # pragma: no cover — config cassée : jamais de 500
        return level


def resolve_effort(
    fields: dict,
    model: str,
    *,
    default_when_unspecified: str | None = None,
) -> EffortDecision:
    """Résout l'effort d'une requête — **le** point d'entrée unique.

    Args:
        fields: corps de requête (ou tout dict portant les champs d'effort).
        model: identifiant du modèle cible (pour le plafond).
        default_when_unspecified: niveau à retenir si le client n'exprime
            RIEN. ``None`` (défaut) = pas de raisonnement.

    Returns:
        ``EffortDecision`` — identique pour les 6 chemins à entrée égale.
    """
    level, source, explicit = extract_requested_level(fields or {})

    if level == MODEL_MAX:
        # « Le maximum du modèle » : décision différée au modèle cible. Le
        # plafond est le bon niveau par construction — inutile de clamper.
        cap = max_level_for_model(model) or DEFAULT_LEVEL
        return EffortDecision(wants=True, level=cap, source=source, explicit=explicit)

    if level is None and not explicit and default_when_unspecified:
        level = normalize_level(default_when_unspecified)
        source = "default"

    if level is None:
        return EffortDecision(wants=False, level=None, source=source, explicit=explicit)

    clamped = clamp_level(level, model)
    if clamped is None:
        # Le plafond du modèle désactive le raisonnement.
        return EffortDecision(wants=False, level=None, source=source, explicit=explicit)
    return EffortDecision(wants=True, level=clamped, source=source, explicit=explicit)
