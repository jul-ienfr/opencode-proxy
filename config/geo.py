"""config.geo — résolution géographique (Phase 1 refonte).

Déplacé depuis ``config/settings.py`` (déplacement pur) : cache epoch +
fonctions (``_bump_geo_cache``, ``_server_countries_set``,
``_resolve_geo_extends``, ``_normalize_geo_list``, ``resolve_geo``,
``geo_strict_union``).

AUCUN import du projet (``_vpn_normalize_country`` importé en guarded
directement, comme l'historique). ``logger`` = ``"config.settings"`` (nom
historique préservé). Tout l'état qui appartient au STORE reste possédé
par l'hôte et PASSÉ EN PARAMÈTRE (snapshots ``GEO_ENABLED`` /
``GEO_POLICIES`` / ``IP_ROTATION`` lus À L'APPEL par les wrappers
``config.settings`` — hot-reload + dashboard `/api/geo*` inchangés) ;
le cache mémo + epoch + plafond vivent ici (aucun lecteur externe —
vérifié par grep).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("config.settings")

try:
    from vpn.manager import (
        _normalize_country as _vpn_normalize_country,
    )  # single source (no duplication)
except ImportError:

    def _vpn_normalize_country(name: str) -> str:  # fallback before vpn_manager importable
        c = name.strip().replace("_", " ").strip().title()
        return c


# [P1 perf] mémo resolve_geo — epoch bumpé à chaque hot-reload touchant les
# entrées geo/routes ; clé complète dans resolve_geo (contenu + inputs).
_geo_resolve_cache: dict = {}
_GEO_RESOLVE_CACHE_MAX = 1024
_geo_cache_epoch = 0


def bump_geo_cache() -> None:
    global _geo_cache_epoch
    _geo_cache_epoch += 1
    _geo_resolve_cache.clear()


# Alias historique (config.settings._bump_geo_cache — wrapper hôte conservé).
_bump_geo_cache = bump_geo_cache


def server_countries_set(ip_rotation: dict, normalize_fn=_vpn_normalize_country) -> set:
    """Normalized set(server_countries) via single-source _vpn_normalize_country."""
    raw = ip_rotation.get("server_countries", "") if isinstance(ip_rotation, dict) else ""
    if isinstance(raw, list):
        parts = [str(p).strip() for p in raw if str(p).strip()]
    elif isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        parts = []
    return {normalize_fn(p) for p in parts if p}


# Alias historique (config.settings._server_countries_set — wrapper hôte conservé).
_server_countries_set = server_countries_set


def resolve_geo_extends(raw_geo: dict, policies: dict, log=logger) -> dict:
    """Resolve geo.extends: shallow copy of GEO_POLICIES[name] merged with overrides (route wins)."""
    if not isinstance(raw_geo, dict):
        return {}
    extends = raw_geo.get("extends")
    if not extends:
        return dict(raw_geo)
    if not isinstance(extends, str):
        log.warning("[geo] extends must be str, got %r — dropping extends", extends)
        d = dict(raw_geo)
        d.pop("extends", None)
        return d
    base = (policies or {}).get(extends)
    if not isinstance(base, dict):
        log.warning("[geo] extends=%r not found in geo.policies — dropping extends", extends)
        d = dict(raw_geo)
        d.pop("extends", None)
        return d
    merged = dict(base)
    for k, v in raw_geo.items():
        if k == "extends":
            continue
        merged[k] = v
    return merged


# Alias historique (config.settings._resolve_geo_extends — wrapper hôte conservé).
_resolve_geo_extends = resolve_geo_extends


def normalize_geo_list(countries, server_set: set, normalize_fn=_vpn_normalize_country, log=logger) -> tuple[set, list]:
    """Normalize list via _vpn_normalize_country, drop invalid (WARN), dedup. Returns (valid_set, dropped)."""
    valid: set = set()
    dropped: list = []
    if not countries:
        return valid, dropped
    if not isinstance(countries, (list, tuple)):
        log.warning("[geo] countries must be list, got %r — dropping", type(countries).__name__)
        return valid, dropped
    for c in countries:
        if not isinstance(c, str) or not c.strip():
            dropped.append(c)
            continue
        norm = normalize_fn(c)
        if norm not in server_set:
            log.warning(
                "[geo] country %r → %r not in server_countries — dropping (intersection check)",
                c,
                norm,
            )
            dropped.append(c)
            continue
        valid.add(norm)
    return valid, dropped


# Alias historique (config.settings._normalize_geo_list — wrapper hôte conservé).
_normalize_geo_list = normalize_geo_list


def resolve_geo(
    route: dict,
    *,
    enabled: bool,
    policies: dict,
    ip_rotation: dict,
    normalize_fn=_vpn_normalize_country,
    log=logger,
) -> dict:
    """Resolve effective geo for a route.

    Returns {effective_allowed: set, mode: str, require_vpn: bool, geo_status: str}
    where geo_status is ok|misconfigured|disabled.
    Validation after normalization against set(server_countries normalized).
    Precedence blocked > allowed: effective = (allowed - blocked) ∩ server_countries
    else server_countries - blocked if only blocked. Empty effective + strict => misconfigured.
    """
    if not isinstance(route, dict):
        return {
            "effective_allowed": set(),
            "mode": "strict",
            "require_vpn": False,
            "geo_status": "disabled" if not enabled else "ok",
        }
    raw_geo = route.get("geo")
    if not raw_geo or not isinstance(raw_geo, dict):
        return {
            "effective_allowed": set(),
            "mode": "strict",
            "require_vpn": False,
            "geo_status": "disabled" if not enabled else "ok",
        }
    if not enabled:
        # Kill-switch: passthrough but still report disabled status (P1 no enforcement)
        return {
            "effective_allowed": set(),
            "mode": str(raw_geo.get("mode", "strict")),
            "require_vpn": bool(raw_geo.get("require_vpn", False)),
            "geo_status": "disabled",
        }
    # [P1 perf] mémo résolution : la clé couvre TOUTES les entrées qui
    # influencent le résultat (contenu geo, flag global, server_countries,
    # identité GEO_POLICIES + epoch bumpé au reload) — un monkeypatch/test ou
    # un hot-reload produit donc forcément une clé différente.
    server_set = server_countries_set(ip_rotation, normalize_fn)
    cache_key = (
        _geo_cache_epoch,
        repr(raw_geo),
        tuple(sorted(server_set)),
        id(policies),
    )
    cached = _geo_resolve_cache.get(cache_key)
    if cached is not None:
        return {
            "effective_allowed": set(cached[0]),
            "mode": cached[1],
            "require_vpn": cached[2],
            "geo_status": cached[3],
        }
    geo = resolve_geo_extends(raw_geo, policies, log)
    mode = str(geo.get("mode", "strict")).lower()
    if mode not in ("strict", "prefer", "warn"):
        log.warning("[geo] invalid mode %r — fallback to strict", mode)
        mode = "strict"
    require_vpn = bool(geo.get("require_vpn", False))
    allowed_raw = geo.get("allowed_countries", None)
    blocked_raw = geo.get("blocked_countries", None)
    # Normalize (invalid WARN+drop)
    allowed_set: set = set()
    blocked_set: set = set()
    if allowed_raw is not None:
        allowed_set, _ = normalize_geo_list(allowed_raw, server_set, normalize_fn, log)
    if blocked_raw is not None:
        blocked_set, _ = normalize_geo_list(blocked_raw, server_set, normalize_fn, log)
    # DRY note: blocked > allowed (dedup)
    if allowed_set and blocked_set:
        overlap = allowed_set & blocked_set
        if overlap:
            log.warning("[geo] blocked > allowed overlap %r — blocked wins", sorted(overlap))
        allowed_set = allowed_set - blocked_set
    has_allowed = allowed_raw is not None
    has_blocked = blocked_raw is not None

    def _cached(effective: set, m: str, rv: bool, status: str) -> dict:
        if len(_geo_resolve_cache) >= _GEO_RESOLVE_CACHE_MAX:
            _geo_resolve_cache.clear()
        _geo_resolve_cache[cache_key] = (frozenset(effective), m, rv, status)
        return {
            "effective_allowed": set(effective),
            "mode": m,
            "require_vpn": rv,
            "geo_status": status,
        }

    if not has_allowed and not has_blocked:
        return _cached(set(server_set) if server_set else set(), mode, require_vpn, "ok")
    if allowed_set and blocked_set:
        effective = (allowed_set - blocked_set) & server_set
    elif allowed_set:
        effective = allowed_set & server_set
    elif blocked_set:
        effective = server_set - blocked_set
    else:
        # Both normalized empty after WARN drops
        if has_allowed:
            # allowed declared but nothing valid => empty effective => misconfigured in strict
            effective = set()
        else:
            # only blocked declared but all invalid => nothing to block
            return _cached(
                set(server_set) if server_set else set(), mode, require_vpn, "ok"
            )
    geo_status = "misconfigured" if (not effective and mode == "strict") else "ok"
    return _cached(effective, mode, require_vpn, geo_status)


def geo_strict_union(
    *,
    enabled: bool,
    policies: dict,
    ip_rotation: dict,
    normalize_fn=_vpn_normalize_country,
    log=logger,
) -> set:
    """Union des effective_allowed de toutes les policies strict+require_vpn (Axe B).

    Vide si GEO désactivé ou aucune policy strict. Consulté par vpn_manager
    pour filtrer les rotations géo-restricted.
    """
    if not enabled:
        return set()
    union: set = set()
    for _name, _pol in policies.items() if isinstance(policies, dict) else []:
        # La policy brute peut ne pas avoir blocked/allowed — on passe par
        # un faux route {geo: {extends: name}} pour réutiliser resolve_geo
        # (normalisation + intersection server_countries).
        try:
            _info = resolve_geo(
                {"geo": {"extends": _name}},
                enabled=enabled,
                policies=policies,
                ip_rotation=ip_rotation,
                normalize_fn=normalize_fn,
                log=log,
            )
        except Exception:
            continue
        if (
            _info.get("mode") == "strict"
            and _info.get("require_vpn")
            and _info.get("geo_status") != "misconfigured"
        ):
            eff = _info.get("effective_allowed")
            if isinstance(eff, set):
                union |= eff
    return union


__all__ = [
    "_GEO_RESOLVE_CACHE_MAX",
    "_bump_geo_cache",
    "_geo_cache_epoch",
    "_geo_resolve_cache",
    "_normalize_geo_list",
    "_resolve_geo_extends",
    "_server_countries_set",
    "_vpn_normalize_country",
    "bump_geo_cache",
    "geo_strict_union",
    "normalize_geo_list",
    "resolve_geo",
    "resolve_geo_extends",
    "server_countries_set",
]
