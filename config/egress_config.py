"""config.egress_config — configuration egress & clients (023 T017, T215).

Extrait de ``config/settings.py`` (plan 024, tâche T215) : helpers PURS de
résolution de la section ``egress:`` / ``clients:`` de ``config.yaml``.

Aucun import du projet, AUCUN état gardé ici : le store (``_yaml_data``) et
le lecteur ``yaml_get`` sont INJECTÉS par l'hôte (``config.settings``) à
chaque appel. C'est ce qui rend l'extraction sûre — un hot-reload de
``config.yaml`` reste immédiatement visible, exactement comme quand les
fonctions vivaient dans ``settings.py``.

Compatibilité : ``config.settings`` ré-exporte ``EGRESS`` / ``CLIENTS`` et
les wrappers ``_egress_provider_default`` / ``_egress_provider_weight_default``
/ ``_egress_strategy_default`` / ``get_egress_config`` / ``get_clients_config``
avec la même signature et le même comportement qu'avant l'extraction.
"""

from __future__ import annotations

from typing import Any, Callable

#: ``enabled`` / ``weight`` de repli par provider d'egress. La ventilation
#: historique 52/24/24/0 représente ~89 % du poids total.
PROVIDER_DEFAULTS: dict[str, tuple[bool, int]] = {
    "nordvpn": (True, 52),
    "warp": (True, 24),
    "proxies": (True, 24),
    "direct": (False, 0),
}
STRATEGIES = ("balance", "priority", "failover")
CLIENT_FAMILIES = ("anthropic_messages", "openai_chat", "openai_responses", "passthrough")


def provider_default(name: str) -> bool:
    """Valeur `enabled` de repli pour un provider d'egress."""
    return PROVIDER_DEFAULTS.get(str(name), (False, 0))[0]


def provider_weight_default(name: str) -> int:
    """Poids de repli (89 % de 52/24/24/0 est la répartition historique)."""
    return PROVIDER_DEFAULTS.get(str(name), (False, 0))[1]


def strategy_default(yaml_data: dict) -> str:
    """Stratégie de sélection : balance | priority | failover."""
    raw = (yaml_data or {}).get("egress")
    strategy = str(raw.get("strategy") or "balance").strip().lower() if isinstance(raw, dict) else "balance"
    return strategy if strategy in STRATEGIES else "balance"


def get_egress_config(yaml_data: dict, yaml_get: Callable[..., Any]) -> dict[str, Any]:
    """Configuration egress consolidée (providers, wire, rotation).

    Migration douce (023 T017) : `egress.providers.<name>.enabled` prime ;
    à défaut, `ip_rotation.egress_providers.<name>` (legacy) est consulté ;
    à défaut encore, les valeurs de repli historiques s'appliquent.

    ``yaml_data`` et ``yaml_get`` sont injectés par l'hôte : ce module ne
    garde aucune référence globale, donc la lecture reste live.
    """
    data = yaml_data if isinstance(yaml_data, dict) else {}
    raw = data.get("egress")
    raw = raw if isinstance(raw, dict) else {}
    providers_raw = raw.get("providers")
    providers_raw = providers_raw if isinstance(providers_raw, dict) else {}
    legacy = yaml_get("ip_rotation", "egress_providers", {}) or {}
    legacy = legacy if isinstance(legacy, dict) else {}
    providers: dict[str, dict[str, Any]] = {}
    for name, (default_enabled, default_weight) in PROVIDER_DEFAULTS.items():
        cfg = providers_raw.get(name)
        cfg = cfg if isinstance(cfg, dict) else {}
        enabled = cfg.get("enabled", legacy.get(name, default_enabled))
        weight = cfg.get("weight", default_weight)
        try:
            weight_int = int(weight)
        except (TypeError, ValueError):
            weight_int = default_weight
        providers[name] = {"enabled": bool(enabled), "weight": max(0, weight_int)}
    wire = raw.get("wire")
    wire = wire if isinstance(wire, dict) else {}
    rotation = raw.get("rotation")
    rotation = rotation if isinstance(rotation, dict) else {}

    def _int(key: str, default: int) -> int:
        try:
            return max(0, int(rotation.get(key, default)))
        except (TypeError, ValueError):
            return default

    return {
        "providers": providers,
        "strategy": strategy_default(data),
        "wire": {
            "client_mode": str(wire.get("client_mode") or "observe"),
            "burn_guard": bool(wire.get("burn_guard", True)),
        },
        "rotation": {
            "min_interval_s": _int("min_interval_s", 180),
            "max_per_hour": _int("max_per_hour", 12),
            "probe_budget_per_ip": _int("probe_budget_per_ip", 3),
            "probe_cooldown_s": _int("probe_cooldown_s", 300),
            "release_on_single_429": bool(rotation.get("release_on_single_429", False)),
            "require_ip_agreement": bool(rotation.get("require_ip_agreement", True)),
        },
    }


def get_clients_config(yaml_data: dict) -> dict[str, Any]:
    """Configuration des familles de clients (023 US7)."""
    data = yaml_data if isinstance(yaml_data, dict) else {}
    raw = data.get("clients")
    raw = raw if isinstance(raw, dict) else {}
    families_raw = raw.get("families")
    families_raw = families_raw if isinstance(families_raw, dict) else {}
    families = {
        name: (dict(families_raw[name]) if isinstance(families_raw.get(name), dict) else {})
        for name in CLIENT_FAMILIES
    }
    return {
        "enabled": bool(raw.get("enabled", True)),
        "default_family": str(raw.get("default_family") or "anthropic_messages"),
        "registry": str(raw.get("registry") or "free/clients/registry.json"),
        "tool_policy": str(raw.get("tool_policy") or "free/clients/tool_policy.json"),
        "unknown_client": str(raw.get("unknown_client") or "passthrough"),
        "families": families,
    }


__all__ = [
    "CLIENT_FAMILIES",
    "PROVIDER_DEFAULTS",
    "STRATEGIES",
    "get_clients_config",
    "get_egress_config",
    "provider_default",
    "provider_weight_default",
    "strategy_default",
]
