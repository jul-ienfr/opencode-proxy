"""config.limits — limites de sortie par modèle (config-driven).

[PLAN ALIGNEMENT CLIENT OFFICIEL — V3, écart E2]
Lit en live ``models.<id>.output_limit`` depuis ``config.yaml`` via
``yaml_get`` (lookup dict mémoire, pas d'IO) : aucune snapshot à l'import,
le hot-reload existant (throttle 5 s sur mtime de ``config.yaml``)
s'applique sans toucher aux pollers.

Pourquoi une table et pas une constante : l'officiel envoie
``min(model.limit.output, 32 000)`` sur les trois protocoles
(``transform.ts:18,1481`` → ``anthropic-messages.ts:546``,
``openai-chat.ts:361``, ``openai-responses.ts:493``). Le plafond dur à
32 000 est un **défaut réglable du client officiel**, pas une loi de
l'amont (§14.3 du plan) : le copier régresserait nos autres clients. Ce
qui, en revanche, est une loi de l'amont, c'est ``limit.output`` du
catalogue — au-delà, l'upstream tronque ou refuse. D'où la table : on
borne par la limite **réelle du modèle**, là où le catalogue en donne
une.

Sémantique : ``clamp(demandé, modèle) = min(demandé, limite(modèle))``,
et ``limite(modèle) = None`` quand le catalogue ne dit rien. Table vide
ou modèle absent ⇒ **comportement d'origine bit pour bit** : la fonction
rend la valeur demandée inchangée.

AUCUN import du projet hormis ``yaml_get`` (même pattern que
``config/effort_caps.py``). ``config/settings.py`` ne doit JAMAIS
importer ce module au top (règle anti-cycles ``config/*``).
``logger`` = ``"config.settings"`` (nom historique préservé).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("config.settings")

#: Nom de la clé sous ``models.<id>`` (lisible en live, datée à la main).
LIMIT_KEY = "output_limit"
#: Surcharge locale de la fenêtre de contexte (sinon catalogue).
CONTEXT_KEY = "context_limit"

#: Cache du catalogue de routes (mtime, {model: context_limit}).
_CATALOG_LIMITS_CACHE: tuple[float, dict] | None = None

#: Parité officielle v2 (`model-request.ts:49-57`) : fallback quand le
#: catalogue ne donne rien, cap absolu, plancher du calcul de place.
OUTPUT_TOKEN_FALLBACK = 32000
OUTPUT_TOKEN_MAX = 256000
OUTPUT_TOKEN_MIN = 1024


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


def _models_table() -> dict:
    """Section ``models:`` brute, lue en live ({} si absente/cassée)."""
    raw = _yaml_get("models", default=None)
    return raw if isinstance(raw, dict) else {}


def _coerce_limit(value):
    """Entier strictement positif, ou ``None`` (booléens/str/floats rejetés).

    ``True`` est un ``int`` en Python : un ``output_limit: true`` en YAML
    doit valoir ``None`` et non ``1``, sans quoi une limite de 1 token
    tronquerait toute réponse.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def output_limit(model) -> int | None:
    """Limite de sortie du catalogue pour ``model``, ou ``None``.

    ``None`` n'est **pas** un défaut : il dit « le catalogue ne donne
    aucune limite pour ce modèle », donc pas de clamp. Lookup exact,
    puis reprise insensible à la casse (les ids du catalogue sont déjà
    en minuscules, mais un appelant peut passer une forme mixte).
    """
    name = str(model or "").strip()
    if not name:
        return None
    table = _models_table()
    entry = table.get(name)
    if entry is None and name.lower() != name:
        entry = table.get(name.lower())
    if not isinstance(entry, dict):
        return None
    value = _coerce_limit(entry.get(LIMIT_KEY))
    if value is None and entry.get(LIMIT_KEY) is not None:
        logger.warning(
            "models.%s.%s=%r invalide (entier > 0 attendu), clamp ignoré",
            name,
            LIMIT_KEY,
            entry.get(LIMIT_KEY),
        )
    return value


def clamp_output_token_limit(limit, model):
    """Borne ``limit`` par la limite de sortie réelle du modèle (parité v2).

    - Pas d'entrée pour ce modèle ⇒ ``limit`` rendu **inchangé** (y
      compris ``None`` et les formes non-entières : ce n'est pas à cette
      couche de valider la demande du client).
    - ``limit`` au-dessous de la limite modèle ⇒ inchangé (le client est
      volontairement plus court, on respecte son choix).
    - Au-dessus ⇒ ramené à la limite du modèle.
    - Cap absolu officiel `256 000` dans tous les cas (un 500k catalogue
      ou client ne part jamais tel quel).
    """
    try:
        from config.limits import OUTPUT_TOKEN_MAX as _CAP
    except Exception:
        _CAP = 256000
    cap = output_limit(model)
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return limit
    if isinstance(limit, bool) or value != limit:
        return limit
    if cap is not None and value > cap:
        value = cap
    if value > int(_CAP):
        value = int(_CAP)
    return value


def _catalog_context_limits() -> dict:
    """``{model_id: context_limit}`` depuis ``catalog_routes.json`` (cache mtime)."""
    global _CATALOG_LIMITS_CACHE
    path = Path(__file__).with_name("catalog_routes.json")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _CATALOG_LIMITS_CACHE is not None and _CATALOG_LIMITS_CACHE[0] == mtime:
        return _CATALOG_LIMITS_CACHE[1]
    out: dict[str, int] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for leg in (data.get("providers") or {}).values():
            for model_id, route in (leg or {}).items():
                limit = route.get("limit") if isinstance(route, dict) else None
                if not isinstance(limit, dict):
                    continue
                try:
                    # Parité `calculateCeiling` : `limit.input || limit.context`.
                    ctx = int(limit.get("input") or limit.get("context") or 0)
                except (TypeError, ValueError):
                    ctx = 0
                if ctx > 0 and model_id not in out:
                    out[str(model_id)] = ctx
    except Exception:
        out = {}
    _CATALOG_LIMITS_CACHE = (mtime, out)
    return out


def context_limit(model) -> int | None:
    """Fenêtre de contexte (tokens) du modèle, ou ``None`` si inconnue.

    Priorité : ``models.<id>.context_limit`` (config locale) puis catalogue
    ``catalog_routes.json``. Sert au déclencheur proactif officiel
    ``context_limit - buffer``.
    """
    name = str(model or "").strip()
    if not name:
        return None
    table = _models_table()
    entry = table.get(name)
    if entry is None and name.lower() != name:
        entry = table.get(name.lower())
    if isinstance(entry, dict):
        value = _coerce_limit(entry.get(CONTEXT_KEY))
        if value is not None:
            return value
    catalog = _catalog_context_limits()
    return catalog.get(name) or catalog.get(name.lower())


def output_limit_with_fallback(model) -> int:
    """Limite modèle v2 : `min(catalogue.output ou 32k fallback, 256k cap)`."""
    cap = output_limit(model)
    base = cap if cap is not None else OUTPUT_TOKEN_FALLBACK
    return base if base <= OUTPUT_TOKEN_MAX else OUTPUT_TOKEN_MAX


__all__ = [
    "CONTEXT_KEY",
    "LIMIT_KEY",
    "OUTPUT_TOKEN_FALLBACK",
    "OUTPUT_TOKEN_MAX",
    "OUTPUT_TOKEN_MIN",
    "clamp_output_token_limit",
    "context_limit",
    "output_limit",
    "output_limit_with_fallback",
]
