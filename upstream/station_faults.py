"""V6.3.2 — Attribuer une panne à une station, et seulement quand c'est elle.

**Le probleme que ce module tranche.** Un `403` free ne dit pas qui est fautif.
Deux causes opposées y ressemblent :

* la **sortie** est refusée (IP cramée, tunnel mort) -> mettre *cette* station de
  côté est la bonne réponse, et les autres continuent de servir ;
* la **requête** n'a pas la forme attendue (pas de `stream`, pas de shim) ->
  écarter la station ne sert à rien : les cinq échoueront pareil, et on aura
  consommé du quota de rotation pour rien.

La session a mesuré cette confusion une première fois (V6.3.1) : un `403` de
forme déclenchait la même rotation qu'un quota épuisé.

**La règle, et c'est la seule chose que ce module ajoute :**

| Signal | Attribuable à la station ? |
|---|---|
| facteur `egress` en échec | **oui** — l'IP est en cause |
| `egress` **et** `body`/`identity` en échec | **non** — la requête est fautive aussi, on ne peut pas accuser l'IP |
| timeout / connexion impossible | oui — la station ne répond pas |
| 5xx de transit | oui, mais brièvement : c'est souvent passager |
| `body` / `identity` seuls | **non** |
| quota (`429`) | traité ailleurs (`on_quota_exhausted`), pas ici |

**Ce que ce module ne fait pas.** Il ne tient aucun état de disponibilité : les
cooldowns vivent dans la pool (`_set_free_cooldown`), qui est la seule source de
vérité. Il fournit la **politique** (qui accuser, combien de temps) et un
**compteur** d'observabilité. Dupliquer l'état aurait créé deux vérités qui
divergent — le défaut que cette session a corrigé trois fois.
"""

from __future__ import annotations

__all__ = [
    "STATION_FAULTS",
    "attributable_fault",
    "cooldown_seconds",
    "reset_station_fault_stats",
    "station_fault_stats",
    "note_station_fault",
]

#: Pannes qu'on peut imputer à une station.
STATION_FAULTS = ("egress", "timeout", "connect", "transit")

#: Cooldown par panne, en secondes. Volontairement court pour `transit` (une
#: surcharge passagère ne mérite pas de perdre une station) et plus long pour
#: `egress` (une IP refusée le reste un moment).
_COOLDOWNS = {
    "egress": 300.0,
    "timeout": 60.0,
    "connect": 60.0,
    "transit": 45.0,
}

_MIN_COOLDOWN = 5.0
_MAX_COOLDOWN = 600.0

_STATS: dict[str, int] = {}
_STATS_MAX_KEYS = 200


def attributable_fault(gate_factors=(), fault: str | None = None) -> str | None:
    """Nomme la panne si elle est imputable à la station, sinon ``None``.

    ``gate_factors`` : facteurs en échec du diagnostic de porte (V6.1.2).
    ``fault`` : panne constatée hors porte (``"timeout"``, ``"connect"``,
    ``"transit"``).

    L'ordre des règles porte tout le sens : `egress` est examiné **avant** tout
    le reste, parce qu'un refus de sortie est le seul signal qui désigne l'IP.
    Si `body` ou `identity` échoue *en plus*, on renvoie ``None`` : on ne peut
    pas prouver que l'IP est fautive quand la requête l'est aussi.
    """
    try:
        factors = {str(f) for f in (gate_factors or ())}
    except Exception:
        factors = set()

    if "egress" in factors:
        # Requête également malformée : l'accusation n'est pas démontrable.
        if "body" in factors or "identity" in factors:
            return None
        return "egress"

    if factors & {"body", "identity"}:
        # Refus de forme : la rotation ne réparerait rien.
        return None

    if fault is None:
        return None
    name = str(fault).strip().lower()
    return name if name in STATION_FAULTS else None


def cooldown_seconds(fault: str | None) -> float:
    """Durée de mise à l'écart d'une station pour cette panne (bornée)."""
    try:
        raw = _COOLDOWNS.get(str(fault or "").strip().lower())
    except Exception:
        raw = None
    if raw is None:
        return 0.0
    return max(_MIN_COOLDOWN, min(_MAX_COOLDOWN, float(raw)))


def note_station_fault(station_id, fault: str) -> None:
    """Compte les pannes par station et par cause (fail-soft, borné)."""
    try:
        key = f"{station_id if station_id is not None else '?'}:{fault}"
        if key not in _STATS and len(_STATS) >= _STATS_MAX_KEYS:
            return
        _STATS[key] = int(_STATS.get(key, 0)) + 1
    except Exception:
        pass


def station_fault_stats() -> dict:
    """``{"<station>:<cause>": occurrences}``, du plus fréquent au moins fréquent."""
    try:
        return dict(sorted(_STATS.items(), key=lambda kv: -kv[1]))
    except Exception:
        return {}


def reset_station_fault_stats() -> None:
    try:
        _STATS.clear()
    except Exception:
        pass
