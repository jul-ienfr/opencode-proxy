"""Classification des échecs de la jambe free (V6.3.1).

**Pourquoi classer.** Aujourd'hui un échec free est traité comme « un 429 » :
la même réaction (cooldown + rotation de station) s'applique à trois causes qui
n'ont rien à voir entre elles.

| Cause | Ce que dit l'amont | Ce qu'il faut faire |
|---|---|---|
| `gate` | 403 : la requête n'a pas la forme attendue | **tourner ne sert à rien** — c'est le corps/les en-têtes, pas l'IP |
| `quota` | 429 | tourner utile, avec un cooldown honnête |
| `transit` | 5xx / 502 / 503 / 504 | cooldown court, retenter la même station peut suffire |
| `auth` | 401 « Missing API key » | le modèle est fermé aux anonymes : inutile de réessayer |
| `model` | 400 « Model is unavailable » | idem : le modèle est éteint, pas la station |
| `empty` | 200 puis aucun octet | défaut de flux (V5.1), pas de quota |

**Ce que ce module fait, et ce qu'il ne fait pas.** Il *classifie* et il
*compte*. Il ne change encore aucune décision : c'est un préalable. Changer la
politique de reprise sans mesure reviendrait à deviner — et cette session a
assez montré ce que valent les déductions non mesurées.

**Pourquoi `gate` et `quota` ne doivent pas être confondus.** Une rotation de
station coûte cher (tunnel, latence, quota d'IP). La déclencher sur un refus de
forme est un gaspillage pur : la même requête échouera sur les cinq stations.
C'est l'erreur que la classification rend visible.
"""

from __future__ import annotations

__all__ = [
    "FREE_FAILURE_KINDS",
    "classify_free_failure",
    "free_failure_stats",
    "gate_factor_stats",
    "note_free_failure",
    "note_gate_factors",
    "reset_free_failure_stats",
]

#: Les classes reconnues. Toute autre valeur retombe sur ``unknown``.
FREE_FAILURE_KINDS = (
    "gate",
    "quota",
    "transit",
    "auth",
    "model",
    "empty",
    "unknown",
)

#: Fragments de corps qui trahissent une cause précise, une fois le statut lu.
#: Volontairement minuscules : ce sont des messages d'amont stables.
_GATE_MARKERS = (
    "free tier can only be used",
    "within opencode",
    "data policy",
    "datapolicy",
)
_AUTH_MARKERS = ("missing api key", "unauthorized")
_MODEL_MARKERS = ("model is unavailable", "model not found", "unavailable")

_STATS: dict[str, int] = {}
_STATS_MAX_KEYS = 200


def classify_free_failure(status: int | None, body: str | bytes | None = None) -> str:
    """Classe un échec free en ``gate``/``quota``/``transit``/``auth``/``model``/``empty``.

    Le statut prime ; le corps ne sert qu'à trancher les cas où le statut est
    ambigu (un 403 « data policy » et un 403 « clé invalide » n'appellent pas la
    même réaction, un 400 « modèle éteint » non plus).

    Pur : aucune I/O, aucun état lu — testable et utilisable dans n'importe
    quelle couche sans dépendance.
    """
    try:
        code = int(status) if status is not None else 0
    except (TypeError, ValueError):
        code = 0

    text = ""
    if isinstance(body, (bytes, bytearray)):
        text = bytes(body).decode("utf-8", "replace").lower()
    elif isinstance(body, str):
        text = body.lower()

    if code == 0:
        # Pas de statut : on ne peut se fier qu'au corps. Les marqueurs
        # explicites priment ; un corps muet est un défaut de flux.
        if any(m in text for m in _AUTH_MARKERS):
            return "auth"
        if any(m in text for m in _MODEL_MARKERS):
            return "model"
        if any(m in text for m in _GATE_MARKERS):
            return "gate"
        return "empty" if text else "unknown"
    if code == 200:
        # Un 200 sans contenu est un défaut de flux, pas un refus.
        return "empty"
    if code == 429:
        return "quota"
    if code >= 500:
        return "transit"
    if code == 401:
        return "auth"
    if code == 403:
        return "gate"
    if code == 400:
        return "model" if any(m in text for m in _MODEL_MARKERS) else "gate"
    if any(m in text for m in _AUTH_MARKERS):
        return "auth"
    if any(m in text for m in _MODEL_MARKERS):
        return "model"
    if any(m in text for m in _GATE_MARKERS):
        return "gate"
    return "unknown"


def note_free_failure(kind: str) -> None:
    """Compteur par classe d'échec (fail-soft, borné)."""
    try:
        key = kind if kind in FREE_FAILURE_KINDS else "unknown"
        if key not in _STATS and len(_STATS) >= _STATS_MAX_KEYS:
            return
        _STATS[key] = int(_STATS.get(key, 0)) + 1
    except Exception:
        pass


def free_failure_stats() -> dict:
    """Vue triée par fréquence, pour ``/health?detail=full``."""
    try:
        return dict(sorted(_STATS.items(), key=lambda kv: -kv[1]))
    except Exception:
        return {}


def reset_free_failure_stats() -> None:
    """Réinitialise les compteurs (tests, ou rotation d'observation)."""
    try:
        _STATS.clear()
        _GATE_FACTORS.clear()
    except Exception:
        pass


# ── V6.1.2 : quel facteur de la porte a refusé ────────────────────────────────
#
# Un `403 gate` dit qu'un des trois facteurs a échoué, jamais lequel. La théorie
# est dans `config/free_gate.py` et y est testée facteur par facteur ; ce
# compteur en fait une **mesure de production** : savoir que 9 refus sur 10
# viennent du corps (et non de l'IP) change complètement la réponse à apporter.

_GATE_FACTORS: dict[str, int] = {}


def note_gate_factors(factors) -> None:
    """Compte les facteurs en échec d'un refus de porte (fail-soft)."""
    try:
        for factor in factors or ():
            key = str(factor)
            if len(_GATE_FACTORS) >= _STATS_MAX_KEYS and key not in _GATE_FACTORS:
                continue
            _GATE_FACTORS[key] = int(_GATE_FACTORS.get(key, 0)) + 1
    except Exception:
        pass


def gate_factor_stats() -> dict:
    """``{facteur: occurrences}``, du plus fréquent au moins fréquent."""
    try:
        return dict(sorted(_GATE_FACTORS.items(), key=lambda kv: -kv[1]))
    except Exception:
        return {}
