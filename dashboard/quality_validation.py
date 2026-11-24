"""Validation pure des blocs qualité/presélection du dashboard.

[024 T153 2026-11-21] Extrait de `dashboard/api.py` (l. 772-838) pour sortir
une unité cohérente du monolithe, sans changer le comportement : `api.py`
ré-exporte les deux symboles sous leurs anciens noms privés.

Ce module n'importe ni FastAPI ni SQLite : il ne dépend que de ses données.
"""

from __future__ import annotations

QUALITY_BLOCK_SPEC: dict[str, dict[str, tuple]] = {
    "quality_preselection": {
        "enabled": (bool, None, None),
        "fast_tps": (float, 0.1, 1000.0),
        "slow_tps": (float, 0.1, 1000.0),
        "max_load": (int, 0, 100),
        "tier_b_quota": (int, 1, 500),
        "tier_c_quota": (int, 1, 500),
    },
    "quality_rotation": {
        "enabled": (bool, None, None),
        "min_requests": (int, 1, 500),
        "min_samples": (int, 1, 20),
        "trend_pct": (float, 0.0, 100.0),
        "ttfb_ceiling_ms": (float, 100.0, 60000.0),
        "max_per_hour": (int, 0, 60),
        "global_slow_guard": (int, 1, 10),
    },
    "post_rotation_probe": {
        "enabled": (bool, None, None),
        "max_attempts": (int, 1, 5),
        "max_tokens": (int, 1, 32),
    },
}


def validate_quality_blocks(body) -> list:
    """Validation des blocs qualité/presélection (partiels acceptés)."""
    errors: list = []
    if not isinstance(body, dict):
        return errors
    for block, spec in QUALITY_BLOCK_SPEC.items():
        if block not in body:
            continue
        data = body[block]
        if not isinstance(data, dict):
            errors.append(f"{block} doit être un objet")
            continue
        for key, val in data.items():
            if key not in spec:
                errors.append(f"{block}.{key} non supporté")
                continue
            typ, lo, hi = spec[key]
            if typ is bool:
                if not isinstance(val, bool):
                    errors.append(f"{block}.{key} doit être booléen")
                continue
            try:
                num = float(val)
            except (TypeError, ValueError):
                errors.append(f"{block}.{key} doit être numérique")
                continue
            if lo is not None and not lo <= num <= hi:
                errors.append(f"{block}.{key}={num} hors bornes [{lo}, {hi}]")
            if typ is int and not num.is_integer():
                errors.append(f"{block}.{key} doit être un entier")
        if block == "quality_preselection":
            _fast = data.get("fast_tps")
            _slow = data.get("slow_tps")
            if _fast is not None and _slow is not None:
                try:
                    if float(_slow) >= float(_fast):
                        errors.append("quality_preselection.slow_tps doit être < fast_tps")
                except (TypeError, ValueError):
                    pass
    return errors


__all__ = ["QUALITY_BLOCK_SPEC", "validate_quality_blocks"]
