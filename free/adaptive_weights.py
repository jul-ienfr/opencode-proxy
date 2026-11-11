"""Poids d'egress adaptatifs selon les 429 récents.

Les poids configurés restent la base. Un provider qui a pris des 429 récents
voit sa part réduite ; un provider dont tous les buckets connus sont bloqués
tombe à zéro ; le reste est renormalisé à 100. Module pur, testable sans pool.
"""

from __future__ import annotations

from typing import Any

from free.auto_egress import normalize_weights

DEFAULTS = {
    "enabled": False,
    "window_s": 300.0,
    "min_samples": 3,
    "recent_429_penalty": 0.25,
    "min_weight_pct": 2,
}


def parse_config(raw: Any) -> dict:
    """Normalise ``ip_rotation.egress_adaptive`` avec des bornes sûres."""
    data = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)

    def _float(key: str, low: float, high: float) -> None:
        try:
            out[key] = max(low, min(high, float(data.get(key, out[key]))))
        except (TypeError, ValueError):
            pass

    def _int(key: str, low: int, high: int) -> None:
        try:
            out[key] = max(low, min(high, int(data.get(key, out[key]))))
        except (TypeError, ValueError):
            pass

    out["enabled"] = bool(data.get("enabled", out["enabled"]))
    _float("window_s", 30.0, 3600.0)
    _int("min_samples", 1, 100)
    _float("recent_429_penalty", 0.0, 0.9)
    _int("min_weight_pct", 0, 50)
    return out


def adaptive_weights(
    base: dict[str, int],
    health: dict[str, dict[str, Any]],
    *,
    min_samples: int = 3,
    recent_429_penalty: float = 0.25,
    min_weight_pct: int = 2,
) -> dict[str, int]:
    """Retourne des poids renormalisés à 100 selon la santé 429.

    ``health[provider]`` accepte ``ok``, ``n429``, ``recent_429`` et
    ``blocked_ratio``. Un provider sans échantillon garde son poids de base ;
    seuls les 429 récents réduisent la part. Si tous les providers sont
    bloqués, on retombe sur la base pour ne pas laisser le sélecteur sans
    candidat.
    """
    cleaned = {str(name): max(0, int(weight or 0)) for name, weight in (base or {}).items()}
    if not cleaned:
        return {}
    effective: dict[str, int] = {}
    for name, base_weight in cleaned.items():
        if base_weight <= 0:
            effective[name] = 0
            continue
        stats = health.get(name) or {}
        try:
            ok = max(0, int(stats.get("ok") or 0))
            n429 = max(0, int(stats.get("n429") or 0))
            recent = max(0, int(stats.get("recent_429") or 0))
            blocked_ratio = max(0.0, min(1.0, float(stats.get("blocked_ratio") or 0.0)))
        except (TypeError, ValueError):
            effective[name] = base_weight
            continue
        samples = ok + n429
        if samples < max(1, int(min_samples)):
            effective[name] = base_weight
            continue
        if blocked_ratio >= 0.999:
            effective[name] = 0
            continue
        # On ne pénalise que les 429 RÉCENTS : un provider qui a beaucoup
        # échoué dans le passé mais qui est sain maintenant doit retrouver sa
        # part. Les compteurs `ok`/`n429` ne servent qu'à exiger un minimum
        # d'échantillons avant d'appliquer la modulation.
        factor = 1.0
        if recent:
            factor = max(0.2, 1.0 - float(recent_429_penalty) * min(4, recent))
        effective[name] = max(int(min_weight_pct), int(round(base_weight * factor)))
    if not any(effective.values()):
        return cleaned
    return normalize_weights(effective, list(effective))


__all__ = ["DEFAULTS", "adaptive_weights", "parse_config"]
