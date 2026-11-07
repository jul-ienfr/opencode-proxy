"""Capacité des egress : combien de stations par provider et par mode (010).

Module pur, sans I/O, Docker ni réseau. Il répond à une question distincte de
la stratégie 008 :

- la stratégie choisit **qui sert** (Fallback/Répartition) ;
- les poids choisissent la **part de trafic** ;
- la capacité choisit **combien d'endpoints** sont nécessaires.

Un endpoint = un bucket de quota (station NordVPN, endpoint WARP, proxy,
Direct). ``target_slots`` est le nombre de stations client-utilisables visées.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from free.auto_egress import equal_weights, normalize_weights

PROVIDER_ORDER: tuple[str, ...] = ("nordvpn", "warp", "proxies", "direct")
PROVIDER_LABELS: dict[str, str] = {
    "nordvpn": "NordVPN",
    "warp": "WARP",
    "proxies": "Proxys SOCKS5/HTTPS",
    "direct": "Direct",
}
POLICIES: tuple[str, ...] = ("full", "shared", "proportional")
FIXED_PROVIDERS: frozenset[str] = frozenset({"direct"})
DEFAULT_TARGET_SLOTS = 6


def provider_label(name: str) -> str:
    return PROVIDER_LABELS.get(str(name or ""), str(name or ""))


def capacity_config(raw: Any) -> tuple[int, str]:
    """Lit ``egress_capacity`` avec des défauts robustes.

    Retourne ``(target_slots, reserve_policy)``. ``target_slots`` est borné
    1–10 ; une politique inconnue retombe sur ``full``.
    """
    data = raw if isinstance(raw, dict) else {}
    try:
        target = int(data.get("target_slots", DEFAULT_TARGET_SLOTS) or 0)
    except (TypeError, ValueError):
        target = DEFAULT_TARGET_SLOTS
    target = max(1, min(10, target))
    policy = str(data.get("reserve_policy") or "full").strip().lower()
    if policy not in POLICIES:
        policy = "full"
    return target, policy


def effective_policy(strategy: str, policy: str) -> tuple[str, bool]:
    """Politique réellement applicable ; ``proportional`` exige Répartition."""
    normalized = str(strategy or "fallback").strip().lower()
    if normalized not in ("fallback", "balance"):
        normalized = "fallback"
    if policy == "proportional" and normalized != "balance":
        return "shared", True
    return policy, False


def _compositions(total: int, parts: int) -> Iterable[tuple[int, ...]]:
    """Toutes les compositions de ``total`` en ``parts`` entiers >= 0."""
    if parts <= 0:
        if total == 0:
            yield ()
        return
    if parts == 1:
        yield (total,)
        return
    for value in range(total + 1):
        for rest in _compositions(total - value, parts - 1):
            yield (value, *rest)


def proportional_allocation(
    target_slots: int,
    weights: dict[str, int | float],
    fixed: dict[str, int] | None = None,
) -> dict[str, int]:
    """Répartit ``target_slots`` endpoints en minimisant l'écart de charge.

    ``weights`` ne contient que les providers scalables (pas Direct). Un poids
    0 signifie secours : il n'obtient aucune part tant qu'un poids positif
    existe. ``fixed`` réserve des slots non scalables (Direct) ; il n'est
    utilisé que si Direct a un poids > 0.
    """
    target = max(0, int(target_slots))
    fixed = {str(k): max(0, int(v)) for k, v in (fixed or {}).items()}
    scalable = [str(name) for name in weights if str(name) not in fixed]
    remaining = max(0, target - sum(fixed.values()))
    allocation: dict[str, int] = dict.fromkeys(scalable, 0)
    if not scalable:
        return dict(fixed)

    positive = [name for name in scalable if float(weights.get(name, 0) or 0) > 0]
    if not positive:
        base, extra = divmod(remaining, len(scalable))
        for index, name in enumerate(scalable):
            allocation[name] = base + (1 if index < extra else 0)
        return {**fixed, **allocation}

    best_key: tuple | None = None
    best: dict[str, int] | None = None
    for combo in _compositions(remaining, len(scalable)):
        candidate = dict(zip(scalable, combo))
        # Un provider à poids positif doit avoir au moins 1 endpoint si la
        # place le permet ; sinon il est simplement non couvert (pénalisé).
        if remaining >= len(positive) and any(candidate[n] < 1 for n in positive):
            continue
        unserved = sum(1 for n in positive if candidate[n] == 0)
        loads = [
            float(weights[n]) / candidate[n]
            for n in positive
            if candidate[n] > 0
        ]
        if not loads:
            ratio = max_load = variance = float("inf")
        else:
            ratio = max(loads) / min(loads)
            max_load = max(loads)
            average = sum(loads) / len(loads)
            variance = sum((load - average) ** 2 for load in loads) / len(loads)
        key = (
            unserved,
            ratio,
            max_load,
            variance,
            tuple(-candidate[n] for n in scalable),
        )
        if best_key is None or key < best_key:
            best_key = key
            best = candidate
    if best is not None:
        allocation = best
    return {**fixed, **allocation}


def _normalized_weights(selected: list[str], weights: dict[str, int] | None) -> dict[str, int]:
    names = [name for name in selected if name]
    if not names:
        return {}
    raw = weights if isinstance(weights, dict) else {}
    values = {name: max(0, int(raw.get(name, 0) or 0)) for name in names}
    if sum(values.values()) <= 0:
        return equal_weights(names)
    return normalize_weights(values, names)


@dataclass
class ProviderCapacity:
    name: str
    configured: int = 0
    enabled: int = 0
    usable: int = 0
    selected: bool = False
    weight: int | None = None
    expected_share: float | None = None
    per_endpoint: float | None = None
    observed: int = 0
    observed_share: float | None = None
    required: int | None = None
    deficit: int = 0
    status: str = "disabled"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "enabled": self.enabled,
            "usable": self.usable,
            "selected": self.selected,
            "weight": self.weight,
            "expected_share": self.expected_share,
            "per_endpoint": self.per_endpoint,
            "observed": self.observed,
            "observed_share": self.observed_share,
            "required": self.required,
            "deficit": self.deficit,
            "status": self.status,
        }


@dataclass
class CapacityReport:
    target_slots: int
    strategy: str
    reserve_policy: str
    requested_policy: str
    providers: dict[str, ProviderCapacity] = field(default_factory=dict)
    selected: list[str] = field(default_factory=list)
    total_usable: int = 0
    total_required: int = 0
    observed_requests: int = 0
    deficit: int = 0
    verdict: str = "insufficient"
    warnings: list[dict[str, str]] = field(default_factory=list)
    recommendations: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_slots": self.target_slots,
            "strategy": self.strategy,
            "reserve_policy": self.reserve_policy,
            "requested_policy": self.requested_policy,
            "providers": {
                name: provider.to_dict()
                for name, provider in self.providers.items()
            },
            "selected": list(self.selected),
            "total_usable": self.total_usable,
            "total_required": self.total_required,
            "observed_requests": self.observed_requests,
            "deficit": self.deficit,
            "verdict": self.verdict,
            "warnings": list(self.warnings),
            "recommendations": list(self.recommendations),
        }


def _provider_names(selected: list[str]) -> list[str]:
    return [name for name in selected if name in PROVIDER_ORDER]


def _counts_for(name: str, counts: dict[str, dict[str, int]]) -> dict[str, int]:
    raw = counts.get(name) or {}
    configured = max(0, int(raw.get("configured", 0) or 0))
    enabled = max(0, int(raw.get("enabled", 0) or 0))
    usable = max(0, int(raw.get("usable", enabled) or 0))
    return {
        "configured": configured,
        "enabled": enabled,
        "usable": min(usable, enabled),
    }


def _message(code: str, fr: str, en: str, severity: str = "info") -> dict[str, str]:
    return {"code": code, "severity": severity, "fr": fr, "en": en}


def _counts_text(selected: list[str], providers: dict[str, ProviderCapacity]) -> str:
    return ", ".join(
        f"{provider_label(name)} {providers[name].usable}"
        for name in selected
        if name in providers
    )


def evaluate_capacity(
    *,
    providers: dict[str, dict[str, int]],
    selected: list[str] | None = None,
    strategy: str = "fallback",
    weights: dict[str, int] | None = None,
    target_slots: int = DEFAULT_TARGET_SLOTS,
    reserve_policy: str = "full",
    observed: dict[str, int] | None = None,
    endpoint_observed: list[dict[str, Any]] | None = None,
) -> CapacityReport:
    """Construit le rapport de capacité pour le GUI/API."""
    target = max(1, min(10, int(target_slots or DEFAULT_TARGET_SLOTS)))
    normalized_strategy = str(strategy or "fallback").strip().lower()
    if normalized_strategy not in ("fallback", "balance"):
        normalized_strategy = "fallback"
    requested = str(reserve_policy or "full").strip().lower()
    if requested not in POLICIES:
        requested = "full"
    policy, coerced = effective_policy(normalized_strategy, requested)
    selected_names = _provider_names(list(selected or []))
    if not selected_names:
        selected_names = []

    report = CapacityReport(
        target_slots=target,
        strategy=normalized_strategy,
        reserve_policy=policy,
        requested_policy=requested,
        selected=selected_names,
    )

    normalized = _normalized_weights(selected_names, weights) if normalized_strategy == "balance" else {}
    observed_counts = observed if isinstance(observed, dict) else {}
    observed_total = sum(
        max(0, int(observed_counts.get(name, 0) or 0))
        for name in selected_names
    )
    allocation: dict[str, int] = {}
    if policy == "proportional":
        scalable = [name for name in selected_names if name not in FIXED_PROVIDERS]
        fixed: dict[str, int] = {}
        if "direct" in selected_names and normalized.get("direct", 0) > 0:
            fixed["direct"] = 1
        allocation = proportional_allocation(target, {name: normalized.get(name, 0) for name in scalable}, fixed)

    for name in PROVIDER_ORDER:
        counts = _counts_for(name, providers)
        is_selected = name in selected_names
        weight = (
            normalized.get(name, 0)
            if normalized_strategy == "balance" and is_selected
            else None
        )
        required: int | None = None
        if is_selected:
            if policy == "full":
                required = target if name not in FIXED_PROVIDERS else 1
            elif policy == "proportional":
                required = allocation.get(name)
                if required is None and name in FIXED_PROVIDERS and weight:
                    required = 1
        deficit = 0
        if required is not None:
            deficit = max(0, required - counts["usable"])
        status = "disabled"
        if is_selected:
            if counts["usable"] <= 0:
                status = "unavailable"
            elif deficit > 0:
                status = "deficit"
            else:
                status = "ok"
        per_endpoint = None
        expected_share = None
        if normalized_strategy == "balance" and is_selected and weight is not None:
            expected_share = float(weight)
            if counts["usable"] > 0:
                per_endpoint = round(float(weight) / counts["usable"], 2)
        observed_count = (
            max(0, int(observed_counts.get(name, 0) or 0)) if is_selected else 0
        )
        observed_share = (
            round(observed_count * 100.0 / observed_total, 1)
            if observed_total and is_selected
            else None
        )
        report.providers[name] = ProviderCapacity(
            name=name,
            configured=counts["configured"],
            enabled=counts["enabled"],
            usable=counts["usable"],
            selected=is_selected,
            weight=weight,
            expected_share=expected_share,
            per_endpoint=per_endpoint,
            observed=observed_count,
            observed_share=observed_share,
            required=required,
            deficit=deficit,
            status=status,
        )

    report.total_usable = sum(report.providers[name].usable for name in selected_names)
    report.observed_requests = observed_total
    if policy == "full":
        report.total_required = sum(
            report.providers[name].required or 0 for name in selected_names
        )
    else:
        report.total_required = target
    report.deficit = max(0, target - report.total_usable)

    warnings: list[dict[str, str]] = []
    recommendations: list[dict[str, str]] = []

    if coerced:
        warnings.append(
            _message(
                "proportional_fallback",
                "La répartition proportionnelle n'est pas applicable en Fallback : "
                "la politique partagée est utilisée.",
                "Proportional allocation only applies to Load balancing: shared "
                "policy is used instead.",
                "warn",
            )
        )

    for name in selected_names:
        provider = report.providers[name]
        if provider.configured > 0 and provider.enabled < provider.configured:
            warnings.append(
                _message(
                    f"disabled_{name}",
                    f"{provider_label(name)} : "
                    f"{provider.configured - provider.enabled} endpoint(s) désactivé(s).",
                    f"{provider_label(name)}: "
                    f"{provider.configured - provider.enabled} endpoint(s) disabled.",
                    "warn",
                )
            )
        if provider.usable <= 0:
            warnings.append(
                _message(
                    f"unavailable_{name}",
                    f"{provider_label(name)} est sélectionné mais n'a aucun endpoint "
                    "utilisable.",
                    f"{provider_label(name)} is selected but has no usable endpoint.",
                    "danger",
                )
            )

    if normalized_strategy == "balance" and policy in ("full", "shared", "proportional"):
        positive = [
            report.providers[name]
            for name in selected_names
            if (report.providers[name].weight or 0) > 0
            and report.providers[name].usable > 0
        ]
        if len(positive) >= 2:
            loads = [float(provider.per_endpoint or 0) for provider in positive]
            if min(loads) > 0 and max(loads) / min(loads) > 2:
                warnings.append(
                    _message(
                        "endpoint_imbalance",
                        "Déséquilibre de charge : un endpoint du provider le plus "
                        f"chargé reçoit {max(loads) / min(loads):.1f}× plus de trafic "
                        "que le moins chargé.",
                        "Load imbalance: the busiest provider's endpoint receives "
                        f"{max(loads) / min(loads):.1f}× more traffic than the least "
                        "loaded.",
                        "warn",
                    )
                )

    direct = report.providers.get("direct")
    if direct and direct.selected and (direct.weight or 0) > 0 and target > 1:
        warnings.append(
            _message(
                "direct_single_ip",
                "Direct ne fournit qu'une seule IP : avec ce poids, elle peut "
                "épuiser son quota beaucoup plus vite.",
                "Direct provides only one IP: with this weight it can exhaust its "
                "quota much faster.",
                "warn",
            )
        )

    if normalized_strategy == "balance" and observed_total >= 20:
        shifted = []
        for name in selected_names:
            provider = report.providers[name]
            expected = float(provider.expected_share or 0.0)
            if expected <= 0 or provider.observed_share is None:
                continue
            if abs(provider.observed_share - expected) > 20.0:
                shifted.append(
                    f"{provider_label(name)} {provider.observed_share:.0f} % "
                    f"(attendu {expected:.0f} %)"
                )
        if shifted:
            warnings.append(
                _message(
                    "observed_shift",
                    "Part observée éloignée des poids : " + ", ".join(shifted) + ".",
                    "Observed share differs from weights: " + ", ".join(shifted) + ".",
                    "warn",
                )
            )

    endpoint_counts: list[tuple[str, str, int]] = []
    for item in endpoint_observed or []:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip().lower()
        if provider not in selected_names:
            continue
        try:
            requests = int(item.get("requests", 0) or 0)
        except (TypeError, ValueError):
            continue
        if requests > 0:
            endpoint_counts.append(
                (provider, str(item.get("endpoint") or ""), requests)
            )
    if len(endpoint_counts) >= 3:
        values = sorted(requests for _provider, _endpoint, requests in endpoint_counts)
        median_value = float(median(values))
        hot = [
            (provider, endpoint, requests)
            for provider, endpoint, requests in endpoint_counts
            if median_value > 0 and requests > 2.0 * median_value
        ]
        if hot:
            provider, endpoint, requests = max(hot, key=lambda item: item[2])
            ratio = requests / median_value
            warnings.append(
                _message(
                    "endpoint_hotspot",
                    f"Endpoint {endpoint or '?'} ({provider_label(provider)}) : "
                    f"{requests} requêtes, {ratio:.1f}× la médiane. Vérifie son "
                    "quota ou ajuste les poids.",
                    f"Endpoint {endpoint or '?'} ({provider_label(provider)}): "
                    f"{requests} requests, {ratio:.1f}× the median. Check its quota "
                    "or adjust the weights.",
                    "warn",
                )
            )
            recommendations.append(
                _message(
                    "endpoint_reweight",
                    "Pour répartir la charge, augmente le poids des providers "
                    "sous-utilisés ou désactive l'endpoint saturé.",
                    "To spread the load, increase the weight of underused providers "
                    "or disable the saturated endpoint.",
                )
            )

    if policy == "shared":
        if normalized_strategy == "fallback" and len(selected_names) > 1:
            primary = report.providers[selected_names[0]]
            failover_capacity = report.total_usable - primary.usable
            if failover_capacity < target:
                warnings.append(
                    _message(
                        "fallback_degraded",
                        f"Après bascule du primaire, il ne resterait que "
                        f"{failover_capacity} station(s).",
                        f"After the primary fails over, only {failover_capacity} "
                        "station(s) would remain.",
                        "warn",
                    )
                )
        elif normalized_strategy == "balance" and len(selected_names) > 1:
            failover_min = min(
                report.total_usable - report.providers[name].usable
                for name in selected_names
            )
            if failover_min < target:
                warnings.append(
                    _message(
                        "no_reserve",
                        f"Capacité partagée : si un provider tombe, il ne resterait "
                        f"que {failover_min} station(s).",
                        f"Shared capacity: if one provider fails, only {failover_min} "
                        "station(s) would remain.",
                        "warn",
                    )
                )

    full_bad = [
        name
        for name in selected_names
        if name not in FIXED_PROVIDERS and report.providers[name].usable < target
    ]

    if report.total_usable < target:
        recommendations.append(
            _message(
                "add_endpoints",
                f"Il manque {report.deficit} endpoint(s) pour atteindre {target} "
                "stations utilisables.",
                f"{report.deficit} more endpoint(s) are needed to reach {target} "
                "usable stations.",
                "danger",
            )
        )
    elif policy == "full":
        if full_bad:
            missing = ", ".join(
                f"{provider_label(name)} {report.providers[name].deficit}"
                for name in full_bad
            )
            recommendations.append(
                _message(
                    "full_missing",
                    f"Pour la redondance totale, il manque : {missing}.",
                    f"For full redundancy, missing: {missing}.",
                    "warn",
                )
            )
        else:
            recommendations.append(
                _message(
                    "full_reserve",
                    f"Redondance totale : {_counts_text(selected_names, report.providers)}. "
                    f"Chaque provider peut couvrir seul {target} stations.",
                    f"Full redundancy: {_counts_text(selected_names, report.providers)}. "
                    f"Each provider can cover {target} stations alone.",
                )
            )
    elif policy == "shared":
        recommendations.append(
            _message(
                "shared_capacity",
                f"Capacité partagée : {report.total_usable} endpoint(s) pour "
                f"{target} stations.",
                f"Shared capacity: {report.total_usable} endpoint(s) for {target} "
                "stations.",
            )
        )
    else:
        ideal = ", ".join(
            f"{provider_label(name)} {allocation.get(name, 0)}"
            for name in selected_names
            if name in allocation
        )
        recommendations.append(
            _message(
                "proportional_allocation",
                f"Répartition proportionnelle (cible {target}) : {ideal}. La charge "
                "par endpoint est égalisée.",
                f"Proportional allocation (target {target}): {ideal}. Per-endpoint "
                "load is evened out.",
            )
        )

    # Si les poids ne collent pas aux nombres d'endpoints, proposer un
    # reweight plutôt que de supprimer des endpoints.
    if normalized_strategy == "balance":
        positive = [
            provider
            for provider in report.providers.values()
            if provider.selected and (provider.weight or 0) > 0 and provider.usable > 0
        ]
        if len(positive) >= 2:
            loads = [float(provider.per_endpoint or 0) for provider in positive]
            if min(loads) > 0 and max(loads) / min(loads) > 2:
                suggested = normalize_weights(
                    {provider.name: provider.usable for provider in positive},
                    [provider.name for provider in positive],
                )
                suggestion_text = ", ".join(
                    f"{provider_label(name)} {weight} %"
                    for name, weight in suggested.items()
                )
                recommendations.append(
                    _message(
                        "reweight_suggestion",
                        f"Pour égaliser la charge par endpoint avec vos endpoints "
                        f"actuels : {suggestion_text}.",
                        f"To even out per-endpoint load with your current endpoints: "
                        f"{suggestion_text}.",
                    )
                )

    if policy == "full" and report.total_usable >= target and full_bad:
        names = ", ".join(provider_label(name) for name in full_bad)
        plural = len(full_bad) > 1
        warnings.append(
            _message(
                "full_degraded",
                f"Redondance incomplète : {names} "
                f"{'ne peuvent pas couvrir seuls' if plural else 'ne peut pas couvrir seul'} "
                f"{target} stations.",
                f"Incomplete redundancy: {names} cannot cover {target} stations alone.",
                "warn",
            )
        )

    if report.total_usable < target:
        report.verdict = "insufficient"
    elif policy == "full" and any(
        name not in FIXED_PROVIDERS and report.providers[name].usable < target
        for name in selected_names
    ):
        report.verdict = "degraded"
    elif policy == "shared" and normalized_strategy == "fallback" and len(selected_names) > 1:
        primary = report.providers[selected_names[0]]
        failover_capacity = report.total_usable - primary.usable
        report.verdict = "degraded" if failover_capacity < target or primary.usable < target else "ok"
    elif policy == "proportional" and any(
        report.providers[name].required is not None
        and report.providers[name].usable < report.providers[name].required
        for name in selected_names
    ):
        report.verdict = "degraded"
    else:
        report.verdict = "ok"

    report.warnings = warnings
    report.recommendations = recommendations
    return report


__all__ = [
    "CapacityReport",
    "POLICIES",
    "PROVIDER_ORDER",
    "ProviderCapacity",
    "capacity_config",
    "effective_policy",
    "evaluate_capacity",
    "proportional_allocation",
]
