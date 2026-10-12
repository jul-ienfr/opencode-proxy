"""config.reasoning_options — variantes d'effort autorisées, par modèle.

[PLAN ALIGNEMENT CLIENT OFFICIEL — C1, §5]

**Le divergence.** Le client officiel dérive les variantes d'effort du champ
``reasoning_options`` du catalogue
(``packages/opencode/src/provider/transform.ts:1717-1762``) et n'émet **que**
celles-là. Nous n'avions qu'un plafond (``config/effort_caps.py``), donc deux
erreurs symétriques : un effort envoyable chez nous mais refusé par le modèle
(400), et un effort autorisé chez lui mais inatteignable chez nous.

**Ce qu'on prend.** Une table ``thinking.reasoning_options`` (modèle →
variantes autorisées), même matching **longest-prefix** insensible à la casse
que ``effort_caps``, et le clamp :

    niveau_émis = plus grande valeur autorisée ≤ demandée
    (repli sur la plus petite autorisée si l'intersection est vide)

**Ce qu'on laisse.** Rien n'est appliqué tant que
``thinking.allow_list_enabled`` vaut ``false`` — le **défaut**. À ``false``,
``clamp_effort_to_allow_list`` rend son argument inchangé : le comportement
est celui d'avant, bit pour bit. Le flag se retourne sans redémarrage (lecture
live), et son rollback est immédiat.

**Pourquoi l'allow-list passe en dernier.** ``resolve_effort`` enchaîne
``clamp_level`` (plafond local, filet) **puis** ce clamp. Dans l'autre ordre,
un modèle catalogueant ``max`` seul (``kimi-k3``) mais plafonné par nous à
``high`` repartirait avec ``high`` — hors liste, donc 400. L'allow-list est la
déclaration de ce que l'amont accepte : elle gagne quand les deux parlent. Le
plafond local reste le filet partout où la table se tait, c'est-à-dire partout
tant que la table ne couvre que les 8 modèles du §5.

**Liste vide = aucune information, pas « raisonnement interdit ».** Une
entrée ``[]`` est traitée comme une absence de donnée (pas de clamp). Désactiver
le raisonnement est une décision forte et silencieuse ; la faire sur une liste
vide ambigûe — qui dans le catalogue signifie plutôt « champ non renseigné » —
coûterait plus cher qu'un 400 visible. À revoir en vérif live, avec la trace
d'un vrai refus.

**Ordre total.** Le clamp a besoin d'un ordre ; il lit ``thinking.effort_order``
en live, **sans importer** ``config.effort_caps`` (les modules ``config/*``
restent indépendants, même règle que ``config/limits.py``). Ordre illisible ⇒
l'ordre par défaut local, et un niveau hors ordre connu ressort inchangé.

AUCUN import du projet. ``config/settings.py`` ne doit JAMAIS importer ce
module au top (règle anti-cycles ``config/*``). ``logger`` =
``"config.settings"`` (nom historique préservé).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("config.settings")

#: Nom de la table sous ``thinking`` (modèle → variantes autorisées).
OPTIONS_KEY = "reasoning_options"

#: Nom du flag d'armement. ``false`` par défaut = clamp désactivé.
FLAG_KEY = "allow_list_enabled"

#: Ordre total de repli, identique à ``effort_caps.EFFORT_ORDER_DEFAULT``.
#: Dupliqué à dessein : ce module ne dépend d'aucun autre module du projet.
EFFORT_ORDER_DEFAULT = ["minimal", "low", "medium", "high", "xhigh", "max"]

#: Niveaux qui désactivent le raisonnement — jamais clampés.
#: "off" = vocabulaire thinkingLevel (muse-spark) pour la désactivation.
_DISABLED = ("", "none", "off")

__all__ = [
    "OPTIONS_KEY",
    "FLAG_KEY",
    "allow_list_enabled",
    "clamp_effort_to_allow_list",
    "get_allowed_variants",
]


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


def _effort_order() -> list:
    """Ordre total croissant, lu en live (repli local si illisible)."""
    raw = _yaml_get("thinking", "effort_order", default=None)
    if not isinstance(raw, list) or not raw:
        return list(EFFORT_ORDER_DEFAULT)
    order, seen = [], set()
    for item in raw:
        name = str(item).strip().lower()
        if name and name not in seen:
            seen.add(name)
            order.append(name)
    return order or list(EFFORT_ORDER_DEFAULT)


def _normalize_variants(entry):
    """Variantes normalisées d'une entrée, ou ``None`` si elle ne dit rien.

    Formes acceptées :

        deepseek-v4-pro: [high, max]           # liste directe
        deepseek-v4-pro: high                  # chaîne unique
        deepseek-v4-pro:                       # mapping
          variants: [high, max]

    Tout le reste (``None``, scalaire, ``variants`` non liste, liste vide)
    rend ``None`` — « aucune information », jamais « liste interdite ».
    """
    if entry is None:
        return None
    if isinstance(entry, dict):
        raw = entry.get("variants")
    elif isinstance(entry, (list, tuple, str)):
        raw = entry
    else:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return None
    out, seen = [], set()
    for item in raw:
        name = str(item).strip().lower()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out or None


def allow_list_enabled() -> bool:
    """``thinking.allow_list_enabled`` — ``false`` tant que ce n'est pas dit."""
    return bool(_yaml_get("thinking", FLAG_KEY, default=False))


