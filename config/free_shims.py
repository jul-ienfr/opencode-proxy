"""V6.2.1 — registre déclaratif des shims de la jambe gratuite.

**Le problème.** Les deux shims qui font passer la porte d'inférence vivaient en
dur dans `opencode.py` : impossible de les ajuster sans toucher au proxy, et
surtout impossible de dire **lesquels sont nécessaires**. Or l'audit a mesuré
une asymétrie qui compte :

  * `read` avec une définition réaliste → **200** (nécessaire) ;
  * `bash` seul, `edit` seul → **403** (insuffisants) ;
  * un outil inconnu mais plausible → **200** (tolérés en extras).

Autrement dit le registre ne contient pas « des outils utiles » : il contient
« le minimum qui satisfait la grille ». Cette distinction doit être écrite dans
les données, pas devinée dans le code.

**Ce que ce module apporte.**
  * les définitions vivent dans `config.yaml > free.shims`, avec un champ
    `necessity` explicite (`required` / `optional`) ;
  * une définition invalide est **écartée avec sa raison**, jamais silencieusement
    propagée (une description trop courte fait un 403 : autant le dire au
    chargement) ;
  * les valeurs par défaut restent dans le code, en repli — un `config.yaml`
    amputé ne casse jamais la porte.

**Ce qu'il ne fait pas.** Il ne décide pas *quels* shims envoyer : c'est la
politique du proxy (`_free_shim_tool`). Il décrit seulement ce qui existe et
pourquoi.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_SHIMS",
    "MIN_DESCRIPTION",
    "NECESSITY_VALUES",
    "load_shims",
    "necessity",
    "shims",
    "validate_entry",
]

#: Longueur minimale d'une description. Aligné sur la règle de la porte
#: (`config/free_gate.py: MIN_SHIM_DESCRIPTION`) : une description générique
#: type « Do not use this tool » vaut un 403, donc on refuse de charger court.
MIN_DESCRIPTION = 40

NECESSITY_VALUES = ("required", "optional")

#: Repli : les définitions mesurées comme fonctionnelles sur la passerelle.
#: `read` est marqué `required` : la bisection l'a prouvé nécessaire. Il n'y a
#: plus qu'un shim par défaut — `bash` a été retiré par mesure (V6.2.2), pas par
#: goût : il ne remplace pas `read` et ne change pas le verdict en sa présence.
DEFAULT_SHIMS = {
    # [V6.2.2] `bash` a été RETIRÉ après mesure (2026-10-21) : sans outil ->
    # 403, `read` seul -> 200, `bash` seul -> 403, `read` + `bash` -> 200.
    # Il n'est donc load-bearing à aucun titre. Réintroduisible par
    # `config.yaml > free.shims` si une mesure future le réhabilite.
    "read": {
        "necessity": "required",
        "description": (
            "Reads a file from the filesystem and returns its contents. "
            "Use offset and limit to page through large files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filePath": {"type": "string", "description": "Absolute path of the file to read"},
                "offset": {"type": "integer", "description": "First line to read (1-indexed)"},
                "limit": {"type": "integer", "description": "Maximum lines to return"},
            },
            "required": ["filePath"],
        },
    },
}


def validate_entry(name: str, entry) -> tuple[dict | None, str]:
    """Valide une entrée de registre. Rend ``(entrée normalisée | None, raison)``.

    La raison est vide quand l'entrée est acceptée : un chargeur qui écarte des
    entrées doit pouvoir expliquer lesquelles et pourquoi.
    """
    if not isinstance(name, str) or not name.strip():
        return None, "nom d'outil vide"
    if not isinstance(entry, dict):
        return None, "entrée qui n'est pas un mapping"

    description = entry.get("description")
    if not isinstance(description, str) or len(description.strip()) < MIN_DESCRIPTION:
        # La porte rejette les descriptions courtes/placeholder : charger une
        # telle définition ferait échouer chaque requête free avec un 403 muet.
        return None, f"description trop courte (< {MIN_DESCRIPTION} caractères)"

    parameters = entry.get("parameters")
    if not isinstance(parameters, dict):
        return None, "paramètres absents"
    if parameters.get("type") != "object":
        return None, "paramètres sans type: object"
    properties = parameters.get("properties")
    if not isinstance(properties, dict) or not properties:
        # Un `properties: {}` vide est un 403 même avec une description réaliste.
        return None, "propriétés vides"

    necessity = str(entry.get("necessity", "optional")).strip().lower()
    if necessity not in NECESSITY_VALUES:
        return None, f"necessity inconnue ({necessity!r})"

    return {
        "necessity": necessity,
        "description": description.strip(),
        "parameters": parameters,
    }, ""


def load_shims(raw=None) -> dict:
    """Fusionne les définitions par défaut et celles de la configuration.

    ``raw`` : le mapping `config.yaml > free.shims` (ou ``None``). Une entrée
    invalide est ignorée — elle ne remplace jamais un défaut valide.
    """
    merged = {name: dict(spec) for name, spec in DEFAULT_SHIMS.items()}
    if not isinstance(raw, dict):
        return merged
    for name, entry in raw.items():
        clean, _reason = validate_entry(name, entry)
        if clean is not None:
            merged[str(name)] = clean
    return merged


_CACHE: dict | None = None


def shims() -> dict:
    """Registre effectif, lu une fois depuis la configuration."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    raw = None
    try:
        from config.settings import yaml_get

        raw = yaml_get("free", "shims", None)
    except Exception:
        raw = None
    _CACHE = load_shims(raw)
    return _CACHE


def necessity(name: str) -> str:
    """`required`, `optional`, ou `""` si le nom est inconnu."""
    entry = shims().get(str(name))
    return str(entry.get("necessity", "")) if entry else ""


def reset_cache() -> None:
    """Vide le cache (tests, ou après édition de la configuration)."""
    global _CACHE
    _CACHE = None
