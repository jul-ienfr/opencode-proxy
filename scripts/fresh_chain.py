#!/usr/bin/env python3
"""Chain through NordVPN WG candidates until one returns a real free 200.

This is the runtime counterpart of the discovery scan: on every candidate it
rebinds the station via Compose (hostname + country), waits for a real egress,
probes the official free endpoint, records the result in the ledger and moves
to the next candidate on 429/no-egress.  It stops as soon as one candidate is
200, leaving the station bound to it.

Usage:
    python scripts/fresh_chain.py --station 5
    python scripts/fresh_chain.py --station 6 --start 200
    python scripts/fresh_chain.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
from config.settings import yaml_get  # noqa: E402

API_URL = "https://api.nordvpn.com/v1/servers?limit=0"
STATE = ROOT / "logs" / "fresh_chain_state.json"
CANDIDATES_CACHE = ROOT / "logs" / "fresh_chain_candidates.json"
API_CACHE = ROOT / "logs" / "nordvpn_api.json"
EMBEDDED = ROOT / "logs" / "gluetun_nordvpn_embedded.json"


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


def _fetch_api() -> list[dict]:
    if API_CACHE.exists():
        try:
            raw = json.loads(API_CACHE.read_text(encoding="utf-8"))
            if isinstance(raw, list) and len(raw) > 1000:
                return raw
        except Exception:
            pass
    log("fetching NordVPN API")
    response = requests.get(API_URL, timeout=180)
    response.raise_for_status()
    raw = response.json()
    API_CACHE.parent.mkdir(parents=True, exist_ok=True)
    API_CACHE.write_text(json.dumps(raw), encoding="utf-8")
    return raw


def _registry_hosts() -> list[str]:
    path = ROOT / "logs" / "ip_registry.db"
    if not path.exists():
        return []
    try:
        with sqlite3.connect(path) as conn:
            rows = list(
                conn.execute(
                    "select hostname from ip_state "
                    "where last_status = 200 or state in ('fresh','candidate','assigned') "
                    "order by last_status desc, last_ok_at desc"
                )
            )
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        return []


def _active_hosts() -> set[str]:
    try:
        data = requests.get("http://127.0.0.1:4000/api/vpn-status", timeout=20).json()
    except Exception:
        return set()
    hosts = set()
    for station in data.get("stations", []):
        vpn = station.get("vpn") or {}
        for key in ("pinned_hostname", "current_host"):
            value = str(vpn.get(key) or "").strip()
            if value:
                hosts.add(value)
    return hosts


def _has_technology(item: dict, identifier: str) -> bool:
    return any(
        isinstance(t, dict) and t.get("identifier") == identifier
        for t in (item.get("technologies") or [])
    )


def _matches_transport(item: dict, transport: str) -> bool:
    if transport == "openvpn-tcp":
        return _has_technology(item, "openvpn_tcp")
    if transport == "openvpn-udp":
        return _has_technology(item, "openvpn_udp")
    return _has_technology(item, "wireguard_udp")


def _embedded_matches_transport(item: dict, transport: str) -> bool:
    vpn = str(item.get("vpn") or "").strip().lower()
    if transport.startswith("openvpn"):
        return vpn == "openvpn"
    return vpn == "wireguard"


def _is_wireguard(item: dict) -> bool:
    return _matches_transport(item, "wireguard")


def _host_quality() -> dict[str, dict]:
    """Measured quality per hostname from ``requests`` (7 days).

    This is the only performance truth: the API ``load`` is an aggregate
    prior, not a speed measurement.
    """

    db = ROOT / "logs" / "requests.db"
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except Exception:
        return {}
    try:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(requests)")}
        if "server_host" not in columns:
            return {}
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7 * 86400)
        )
        rows = conn.execute(
            """
            SELECT server_host AS host,
                   COUNT(*) AS n,
                   AVG(tokens_output * 1000.0 / NULLIF(duration_ms, 0)) AS tps,
                   MAX(timestamp) AS last
            FROM requests
            WHERE timestamp >= ?
              AND server_host IS NOT NULL AND server_host != ''
              AND success = 1
              AND tokens_output > 0
              AND duration_ms > 0
            GROUP BY server_host
            """,
            (cutoff,),
        ).fetchall()
        return {
            str(r["host"]): {
                "n": int(r["n"]),
                "tps": float(r["tps"] or 0.0),
                "last": str(r["last"] or ""),
            }
            for r in rows
        }
    finally:
        conn.close()


def build_candidates(transport: str = "wireguard") -> list[dict]:
    api = _fetch_api()
    embedded = []
    if EMBEDDED.exists():
        try:
            payload = json.loads(EMBEDDED.read_text(encoding="utf-8"))
            embedded = payload.get("servers") or []
        except Exception:
            embedded = []

    active = _active_hosts()
    seen: set[str] = set()
    quality = _host_quality()
    has_200 = set(_registry_hosts())
    qcfg = yaml_get("ip_rotation", "quality_preselection", {}) or {}
    try:
        fast_tps = float((qcfg or {}).get("fast_tps", 50.0))
    except (TypeError, ValueError):
        fast_tps = 50.0
    try:
        slow_tps = float((qcfg or {}).get("slow_tps", 20.0))
    except (TypeError, ValueError):
        slow_tps = 20.0

    def add(rows: list[dict], item: dict, source: str) -> None:
        host = str(item.get("hostname") or "").strip()
        if not host or host in seen or host in active:
            return
        country = str(item.get("country") or "").strip()
        if not country:
            loc = (item.get("locations") or [{}])[0]
            if isinstance(loc, dict):
                country = str(((loc.get("country") or {}) if isinstance(loc.get("country"), dict) else {}).get("name") or "")
        if not country:
            return
        try:
            load = int(item.get("load") or 100)
        except (TypeError, ValueError):
            load = 100
        ips = [
            str((entry.get("ip") or {}).get("ip") or "")
            for entry in (item.get("ips") or [])
            if isinstance(entry, dict)
        ]
        ips = [ip for ip in ips if ip]
        entry_ip = str(item.get("station") or (ips[0] if ips else ""))
        seen.add(host)
        rows.append(
            {
                "host": host,
                "country": country,
                "source": source,
                "load": load,
                "ip": entry_ip,
                "ips": ips,
            }
        )

    rows: list[dict] = []
    api_by_host = {
        str(item.get("hostname") or ""): item
        for item in api
        if isinstance(item, dict)
    }

    # 1. Every old 200 hostname that supports the transport.
    for host in _registry_hosts():
        item = api_by_host.get(host)
        if item is not None and not _matches_transport(item, transport):
            continue
        if item is None and transport != "wireguard":
            continue
        add(rows, item or {"hostname": host, "country": "Albania"}, "registry-200")

    # 2. Embedded catalog (may contain servers omitted by the public API).
    for item in embedded:
        if _embedded_matches_transport(item, transport):
            add(rows, item, "embedded")

    # 3. Public API candidates.
    public = []
    for item in api:
        if item.get("status") != "online" or not _matches_transport(item, transport):
            continue
        station_ip = str(item.get("station") or "")
        loc = (item.get("locations") or [{}])[0]
        country = str(((loc.get("country") or {}) if isinstance(loc, dict) else {}).get("name") or "")
        if not station_ip or not country:
            continue
        public.append((str(item.get("created_at") or ""), station_ip, item))

    seen_net: set[str] = set()
    for _, station_ip, item in public:
        net = ".".join(station_ip.split(".")[:3])
        if net in seen_net:
            continue
        seen_net.add(net)
        add(rows, item, "public-1-per-24")
    for _, _, item in public:
        add(rows, item, "public-rest")

    def _candidate_key(row: dict) -> tuple:
        host = row["host"]
        load = int(row.get("load") or 100)
        q = quality.get(host)
        if q and int(q.get("n") or 0) >= 3:
            tps = float(q.get("tps") or 0.0)
            if tps >= fast_tps:
                return (0, -tps, load, host)
            if tps >= slow_tps:
                return (1, -tps, load, host)
            return (4, -tps, load, host)
        if host in has_200:
            return (2, 0.0, load, host)
        return (3, 0.0, load, host)

    rows.sort(key=_candidate_key)
    top = ", ".join(
        f"{row['host']}({row['source']})" for row in rows[:3]
    )
    log(
        f"candidate quality: {len(quality)} measured, {len(has_200)} known-200; "
        f"top={top or '-'}"
    )
    return rows


def station_container_names(names, station: int) -> list[str]:
    """Return the containers owned by ``station`` (exact match only).

    Compose can leave hashed-prefix containers (``<id>_opencode-vpn-5``)
    after a failed ``up --force-recreate``; those must be removed before the
    next bind. A substring match is NOT safe here: station 1's service is
    ``opencode-vpn`` and would also match ``opencode-vpn-2`` … ``-10``,
    deleting the whole serving fleet on every chain iteration.
    """
    service = "opencode-vpn" if int(station) == 1 else f"opencode-vpn-{int(station)}"
    return [name for name in names if name == service or name.endswith("_" + service)]


def _clean_station_container(station: int) -> None:
    """Remove stale/duplicated Compose containers for THIS station only.

    Failed `up --force-recreate` can leave hashed-prefix containers
    (``<id>_opencode-vpn-5``) that make the next compose call fail with
    "Conflict. The container name ... is already in use".
    """
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            timeout=30,
            creationflags=_NO_WINDOW,
        ).stdout
    except Exception:
        return
    names = out.decode("utf-8", "ignore").splitlines()
    for name in station_container_names(names, station):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, creationflags=_NO_WINDOW)


def _state_path(station: int, transport: str = "wireguard") -> Path:
    suffix = "" if transport == "wireguard" else f"_{transport}"
    return ROOT / "logs" / f"fresh_chain_state_{int(station)}{suffix}.json"


def _lock_path(station: int, transport: str = "wireguard") -> Path:
    suffix = "" if transport == "wireguard" else f"_{transport}"
    return ROOT / "logs" / f"fresh_chain_{int(station)}{suffix}.lock"


def acquire_lock(station: int, transport: str = "wireguard") -> bool:
    """One chain per (station, transport); a stale lock expires after one hour."""
    path = _lock_path(station, transport)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(data.get("ts") or 0.0) < 3600.0:
                return False
        except Exception:
            pass
    path.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
    return True


def release_lock(station: int, transport: str = "wireguard") -> None:
    try:
        _lock_path(station, transport).unlink()
    except Exception:
        pass


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"index": 0, "results": [], "winner": None}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _active_egress_ips(api_base: str, exclude_station: int) -> set[str]:
    """Egress IPs currently used by the serving stations."""
    try:
        data = requests.get(api_base + "/api/vpn-status", timeout=20).json()
    except Exception:
        return set()
    out = set()
    for station in data.get("stations", []):
        if int(station.get("station") or 0) == int(exclude_station):
            continue
        ip = str(station.get("current_ip") or "").strip()
        if ip:
            out.add(ip)
    return out


def _ledger_exhausted_ips(api_base: str) -> set[str]:
    try:
        data = requests.get(api_base + "/api/pool-status", timeout=30).json()
    except Exception:
        return set()
    now = time.time()
    out = set()
    for bucket in ((data.get("quota_ledger") or {}).get("buckets") or []):
        if bucket.get("exhausted") and float(bucket.get("reset_at") or 0) > now:
            out.add(str(bucket.get("ip") or ""))
    return out


def mark(
    api_base: str,
    ip: str,
    status: int,
    retry_after: float,
    station: int,
    model: str,
    hostname: str = "",
) -> None:
    body: dict[str, Any] = {
        "ip": ip,
        "model": model,
        "status": status,
        "station": station,
    }
    if hostname:
        body["hostname"] = hostname
    if status == 429:
        body["retry_after"] = retry_after
    try:
        requests.post(api_base + "/api/free/quota/mark", json=body, timeout=60)
    except Exception:
        pass


def _record_registry_winner(
    station: int,
    host: str,
    country: str,
    ip: str,
    latency_ms: float | None,
) -> None:
    """Persist the tested winner in the shared IP registry (test-then-keep).

    The catalog claims DB says who tested what; this makes the assignment
    authoritative for the allocator/dashboard: one station, one egress.
    """

    if not ip:
        return
    try:
        from free.ip_registry import SqliteIpRegistry
        from free.ip_registry.config import load_registry_config

        registry = SqliteIpRegistry.from_config(load_registry_config())
        try:
            record = registry.assign_observed(
                egress_ip=ip,
                hostname=host,
                country=country,
                station=int(station),
                latency_ms=latency_ms,
                tested_at=time.time(),
            )
            if record is not None:
                log(f"registry: station {station} keeps {ip} ({host})")
            else:
                log(f"registry: station {station} could not claim {ip} (owned elsewhere)")
        finally:
            registry.close()
    except Exception as exc:  # noqa: BLE001 - the chain must continue
        log(f"registry assignment failed: {type(exc).__name__}: {exc}")


async def _probe_with_fallbacks(probe, station: int, primary: str, fallbacks: list[str]):
    """Try the primary free model, then the other buckets before condemning IP.

    A 429 is keyed by (model, IP bucket): an egress rejected on deepseek can
    still answer 200 on glm/mimo. One 200 is enough to keep the IP.
    """

    result = await probe.probe(station, primary)
    if result.status == 200:
        return result, primary
    for model in fallbacks:
        if not model or model == primary:
            continue
        alt = await probe.probe(station, model)
        if alt.status == 200:
            return alt, model
    return result, primary


async def _run_claims(args, candidates, binder, probe, state_path, state) -> int:
    """Parallel-safe chain: one shared catalog, exclusive hostname claims."""
    from free.catalog_claims import CatalogClaims

    claims = CatalogClaims(
        args.claims_db,
        rebind_limit_per_min=max(1, int(args.rebind_limit)),
    )
    try:
        seeded = claims.seed(candidates)
        log(
            f"claims mode: catalog={seeded} db={args.claims_db} "
            f"rebind_limit={args.rebind_limit}/min station={args.station} "
            f"first={[str(c.get('host')) for c in candidates[:3]]}"
        )
        tested = 0
        while True:
            if args.max and tested >= args.max:
                log(f"claims mode: max={args.max} reached")
                return 2
            claim = claims.claim_next(args.station)
            if claim is None:
                log("catalog exhausted or every candidate is claimed/reset")
                return 2
            host = str(claim["host"])
            country = str(claim["country"])
            tested += 1
            while not claims.rebind_allowed():
                await asyncio.sleep(2)
            log(f"claim {host} ({country})")
            _clean_station_container(args.station)
            bind = await binder.assign(args.station, host, country=country)
            if not bind.ok:
                log(f"  bind rejected: {bind.error}")
                claims.record(host, args.station, "bind_error", error=bind.error or "")
                row = {"host": host, "country": country, "status": "bind-error", "error": bind.error}
                state["results"] = (state.get("results") or [])[-499:] + [row]
                save_state(state_path, state)
                await asyncio.sleep(args.delay)
                continue
            ip = str(bind.egress_ip or "")
            log(f"  egress {ip}")
            if ip and ip in _active_egress_ips(args.api_base, args.station):
                log("  egress already used by a serving station — release")
                claims.record(host, args.station, "duplicate", ip=ip)
                await asyncio.sleep(args.delay)
                continue
            result, model_used = await _probe_with_fallbacks(
                probe, args.station, args.model, args.probe_models
            )
            log(
                f"  free status={result.status} ra={result.retry_after_s} "
                f"model={model_used}"
            )
            row = {
                "host": host,
                "country": country,
                "ip": ip,
                "status": result.status,
                "retry_after": result.retry_after_s,
                "model": model_used,
            }
            state["results"] = (state.get("results") or [])[-499:] + [row]
            if result.status == 200:
                claims.record(host, args.station, "ok", ip=ip)
                mark(
                    args.api_base,
                    ip,
                    200,
                    0.0,
                    args.station,
                    model_used,
                    hostname=host,
                )
                _record_registry_winner(
                    args.station, host, country, ip, getattr(result, "latency_ms", None)
                )
                state["winner"] = {
                    "host": host,
                    "country": country,
                    "ip": ip,
                    "model": model_used,
                }
                save_state(state_path, state)
                log(f"WINNER station={args.station} host={host} egress={ip} model={model_used}")
                return 0
            if result.status == 429:
                claims.record(
                    host,
                    args.station,
                    "429",
                    ip=ip,
                    retry_after=result.retry_after_s,
                )
                mark(
                    args.api_base,
                    ip,
                    429,
                    result.retry_after_s,
                    args.station,
                    model_used,
                )
            else:
                claims.record(
                    host,
                    args.station,
                    "no_egress",
                    ip=ip,
                    error=str(result.status),
                )
            save_state(state_path, state)
            await asyncio.sleep(args.delay)
    finally:
        claims.close()


async def run(args) -> int:
    from free.ip_registry import ComposeHostnameBinder, StationEgress, StationProbe

    if not acquire_lock(args.station, args.transport):
        log(f"chain already running for station {args.station} transport={args.transport} — exit")
        return 0
    atexit.register(release_lock, args.station, args.transport)

    state_path = _state_path(args.station, args.transport)
    state = load_state(state_path)
    candidates_cache = CANDIDATES_CACHE.with_name(
        f"fresh_chain_candidates_{args.transport}.json"
    )
    if args.candidates and Path(args.candidates).exists():
        candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    elif (
        candidates_cache.exists()
        and not args.refresh
        and time.time() - candidates_cache.stat().st_mtime < 1800.0
    ):
        candidates = json.loads(candidates_cache.read_text(encoding="utf-8"))
    else:
        log("building candidate list")
        candidates = build_candidates(args.transport)
        candidates_cache.write_text(json.dumps(candidates), encoding="utf-8")
    if not candidates:
        log("no candidates")
        return 1
    log(f"candidates={len(candidates)} start={args.start or state.get('index', 0)}")

    if args.dry_run:
        for row in candidates[: args.max or 20]:
            print(row["host"], row["country"], row["source"])
        return 0

    # Multiple independent egress services + a longer tunnel settle: a single
    # 5 s ipify attempt after 8 s declared many healthy WireGuard servers
    # "no-egress" and burned the candidate catalog.
    egress = StationEgress(timeout_s=8.0)
    probe = StationProbe(egress=egress, timeout_s=60.0)
    binder = ComposeHostnameBinder(
        ROOT,
        override_path=f"logs/compose-hostname-{args.station}.yml",
        profile="multi-station",
        settle_s=12.0,
        egress_wait_s=20.0,
        egress_poll_s=3.0,
        egress=egress,
        recreate_timeout_s=180.0,
    )
    start = int(args.start if args.start is not None else state.get("index", 0))
    vpn_type = "openvpn" if args.transport.startswith("openvpn") else None
    openvpn_protocol = args.transport.split("-", 1)[1] if vpn_type else None
    args.probe_models = [
        part.strip()
        for part in str(getattr(args, "models", "") or "").split(",")
        if part.strip() and part.strip() != args.model
    ]
    exhausted = _ledger_exhausted_ips(args.api_base)
    tested = 0
    if args.claims:
        return await _run_claims(args, candidates, binder, probe, state_path, state)
    for index in range(start, len(candidates)):
        if args.max and tested >= args.max:
            break
        if tested and args.delay > 0:
            await asyncio.sleep(args.delay)
        candidate = candidates[index]
        host = str(candidate.get("host") or "")
        country = str(candidate.get("country") or "")
        if not host:
            continue
        if tested and tested % 20 == 0:
            exhausted = _ledger_exhausted_ips(args.api_base)
            state["index"] = index
            save_state(state_path, state)
        log(f"[{index + 1}/{len(candidates)}] bind {host} ({country})")
        _clean_station_container(args.station)
        bind = await binder.assign(
            args.station,
            host,
            country=country,
            vpn_type=vpn_type,
            openvpn_protocol=openvpn_protocol,
        )
        tested += 1
        if not bind.ok:
            log(f"  bind rejected: {bind.error}")
            state["results"].append({"host": host, "country": country, "status": "bind-error", "error": bind.error})
            state["index"] = index + 1
            save_state(state_path, state)
            continue
        ip = str(bind.egress_ip or "")
        log(f"  egress {ip}")
        if ip and ip in _active_egress_ips(args.api_base, args.station):
            log("  egress already used by a serving station — skip")
            state["results"].append({"host": host, "country": country, "ip": ip, "status": "duplicate-active"})
            state["index"] = index + 1
            save_state(state_path, state)
            continue
        if ip and ip in exhausted:
            log("  egress already in the 429 ledger — skip probe")
            state["results"].append({"host": host, "country": country, "ip": ip, "status": "ledger-429"})
            state["index"] = index + 1
            save_state(state_path, state)
            continue
        result, model_used = await _probe_with_fallbacks(
            probe, args.station, args.model, args.probe_models
        )
        log(
            f"  free status={result.status} ra={result.retry_after_s} "
            f"model={model_used}"
        )
        state["results"].append(
            {
                "host": host,
                "country": country,
                "ip": ip,
                "status": result.status,
                "retry_after": result.retry_after_s,
                "model": model_used,
            }
        )
        state["index"] = index + 1
        save_state(state_path, state)
        if result.status == 200:
            mark(
                args.api_base,
                ip,
                200,
                0.0,
                args.station,
                model_used,
                hostname=host,
            )
            _record_registry_winner(
                args.station, host, country, ip, getattr(result, "latency_ms", None)
            )
            state["winner"] = {
                "host": host,
                "country": country,
                "ip": ip,
                "model": model_used,
            }
            save_state(state_path, state)
            log(f"WINNER station={args.station} host={host} egress={ip} model={model_used}")
            return 0
        if result.status == 429:
            mark(args.api_base, ip, 429, result.retry_after_s, args.station, args.model)
            exhausted.add(ip)
        # no-egress/probe errors: move on
    log("exhausted without a 200")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", type=int, default=5)
    parser.add_argument(
        "--transport",
        default="wireguard",
        choices=("wireguard", "openvpn-tcp", "openvpn-udp"),
    )
    parser.add_argument("--model", default="deepseek-v4.1-flash-free")
    parser.add_argument(
        "--models",
        default="",
        help="modèles de repli, séparés par des virgules (buckets distincts)",
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:4000")
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--max", type=int, default=0)
    parser.add_argument("--delay", type=float, default=5.0)
    parser.add_argument("--candidates", default="")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--claims", action="store_true")
    parser.add_argument("--claims-db", default=str(ROOT / "logs" / "catalog_claims.db"))
    parser.add_argument("--rebind-limit", type=int, default=8)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