def get_allowed_variants(model):
    """Variantes autorisées pour ``model`` (longest-prefix), ou ``None``.

    ``None`` n'est **pas** un défaut : il dit « la table ne donne rien pour ce
    modèle », donc pas de clamp — le plafond local reste seul juge.
    """
    table = _yaml_get("thinking", OPTIONS_KEY, default=None)
    if not isinstance(table, dict) or not table:
        return None

    name = str(model or "").strip().lower()
    if not name:
        return _normalize_variants(table.get("default"))

    # Longest-prefix d'abord : indépendant de l'ordre YAML, un préfixe court
    # ne masque jamais un long ("muse-spark-..." ne matche pas "spark").
    prefixes = sorted(
        (str(k).strip().lower() for k in table if str(k).strip().lower() != "default"),
        key=len,
        reverse=True,
    )
    for prefix in prefixes:
        if prefix and name.startswith(prefix):
            variants = _normalize_variants(table.get(prefix))
            if variants is not None:
                return variants
            return _normalize_variants(table.get("default"))
    return _normalize_variants(table.get("default"))


def clamp_effort_to_allow_list(level, model):
    """Ramène ``level`` dans l'allow-list de ``model`` — si le flag est armé.

    - Flag à ``false`` ⇒ ``level`` rendu **inchangé** (comportement d'origine).
    - Aucune entrée pour ce modèle ⇒ inchangé (le plafond local suffit).
    - ``None``, ``none``/``off``, niveau hors ``effort_order`` ⇒ inchangé
      (ce n'est pas à cette couche de valider la demande du client).
    - Sinon : plus grande autorisée ≤ demandée ; à défaut, la plus petite
      autorisée connue de l'ordre total.
    """
    if not allow_list_enabled():
        return level
    if level is None:
        return level

    allowed = get_allowed_variants(model)
    if not allowed:
        return level

    name = str(level).strip().lower()
    if name in _DISABLED:
        return level

    order = _effort_order()
    if name not in order:
        return level

    known = [v for v in allowed if v in order]
    if not known:
        # Allow-list intégralement hors ordre connu : on n'invente rien.
        logger.warning(
            "thinking.%s[%r]=%r hors effort_order, clamp ignoré",
            OPTIONS_KEY,
            model,
            allowed,
        )
        return level

    idx = order.index(name)
    below = [v for v in known if order.index(v) <= idx]
    if below:
        return max(below, key=lambda v: order.index(v))
    return min(known, key=lambda v: order.index(v))
