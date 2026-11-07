"""Sélection multi-provider Auto-Egress (feature 008).

Le GUI expose des cases à cocher ; cette couche pure normalise la liste et
dérive le mode legacy compatible (`vpn`, `socks5`, `direct`, `auto_egress`).
Aucun I/O, aucun import projet : testable sans pool.
"""

from __future__ import annotations

from typing import Any

PROVIDERS: tuple[str, ...] = ("nordvpn", "warp", "proxies", "direct")
PROVIDER_LABELS: dict[str, str] = {
    "nordvpn": "NordVPN",
    "warp": "WARP",
    "proxies": "Proxys SOCKS5/HTTPS",
    "direct": "Direct",
}
_ALIASES = {
    "nordvpn": "nordvpn",
    "vpn": "nordvpn",
    "warp": "warp",
    "socks5": "warp",
    "proxies": "proxies",
    "proxy": "proxies",
    "direct": "direct",
}


def normalize_providers(raw: Any) -> list[str]:
    """Déduplique/filtre une liste de providers en préservant l'ordre.

    Accepte une liste, un tuple ou une chaîne ``"warp,nordvpn"``. Les valeurs
    inconnues sont ignorées. ``direct`` peut coexister avec les autres : c'est
    un egress à part entière (IP de la machine), utilisable en répartition.
    """
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        items = []
    ordered: list[str] = []
    for item in items:
        name = _ALIASES.get(str(item or "").strip().lower())
        if name and name not in ordered:
            ordered.append(name)
    return ordered


def derive_mode(providers: list[str]) -> str:
    """Mode legacy dérivé de la liste cochée.

    - ``nordvpn`` seul → ``vpn``
    - ``warp``/``proxies`` (sans NordVPN) → ``socks5``
    - ``direct`` seul → ``direct``
    - ``nordvpn`` + WARP/proxys/direct → ``auto_egress``
    - WARP/proxys + ``direct`` (sans NordVPN) → ``socks5``
    """
    selected = set(providers)
    if not selected:
        raise ValueError("au moins un provider est requis")
    if selected == {"direct"}:
        return "direct"
    if selected == {"nordvpn"}:
        return "vpn"
    if selected <= {"warp", "proxies"}:
        return "socks5"
    if "direct" in selected and "nordvpn" not in selected:
        return "socks5"
    if "nordvpn" in selected and selected & {"warp", "proxies", "direct"}:
        return "auto_egress"
    raise ValueError(f"combinaison de providers non supportée: {sorted(selected)}")


def equal_weights(providers: list[str]) -> dict[str, int]:
    """Parts égales entières dont la somme vaut 100."""
    names = [p for p in providers if p]
    if not names:
        return {}
    base = 100 // len(names)
    remainder = 100 - base * len(names)
    return {
        name: base + (1 if index < remainder else 0)
        for index, name in enumerate(names)
    }


def normalize_weights(raw: Any, providers: list[str]) -> dict[str, int]:
    """Normalise des poids 0–100 sur ``providers`` à une somme de 100.

    Les providers absents valent 0. Si tous les poids sont nuls, on retombe
    sur des parts égales. Le plus grand reste distribue les unités manquantes.
    """
    names = [p for p in providers if p]
    if not names:
        return {}
    data = raw if isinstance(raw, dict) else {}
    values: dict[str, int] = {}
    for name in names:
        try:
            values[name] = max(0, min(100, int(data.get(name, 0) or 0)))
        except (TypeError, ValueError):
            values[name] = 0
    total = sum(values.values())
    if total <= 0:
        return equal_weights(names)
    scaled = {name: values[name] * 100.0 / total for name in names}
    floors = {name: int(scaled[name]) for name in names}
    missing = 100 - sum(floors.values())
    for name in sorted(names, key=lambda n: scaled[n] - floors[n], reverse=True)[:missing]:
        floors[name] += 1
    return floors


class SmoothWeightedRoundRobin:
    """Smooth weighted round-robin déterministe (état en mémoire)."""

    def __init__(self) -> None:
        self._current: dict[str, int] = {}

    def reset(self) -> None:
        self._current.clear()

    def pick(self, weights: dict[str, int]) -> str | None:
        positive = [(name, int(weight)) for name, weight in weights.items() if int(weight) > 0]
        if not positive:
            return None
        if len(positive) == 1:
            return positive[0][0]
        total = sum(weight for _name, weight in positive)
        for name, weight in positive:
            self._current[name] = self._current.get(name, 0) + weight
        # Ordre stable : le premier provider de ``weights`` gagne les égalités.
        order = {name: index for index, name in enumerate(weights)}
        chosen = max(
            positive,
            key=lambda item: (self._current.get(item[0], 0), -order.get(item[0], 0)),
        )[0]
        self._current[chosen] -= total
        return chosen


def providers_from_mode(mode: str) -> list[str]:
    """Compatibilité legacy : mode → liste de providers."""
    value = str(mode or "").strip().lower()
    if value == "vpn":
        return ["nordvpn"]
    if value == "socks5":
        return ["warp"]
    if value == "direct":
        return ["direct"]
    if value == "auto_egress":
        return ["nordvpn", "warp"]
    return ["nordvpn"]


__all__ = [
    "PROVIDERS",
    "PROVIDER_LABELS",
    "SmoothWeightedRoundRobin",
    "derive_mode",
    "equal_weights",
    "normalize_providers",
    "normalize_weights",
    "providers_from_mode",
]
