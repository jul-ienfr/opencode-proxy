"""V8.2 — canary : vérifier périodiquement que la jambe répond *vraiment*.

**La leçon qui a créé ce module.** Le 2026-10-22, `jev-1.13-free` a été recommandé
comme « 3× plus rapide » : il répondait `200` en 4 s. Il était refusé à chaque
appel et ne produisait rien. Le proxy renvoie `200` avec une trame `event: error`
quand l'amont refuse — le flux SSE est déjà ouvert, le code HTTP ne peut plus
changer.

**Conséquence de conception, non négociable.** Un canary qui ne regarde que le
statut HTTP aurait dit « tout va bien » pendant toute la panne. Ici, un probe
n'est un succès que si la réponse contient du **contenu réel** (texte ou
raisonnement) et **aucune trame d'erreur**. Le statut est une condition
nécessaire, jamais suffisante.

**Ce que le module ne fait pas.** Il ne sonde pas lui-même : il enregistre des
verdicts. La sonde réseau vit dans `opencode.py` (elle a besoin du port, du
modèle configuré et du pool de tunnels) ; ce module ne connaît que l'état, ce qui
le rend testable sans réseau.

**Alerte.** Trois échecs consécutifs sur un même probe = `failing`. Un échec
isolé reste `degraded` : une jambe free instable a des ratés normaux, et crier
au loup à chaque hoquet rendrait l'alerte inutilisable.
"""

from __future__ import annotations

import time

__all__ = [
    "ALERT_AFTER",
    "HISTORY",
    "PROBES",
    "clear",
    "record",
    "snapshot",
]

#: Nombre d'échecs consécutifs qui déclenche `failing`.
ALERT_AFTER = 3

#: Profondeur d'historique conservée par probe (borné : la mémoire du proxy).
HISTORY = 20

#: Probes attendus. Un probe inconnu est accepté mais signalé comme non déclaré :
#: mieux vaut voir un nom inattendu que perdre silencieusement la mesure.
PROBES = ("free.chat", "free.messages")

#: `{probe: [{"ok", "ts", "ms", "detail"}, …]}` — plus récent en dernier.
_STATE: dict[str, list[dict]] = {}


def record(probe: str, ok: bool, *, latency_ms: float = 0.0, detail: str = "") -> dict:
    """Enregistre un verdict. Ne lève jamais, ne bloque jamais.

    ``ok`` doit être calculé sur le **contenu** par l'appelant : ce module ne
    peut pas savoir si une réponse vide est un succès.
    """
    name = str(probe or "?").strip() or "?"
    entry = {
        "ok": bool(ok),
        "ts": time.time(),
        "ms": round(float(latency_ms or 0.0), 1),
        "detail": str(detail or "")[:200],
    }
    try:
        history = _STATE.setdefault(name, [])
        history.append(entry)
        if len(history) > HISTORY:
            del history[: len(history) - HISTORY]
    except Exception:
        pass
    return entry


def _consecutive_failures(history: list[dict]) -> int:
    count = 0
    for entry in reversed(history):
        if entry.get("ok"):
            break
        count += 1
    return count


def snapshot() -> dict:
    """État par probe : verdict, échecs consécutifs, dernière mesure."""
    probes: dict[str, dict] = {}
    for name in sorted(_STATE):
        history = list(_STATE.get(name) or [])
        if not history:
            continue
        fails = _consecutive_failures(history)
        last = history[-1]
        if fails >= ALERT_AFTER:
            verdict = "failing"
        elif fails > 0:
            verdict = "degraded"
        else:
            verdict = "ok"
        probes[name] = {
            "verdict": verdict,
            "consecutive_failures": fails,
            "last_ok": bool(last.get("ok")),
            "last_ms": last.get("ms"),
            "last_ts": last.get("ts"),
            "last_detail": last.get("detail"),
            "samples": len(history),
            "ok_rate": round(sum(1 for e in history if e.get("ok")) / len(history), 3),
        }
    verdicts = {info["verdict"] for info in probes.values()}
    if not probes:
        overall = "unknown"
    elif "failing" in verdicts:
        overall = "failing"
    elif "degraded" in verdicts:
        overall = "degraded"
    else:
        overall = "ok"
    return {
        "status": overall,
        "alert_after": ALERT_AFTER,
        "probes": probes,
        "undeclared": [name for name in probes if name not in PROBES],
        "never_run": [name for name in PROBES if name not in probes],
    }


def clear() -> None:
    try:
        _STATE.clear()
    except Exception:
        pass
