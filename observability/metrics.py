"""observability.metrics — compteurs + rendu Prometheus (Phase 3 refonte).

Déplacement PUR depuis ``opencode.py`` (§ « Compteurs fallback/failover »
O3 + « Métriques de diagnostic perf » Lot 0 + ``_build_metrics_text``).
AUCUN import du projet :

* tout l'état mutable est possédé par l'hôte et PASSÉ EN PARAMÈTRE
  (``_FB_*`` / ``_lat_rings`` / ``_MISC_COUNTERS`` / ``_TTFB_*`` /
  ``_fetch_quotas_429_cache`` — lus ou mutés directement par les tests,
  dont ``monkeypatch.setattr(oc, "_FB_FALLBACK_COUNTS", None)`` en
  fail-soft) ; les wrappers hôtes d'une ligne lisent leurs globaux
  À L'APPEL ;
* ``MetricsSnapshot`` transporte des données PLAINES déjà extraites
  (l'extraction live ``shared_state`` / ``protocol_mapping`` reste côté
  hôte, avec ses garde-fous fail-soft + ``_debug`` inchangés) ;
* le rendu ``build_metrics_text`` est byte-identique à l'historique
  (mêmes familles, mêmes labels, mêmes HELP).
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

# Causes free bornées (labels Prometheus stables) : quota_429 / payload_400 /
# upstream_5xx / tunnel_vide / other.
FALLBACK_CAUSES = ("quota_429", "payload_400", "upstream_5xx", "tunnel_vide", "other")

# ~garantie mémoire O(1) par métrique ; p99 glissant.
LAT_RING_MAX = 1024

# Noms des rings de latence exposés (quantiles p50/p95/p99 sous /metrics).
LATENCY_GAUGES = (
    ("proxy_ttfb_upstream_ms", "TTFB du chemin paid upstream (ms) — alimente le seuil du watchdog"),
    ("proxy_curl_checkout_wait_ms", "Attente d'emprunt d'une session curl (ms)"),
)


def fallback_cause(status) -> str:
    """Bucketise un statut free en cause bornée (labels Prometheus stables)."""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return "tunnel_vide"
    if code == 429:
        return "quota_429"
    if code == 400:
        return "payload_400"
    if 500 <= code <= 599:
        return "upstream_5xx"
    if code <= 0:
        return "tunnel_vide"
    return "other"


def bump_fallback_counter(counts: dict | None, lock, leg: str, cause: str) -> None:
    """Incrémente un compteur fallback — fail-soft total (jamais de raise)."""
    if counts is None:
        return
    try:
        with lock:
            _k = (str(leg), str(cause))
            counts[_k] = counts.get(_k, 0) + 1
    except Exception:
        pass


def bump_failover_counter(counts: dict | None, lock, leg: str, cause: str, outcome: str) -> None:
    """Incrémente un compteur failover/garde paid — fail-soft total."""
    if counts is None:
        return
    try:
        with lock:
            _k = (str(leg), str(cause), str(outcome))
            counts[_k] = counts.get(_k, 0) + 1
    except Exception:
        pass


def reset_fallback_metrics(fallback_counts: dict | None, failover_counts: dict | None, lock) -> None:
    """Remise à zéro des compteurs O3 (tests uniquement) — jamais en runtime."""
    if fallback_counts is None or failover_counts is None:
        return
    try:
        with lock:
            fallback_counts.clear()
            failover_counts.clear()
    except Exception:
        pass


def observe_latency_ms(
    rings: dict[str, deque], lock, name: str, ms: float, *, ring_max: int = LAT_RING_MAX
) -> None:
    """Alimente le ring de latence ``name`` — fail-soft, jamais de raise."""
    try:
        with lock:
            ring = rings.get(name)
            if ring is None:
                ring = deque(maxlen=ring_max)
                rings[name] = ring
            ring.append(float(ms))
    except Exception:
        pass


def bump_misc_counter(counters: dict | None, lock, name: str) -> None:
    """Compteur simple (db_queuefull, fetch_quotas_429, …) — fail-soft."""
    if counters is None:
        return
    try:
        with lock:
            counters[name] = counters.get(name, 0) + 1
    except Exception:
        pass


def percentile(sorted_vals: list[float], q: float) -> float:
    """Percentile nearest-rank sur liste triée non vide."""
    idx = max(0, min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1) + 0.5)))
    return sorted_vals[idx]


def latency_snapshot(
    rings: dict[str, deque], lock
) -> dict[str, tuple[int, float, float, float]]:
    """name -> (n, p50, p95, p99) en ms. Snapshot sous lock, tri hors lock."""
    with lock:
        snapshot = {k: list(v) for k, v in rings.items()}
    out: dict[str, tuple[int, float, float, float]] = {}
    for name, vals in snapshot.items():
        vals.sort()
        if vals:
            out[name] = (
                len(vals),
                percentile(vals, 0.50),
                percentile(vals, 0.95),
                percentile(vals, 0.99),
            )
    return out


def new_metrics_lock() -> threading.Lock:
    """Fabrique le lock partagé des registres (mix sync/async, cf F-H3)."""
    return threading.Lock()


@dataclass
class MetricsSnapshot:
    """Données plaines pour ``build_metrics_text`` (extraites côté hôte).

    ``vpn=None`` = source indisponible (section sautée, comme l'historique) ;
    ``conv=None`` = stats cache de conversion indisponibles (lignes sautées).
    """

    fb: dict = field(default_factory=dict)
    fo: dict = field(default_factory=dict)
    misc: dict = field(default_factory=dict)
    ttfb: dict = field(default_factory=dict)
    latency: dict = field(default_factory=dict)
    vpn: dict | None = None
    conv: dict | None = None


def _gauge(lines: list, name: str, help_txt: str, rows) -> None:
    if not rows:
        return
    lines.append(f"# HELP {name} {help_txt}")
    lines.append(f"# TYPE {name} gauge")
    for labels, value in rows:
        lines.append(f"{name}{{{labels}}} {value}")


def render_vpn_section(vpn: dict | None) -> list[str]:
    """Section moteur §3.6 (EWMA/p95/slow, stations, rotations, cooldowns).

    ``has_engine=False`` (moteur indisponible) : seules les stations sont
    émises — rotations/cooldowns/paused sautés, comme l'historique.
    """
    lines: list[str] = []
    if not vpn:
        return lines
    _gauge(lines, "vpn_station_connected", "1 si la station est connectée", vpn.get("stations", []))
    _gauge(lines, "vpn_latency_ewma_ms", "EWMA par station·ip (ms)", vpn.get("ewma", []))
    _gauge(lines, "vpn_latency_p95_ms", "p95 glissant par station·ip (ms)", vpn.get("p95", []))
    _gauge(
        lines,
        "vpn_latency_consecutive_slow",
        "requêtes lentes consécutives par station·ip",
        vpn.get("slow", []),
    )
    if not vpn.get("has_engine"):
        return lines
    _gauge(
        lines,
        "vpn_rotations_total",
        "rotations déclenchées par type",
        [
            ('kind="soft"', vpn.get("total_soft", 0)),
            ('kind="hard"', vpn.get("total_hard", 0)),
        ],
    )
    _gauge(
        lines,
        "vpn_cooldown_active",
        "cooldowns actifs par kind",
        [('kind="soft"', vpn.get("cooldown_soft", 0)), ('kind="hard"', vpn.get("cooldown_hard", 0))],
    )
    _gauge(
        lines,
        "vpn_rotation_paused",
        "mode maintenance actif",
        [
            (
                'paused="true"' if vpn.get("paused") else 'paused="false"',
                1 if vpn.get("paused") else 0,
            )
        ],
    )
    return lines


def render_fallback_section(
    fb_snap: dict, fo_snap: dict, causes: tuple = FALLBACK_CAUSES
) -> list[str]:
    """Familles counter proxy_fallback_total / proxy_failover_total.

    Toujours émises (même à zéro) pour la cause quota_429 — les dashboards
    peuvent alerter dessus sans gérer l'absence de série."""
    lines: list[str] = []
    lines.append("# HELP proxy_fallback_total fallback free→paid par cause")
    lines.append("# TYPE proxy_fallback_total counter")
    for _cause in causes:
        _v = fb_snap.get(("free_to_paid", _cause), 0)
        lines.append(f'proxy_fallback_total{{leg="free_to_paid",cause="{_cause}"}} {_v}')
    for (_leg, _cause), _v in sorted(fb_snap.items()):
        if _leg != "free_to_paid" or _cause in causes:
            continue
        lines.append(f'proxy_fallback_total{{leg="{_leg}",cause="{_cause}"}} {_v}')
    lines.append("# HELP proxy_failover_total failover paid inter-clés / gardes par cause")
    lines.append("# TYPE proxy_failover_total counter")
    for (_leg, _cause, _outcome), _v in sorted(fo_snap.items()):
        lines.append(f'proxy_failover_total{{leg="{_leg}",cause="{_cause}",outcome="{_outcome}"}} {_v}')
    return lines


def render_lot0_section(
    latency_snap: dict,
    misc_snap: dict,
    ttfb_snap: dict,
    conv: dict | None,
) -> list[str]:
    """Percentiles de latence + compteurs diag + hit-rate conversion + TTFB."""
    lines: list[str] = []
    for _m_name, _help in LATENCY_GAUGES:
        _snap = latency_snap.get(_m_name)
        if _snap is None:
            continue
        _n, _p50, _p95, _p99 = _snap
        lines.append(f"# HELP {_m_name} {_help}")
        lines.append(f"# TYPE {_m_name} gauge")
        lines.append(f'{_m_name}{{quantile="0.5"}} {_p50:.1f}')
        lines.append(f'{_m_name}{{quantile="0.95"}} {_p95:.1f}')
        lines.append(f'{_m_name}{{quantile="0.99"}} {_p99:.1f}')
    lines.append("# HELP proxy_misc_total compteurs de diagnostic (Lot 0)")
    lines.append("# TYPE proxy_misc_total counter")
    for _mc, _mv in sorted(misc_snap.items()):
        lines.append(f'proxy_misc_total{{name="{_mc}"}} {_mv}')
    # Hit-rate du cache de conversion (compteurs maintenus dans
    # protocol_mapping, exposés ici pour un point de collecte unique).
    # Également lue par scripts/bench_perf.py --json.
    if isinstance(conv, dict):
        lines.append("# HELP proxy_conversion_cache_total conversions anthropic→openai")
        lines.append("# TYPE proxy_conversion_cache_total counter")
        lines.append(f'proxy_conversion_cache_total{{result="hit"}} {int(conv.get("hit", 0))}')
        lines.append(f'proxy_conversion_cache_total{{result="miss"}} {int(conv.get("miss", 0))}')
    # [plan-perf Lot 2] Failovers du watchdog TTFB paid.
    lines.append("# HELP proxy_ttfb_failover_total retentatives du watchdog TTFB paid")
    lines.append("# TYPE proxy_ttfb_failover_total counter")
    for _ka, _st in sorted(ttfb_snap.keys()):
        lines.append(
            f'proxy_ttfb_failover_total{{key_alias="{_ka}",retry_stage="{_st}"}} {ttfb_snap[(_ka, _st)]}'
        )
    return lines


def build_metrics_text(snap: MetricsSnapshot) -> str:
    """[v10 §12.2.7] Exposition Prometheus (format texte, zéro dépendance).

    Rendu pur d'un snapshot (aucun accès global, aucun raise) — byte-identique
    à l'historique pour un même état. L'hôte assemble le snapshot avec ses
    garde-fous fail-soft (sections None = sautées)."""
    lines: list[str] = []
    lines.extend(render_vpn_section(snap.vpn))
    lines.extend(render_fallback_section(snap.fb, snap.fo))
    lines.extend(render_lot0_section(snap.latency, snap.misc, snap.ttfb, snap.conv))
    return "\n".join(lines) + "\n"


__all__ = [
    "FALLBACK_CAUSES",
    "LATENCY_GAUGES",
    "LAT_RING_MAX",
    "MetricsSnapshot",
    "bump_failover_counter",
    "bump_fallback_counter",
    "bump_misc_counter",
    "build_metrics_text",
    "fallback_cause",
    "latency_snapshot",
    "new_metrics_lock",
    "observe_latency_ms",
    "percentile",
    "render_fallback_section",
    "render_lot0_section",
    "render_vpn_section",
    "reset_fallback_metrics",
]
