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
from free.chain_repair import acquire_lock as _acquire_lock  # noqa: E402
from free.chain_repair import exhausted_egress as _exhausted_egress  # noqa: E402
from free.chain_repair import (  # noqa: E402
    filter_candidates_for_transport as filter_candidates_for_transport,
)
from free.chain_repair import pid_alive as _pid_alive  # noqa: E402
from free.chain_repair import release_lock as _release_lock  # noqa: E402
from free.chain_repair import transport_parts as _transport_parts  # noqa: E402
from free.qualification.chain_bridge import (  # noqa: E402
    record_chain_attempt as _record_chain_attempt,
)

API_URL = "https://api.nordvpn.com/v1/servers?limit=0"
CANDIDATES_CACHE = ROOT / "logs" / "fresh_chain_candidates.json"
API_CACHE = ROOT / "logs" / "nordvpn_api.json"
EMBEDDED = ROOT / "logs" / "gluetun_nordvpn_embedded.json"
GLUETUN_MANIFEST_CACHE = ROOT / "logs" / "gluetun_nordvpn_servers.json"
API_CACHE_TTL_S = 6 * 3600.0
MANIFEST_CACHE_TTL_S = 600.0
CONTAINERS = ("opencode-vpn",) + tuple(
    f"opencode-vpn-{station}" for station in range(2, 11)
)


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


def _fetch_api() -> list[dict]:
    """Return the official catalog, refreshing the on-disk copy every 6 h.

    The previous implementation reused ``logs/nordvpn_api.json`` forever.  That
    made the chain test hosts that had disappeared from the VPN provider while
    claiming the catalog was current.  A short TTL keeps the source honest and
    still falls back to the last good snapshot when the API is unavailable.
    """

    cached: list[dict] | None = None
    if API_CACHE.exists():
        try:
            raw = json.loads(API_CACHE.read_text(encoding="utf-8"))
            if isinstance(raw, list) and len(raw) > 1000:
                cached = raw
                age = time.time() - API_CACHE.stat().st_mtime
                if age < API_CACHE_TTL_S:
                    return raw
        except Exception:
            cached = None
    log("fetching NordVPN API")
    try:
        response = requests.get(API_URL, timeout=180)
        response.raise_for_status()
        raw = response.json()
    except Exception as exc:  # noqa: BLE001 - stale cache is still usable
        if cached is not None:
            log(f"NordVPN API unavailable ({type(exc).__name__}) — using cached catalog")
            return cached
        raise
    API_CACHE.parent.mkdir(parents=True, exist_ok=True)
    API_CACHE.write_text(json.dumps(raw), encoding="utf-8")
    return raw


def _container_json(name: str, path: str) -> dict | None:
    """Read a JSON file from a running container, without shell quoting."""

    try:
        proc = subprocess.run(
            ["docker", "exec", name, "cat", path],
            capture_output=True,
            timeout=60,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return None
    if int(getattr(proc, "returncode", 1)) != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _merge_manifests(payloads: list[dict]) -> dict | None:
    """Union of Gluetun manifests, newest entry wins per (host, vpn).

    The newest manifest removes servers that still work (for example the
    Albanian hosts that keep producing fresh egress), while the embedded
    catalog keeps them.  Gluetun only rejects a hostname that is absent from
    *its* manifest, so the union is the correct candidate universe: every host
    in it is bindable, and no working server is lost to a catalog refresh.
    """

    rows: dict[tuple[str, str], tuple[float, dict]] = {}
    timestamp = 0.0
    version = 0
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        try:
            ts = float(payload.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        try:
            version = max(version, int(payload.get("version") or 0))
        except (TypeError, ValueError):
            pass
        timestamp = max(timestamp, ts)
        for item in payload.get("servers") or []:
            if not isinstance(item, dict):
                continue
            host = str(item.get("hostname") or "").strip()
            if not host:
                continue
            key = (host, str(item.get("vpn") or "").strip().lower())
            previous = rows.get(key)
            if previous is None or ts >= previous[0]:
                rows[key] = (ts, item)
    if not rows:
        return None
    return {
        "version": version or 5,
        "timestamp": int(timestamp),
        "merged": True,
        "servers": [item for _ts, item in rows.values()],
    }


def load_runtime_manifest(*, refresh: bool = True) -> dict | None:
    """Return the union of every Gluetun NordVPN manifest available.

    Gluetun validates ``SERVER_HOSTNAMES`` against its own manifest.  The image
    embeds one, each container volume keeps the copy it had at creation, and
    the manager may refresh it.  A candidate that is absent from the manifest is
    rejected at startup with ``the hostname specified is not valid`` — before
    any tunnel is attempted.  Keeping the union of every snapshot (including
    the embedded catalog) means old-but-working servers stay usable and the
    newest servers are added.
    """

    payloads: list[dict] = []
    cached: dict | None = None
    if GLUETUN_MANIFEST_CACHE.exists():
        try:
            raw = json.loads(GLUETUN_MANIFEST_CACHE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("servers"):
                cached = raw
        except Exception:
            cached = None

    if not refresh and cached is not None:
        return cached
    if cached is not None and cached.get("merged"):
        try:
            age = time.time() - GLUETUN_MANIFEST_CACHE.stat().st_mtime
            if age < MANIFEST_CACHE_TTL_S:
                return cached
        except OSError:
            pass

    if cached is not None:
        payloads.append(cached)
    if EMBEDDED.exists():
        try:
            embedded = json.loads(EMBEDDED.read_text(encoding="utf-8"))
            if isinstance(embedded, dict) and embedded.get("servers"):
                payloads.append(embedded)
        except Exception:
            pass
    for name in CONTAINERS:
        payload = _container_json(name, "/gluetun/servers/nordvpn.json")
        if payload and payload.get("servers"):
            payloads.append(payload)
    merged = _merge_manifests(payloads)
    if merged is not None:
        try:
            tmp = GLUETUN_MANIFEST_CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps(merged), encoding="utf-8")
            os.replace(tmp, GLUETUN_MANIFEST_CACHE)
        except Exception:
            pass
    return merged


def manifest_hosts(
    payload: dict | None, transport: str = "wireguard"
) -> dict[str, str]:
    """Map hostname → country for hosts accepted by Gluetun for this transport."""

    if not isinstance(payload, dict):
        return {}
    rows = payload.get("servers") or []
    if not isinstance(rows, list):
        return {}
    openvpn = str(transport).startswith("openvpn")
    protocol = str(transport).split("-", 1)[1] if openvpn and "-" in str(transport) else ""
    hosts: dict[str, str] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        host = str(item.get("hostname") or "").strip()
        if not host:
            continue
        vpn = str(item.get("vpn") or "").strip().lower()
        if openvpn:
            if vpn != "openvpn":
                continue
            if protocol == "tcp" and not item.get("tcp"):
                continue
            if protocol == "udp" and not item.get("udp"):
                continue
        else:
            if vpn != "wireguard" or not item.get("wgpubkey"):
                continue
        hosts[host] = str(item.get("country") or "").strip()
    return hosts


def _station_volume_name(station: int) -> str:
    """Named volume that holds ``/gluetun`` for this station."""

    container = "opencode-vpn" if int(station) == 1 else f"opencode-vpn-{int(station)}"
    try:
        proc = subprocess.run(
            [
                "docker",
                "inspect",
                container,
                "--format",
                '{{range .Mounts}}{{if eq .Destination "/gluetun"}}{{.Name}}{{end}}{{end}}',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_NO_WINDOW,
        )
        name = (proc.stdout or "").strip()
        if int(getattr(proc, "returncode", 1)) == 0 and name:
            return name
    except Exception:
        pass
    suffix = "" if int(station) == 1 else str(int(station))
    return f"opencode-proxy-one_gluetun{suffix}"


def sync_manifest_to_station(station: int, payload: dict | None) -> bool:
    """Install ``payload`` as this station's Gluetun manifest.

    The volume is the durable part of the container.  Updating the file before
    ``compose up --force-recreate`` means the new container starts with the
    same candidate set the chain just selected, even when its volume was
    created months earlier.
    """

    if not isinstance(payload, dict) or not payload.get("servers"):
        return False
    station = int(station)
    tmp = ROOT / "logs" / f"gluetun_manifest_station{station}.json"
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        volume = _station_volume_name(station)
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume}:/gluetun",
                "-v",
                f"{tmp}:/tmp/nordvpn.json:ro",
                "--entrypoint",
                "sh",
                "qmcgaw/gluetun@sha256:4af0c804003481e100180e1f8d4d5e9c48163fedc0f88d061881b615aef9d183",
                "-c",
                "mkdir -p /gluetun/servers && "
                "cp /tmp/nordvpn.json /gluetun/servers/nordvpn.json.tmp && "
                "mv /gluetun/servers/nordvpn.json.tmp "
                "/gluetun/servers/nordvpn.json && "
                "test -s /gluetun/servers/nordvpn.json && "
                "grep -q '\"servers\"' /gluetun/servers/nordvpn.json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=_NO_WINDOW,
        )
        if int(getattr(proc, "returncode", 1)) != 0:
            log(
                f"manifest sync station {station} failed: "
                f"{str(getattr(proc, 'stderr', '') or '')[:300]}"
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"manifest sync station {station} failed: {type(exc).__name__}: {exc}")
        return False
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def read_station_manifest(station: int) -> dict | None:
    """Read the manifest actually installed in the station volume.

    The merged cache can be newer than the container's volume. Selecting a
    hostname from the cache while the container rejects it produced the
    ``be160`` crash loop. This read makes ``valid_hosts`` reflect the file the
    container will really use.
    """

    station = int(station)
    try:
        volume = _station_volume_name(station)
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume}:/gluetun",
                "--entrypoint",
                "sh",
                "qmcgaw/gluetun@sha256:4af0c804003481e100180e1f8d4d5e9c48163fedc0f88d061881b615aef9d183",
                "-c",
                "cat /gluetun/servers/nordvpn.json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"station manifest read {station} failed: {type(exc).__name__}: {exc}")
        return None
    if int(getattr(proc, "returncode", 1)) != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("servers"):
        return None
    return payload


def _registry_countries() -> dict[str, str]:
    """Hostname → country from the quota inventory (first non-empty wins)."""
    path = ROOT / "logs" / "registry.db"
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        with sqlite3.connect(path) as conn:
            for host, country in conn.execute(
                "select hostname, country from servers "
                "where hostname != '' and country != ''"
            ):
                key = str(host or "").strip().lower()
                if key and key not in out:
                    out[key] = str(country).strip()
    except Exception:
        return {}
    return out


def _registry_hosts(transport: str | None = None) -> list[str]:
    """Registry hostnames, transport-aware (extracted to free.chain_registry)."""

    from free.chain_registry import registry_hosts

    return registry_hosts(ROOT, transport)


def _ensure_registry_schema(path: Path) -> None:
    """Create the target schema when a fresh/temporary DB is passed.

    Production ``registry.db`` is already migrated; this only keeps tests and
    one-off tools from silently falling back to the legacy JSON layout.
    """

    try:
        conn = sqlite3.connect(path, timeout=10.0)
        try:
            has = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='warm_pool'"
            ).fetchone()
        finally:
            conn.close()
        if has:
            return
        from rotation.infrastructure.db.migrate import apply_schema, connect

        conn = connect(path)
        try:
            apply_schema(conn)
        finally:
            conn.close()
    except Exception:
        pass


def _verified_spares(
    db_path: Path | None = None,
    *,
    now: float | None = None,
    transport: str = "",
    limit: int = 24,
) -> list[dict]:
    """Hosts that produced a real 200 recently and still have a live bucket.

    This is the warm-spare cache used to put proven hosts ahead of the raw
    catalog. It is not a proactive rotation yet: it only shortens recovery
    when a station is already down.
    """

    path = db_path or (ROOT / "logs" / "registry.db")
    if not path.exists():
        return []
    _ensure_registry_schema(path)
    ts = float(now if now is not None else time.time())
    try:
        with sqlite3.connect(path) as conn:
            if transport:
                rows = list(
                    conn.execute(
                        "SELECT w.hostname, COALESCE(s.country,''), w.ip, "
                        "COALESCE(w.model,''), w.verified_at "
                        "FROM warm_pool w "
                        "LEFT JOIN servers s ON s.hostname = w.hostname "
                        "WHERE w.expires_at > ? "
                        "AND (w.transport = '' OR w.transport = ?) "
                        "ORDER BY w.verified_at DESC LIMIT ?",
                        (ts, transport, int(limit)),
                    )
                )
            else:
                rows = list(
                    conn.execute(
                        "SELECT w.hostname, COALESCE(s.country,''), w.ip, "
                        "COALESCE(w.model,''), w.verified_at "
                        "FROM warm_pool w "
                        "LEFT JOIN servers s ON s.hostname = w.hostname "
                        "WHERE w.expires_at > ? "
                        "ORDER BY w.verified_at DESC LIMIT ?",
                        (ts, int(limit)),
                    )
                )
    except Exception:
        return []
    return [
        {
            "hostname": str(row[0]),
            "country": str(row[1] or ""),
            "egress_ip": str(row[2] or ""),
            "model": str(row[3] or ""),
            "last_200_at": float(row[4] or 0.0),
        }
        for row in rows
        if row and row[0]
    ]


def _record_verified_spare(
    station: int,
    host: str,
    country: str,
    ip: str,
    model: str,
    transport: str,
    *,
    db_path: Path | None = None,
    now: float | None = None,
) -> None:
    """Persist a proven host/egress for the warm-spare cache."""

    if not host or not ip:
        return
    path = db_path or (ROOT / "logs" / "registry.db")
    _ensure_registry_schema(path)
    ts = float(now if now is not None else time.time())
    try:
        ttl = float(
            yaml_get("ip_registry", "allocator.verified_spare_ttl_s", 21600) or 21600
        )
    except Exception:
        ttl = 21600.0
    try:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO catalog_sources("
                "source_id, kind, fetched_at, stale, server_count, error"
                ") VALUES('fresh-chain','chain',?,0,0,'')",
                (ts,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO servers("
                "hostname, country, source_id, first_seen_at, last_seen_at"
                ") VALUES(?,?, 'fresh-chain', ?, ?)",
                (str(host), str(country or "unknown"), ts, ts),
            )
            conn.execute(
                "INSERT OR IGNORE INTO egress_ips("
                "ip, country, first_seen_at, last_seen_at, state"
                ") VALUES(?,?,?,?, 'fresh')",
                (str(ip), str(country or ""), ts, ts),
            )
            conn.execute(
                "INSERT OR REPLACE INTO warm_pool("
                "ip, hostname, transport, model, verified_at, expires_at, "
                "score_ip) VALUES(?,?,?,?,?,?,0)",
                (
                    str(ip),
                    str(host),
                    str(transport or ""),
                    str(model or ""),
                    ts,
                    ts + max(300.0, ttl),
                ),
            )
    except Exception:
        pass


def _active_hosts(exclude_station: int | None = None) -> set[str]:
    """Hostnames currently owned by a serving station.

    The chain that repairs a station must be allowed to reclaim **its own**
    current hostname: a station can keep a bad egress (429) while still being
    bound to the right server.  Excluding every ``current_host`` made the
    repair chain skip exactly the host that had produced the good IP, so it
    wandered through hundreds of unrelated servers instead.
    """

    try:
        data = requests.get("http://127.0.0.1:4000/api/vpn-status", timeout=20).json()
    except Exception:
        return set()
    hosts = set()
    for station in data.get("stations", []):
        try:
            sid = int(station.get("station") or 0)
        except (TypeError, ValueError):
            sid = 0
        if exclude_station is not None and sid == int(exclude_station):
            continue
        vpn = station.get("vpn") or {}
        status = str(vpn.get("status") or "")
        value = str(vpn.get("current_host") or "").strip()
        pinned = str(vpn.get("pinned_hostname") or "").strip()
        if status in ("connected", "degraded"):
            if value:
                hosts.add(value)
            # A stale pin on a connected station is not live ownership: the
            # current host is the truth. Only fall back to the pin when the
            # live host is unknown.
            elif pinned:
                hosts.add(pinned)
            continue
        # [2026-11-04] A transient error must not hand a proven host to another
        # chain: station 2 lost al41 (87.120.102.85) this way while it was
        # briefly error, and the whole fleet dropped to 0/6. Protect the pin
        # until the manager pin-escape proves it dead.
        if pinned and (not value or value == pinned):
            hosts.add(pinned)
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


def _transport_order() -> tuple[str, ...]:
    """Configured transport order for ``--transport auto``."""

    default = ["wireguard", "openvpn-udp", "openvpn-tcp"]
    raw = yaml_get("ip_rotation", "transport_order", default) or default
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    if not isinstance(raw, (list, tuple)):
        raw = default
    known = {"wireguard", "openvpn-udp", "openvpn-tcp"}
    order = [str(item).strip() for item in raw if str(item).strip() in known]
    if not order:
        order = default
    return tuple(dict.fromkeys(order))


def _read_env_key(key: str) -> str:
    """Read one key from the repository ``.env`` (last value wins)."""

    path = ROOT / ".env"
    value = ""
    try:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, item = line.partition("=")
            if name.strip() == key:
                value = item.strip().strip('"').strip("'")
    except OSError:
        return ""
    return value


def _station_transport(station: int) -> str:
    """Transport that matches the station's configured stack.

    ``fast_discovery`` still probes one transport per attempt, but it must be
    the station's stack rather than a global OpenVPN default: otherwise a
    WireGuard station silently spends every attempt on OpenVPN egress IPs.
    """

    vpn = _read_env_key(f"VPN_TYPE_STATION{int(station)}").lower()
    if not vpn:
        vpn = str(yaml_get("ip_rotation", "vpn_stack", "auto") or "auto").lower()
    if vpn == "wireguard":
        return "wireguard"
    if vpn == "openvpn":
        proto = _read_env_key("OPENVPN_PROTOCOL").lower()
        if proto not in ("tcp", "udp"):
            proto = str(yaml_get("ip_rotation", "ovpn_protocol", "udp") or "udp")
        return f"openvpn-{proto if proto in ('tcp', 'udp') else 'udp'}"
    return ""


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


def _score_weights() -> dict[str, float]:
    """Configurable composite weights (normalized)."""

    defaults = {
        "success": 0.40,
        "latency": 0.25,
        "throughput": 0.20,
        "stability": 0.10,
        "freshness": 0.05,
    }
    raw = {
        "success": yaml_get(
            "ip_registry", "selection.score_weights.success", 0.40
        ),
        "latency": yaml_get(
            "ip_registry", "selection.score_weights.latency", 0.25
        ),
        "throughput": yaml_get(
            "ip_registry", "selection.score_weights.throughput", 0.20
        ),
        "stability": yaml_get(
            "ip_registry", "selection.score_weights.stability", 0.10
        ),
        "freshness": yaml_get(
            "ip_registry", "selection.score_weights.freshness", 0.05
        ),
    }
    weights: dict[str, float] = {}
    for key, default in defaults.items():
        try:
            value = float(raw.get(key) if raw.get(key) is not None else default)
        except (TypeError, ValueError):
            value = default
        weights[key] = max(0.0, value)
    total = sum(weights.values())
    if total <= 0:
        return defaults
    return {key: value / total for key, value in weights.items()}


def _host_success() -> dict[str, dict]:
    """Recent success/latency per hostname from ``observations`` (7 days).

    The score is computed from append-only observations, not from the
    polluted ``ip_state.tests/successes`` counters.
    """

    db = ROOT / "logs" / "registry.db"
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except Exception:
        return {}
    try:
        rows = conn.execute(
            """
            SELECT hostname AS host,
                   SUM(CASE WHEN status = 200 THEN 1 ELSE 0 END) AS ok,
                   SUM(CASE WHEN status IN (0, 429) THEN 1 ELSE 0 END) AS bad,
                   AVG(CASE WHEN status = 200 THEN latency_ms END) AS latency,
                   MAX(tested_at) AS last
            FROM observations
            WHERE tested_at >= ?
              AND hostname IS NOT NULL AND hostname != ''
            GROUP BY hostname
            """,
            (time.time() - 7 * 86400,),
        ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            ok = int(r["ok"] or 0)
            bad = int(r["bad"] or 0)
            n = ok + bad
            if n <= 0:
                continue
            rate = (ok + 1.0) / (n + 2.0)  # Laplace smoothing
            latency = float(r["latency"] or 0.0)
            latency_score = 1.0 - min(1.0, latency / 8000.0) if latency > 0 else 0.5
            try:
                last = float(r["last"] or 0.0)
            except (TypeError, ValueError):
                last = 0.0
            age_h = max(0.0, (time.time() - last) / 3600.0) if last else 48.0
            freshness = max(0.0, min(1.0, 2.718281828 ** (-age_h / 48.0)))
            out[str(r["host"])] = {
                "ok": ok,
                "bad": bad,
                "n": n,
                "rate": rate,
                "latency": latency,
                "latency_score": latency_score,
                "freshness": freshness,
            }
        return out
    except Exception:
        return {}
    finally:
        conn.close()


def build_candidates(
    transport: str = "wireguard",
    valid_hosts: dict[str, str] | None = None,
    station: int | None = None,
) -> list[dict]:
    api = _fetch_api()
    embedded = []
    if EMBEDDED.exists():
        try:
            payload = json.loads(EMBEDDED.read_text(encoding="utf-8"))
            embedded = payload.get("servers") or []
        except Exception:
            embedded = []

    active = _active_hosts(station) if station is not None else _active_hosts()
    seen: set[str] = set()
    quality = _host_quality()
    success = _host_success()
    weights = _score_weights()
    has_200 = set(_registry_hosts(transport))
    registry_countries = _registry_countries()
    # [2026-11-05] No country quota: every country is eligible. Country is
    # metadata only; the selector ranks by measured performance.
    country_counts: dict[str, int] = {}
    if station is not None:
        for _st in _serving_fleet(exclude_station=station):
            _c = str(_st.get("country") or "").strip().lower()
            if _c:
                country_counts[_c] = country_counts.get(_c, 0) + 1

    def add(rows: list[dict], item: dict, source: str) -> None:
        host = str(item.get("hostname") or "").strip()
        if not host or host in seen or host in active:
            return
        if valid_hosts is not None and host not in valid_hosts:
            # Gluetun would reject this hostname before opening the tunnel.
            # Embedded/registry rows can be months old; only the runtime
            # manifest proves a host still exists.
            return
        country = str(item.get("country") or "").strip()
        if not country:
            loc = (item.get("locations") or [{}])[0]
            if isinstance(loc, dict):
                country = str(((loc.get("country") or {}) if isinstance(loc.get("country"), dict) else {}).get("name") or "")
        if not country:
            return
        if valid_hosts is not None:
            country = valid_hosts.get(host) or country
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
        # Embedded/registry rows often lack the live endpoint IP; prefer the
        # current API row for the same hostname so the exhausted-/24 filter
        # also applies to them (otherwise Canada's burned 187.15.140.0/24
        # kept coming back through the embedded tier).
        api_item = api_by_host.get(host) or {}
        entry_ip = str(
            api_item.get("station")
            or item.get("station")
            or (ips[0] if ips else "")
        )
        # The quota is per EGRESS IP, not per endpoint /24 or /16: the same
        # /16 can contain a 200 and a 429. Only the specific-IP ledger and
        # the runtime country backoff are authoritative.
        try:
            last_200 = float(item.get("last_200") or 0.0)
        except (TypeError, ValueError):
            last_200 = 0.0
        in_api = host in api_by_host
        sr = success.get(host) or {}
        try:
            success_rate = float(sr.get("rate", 0.5))
        except (TypeError, ValueError):
            success_rate = 0.5
        try:
            latency_score = float(sr.get("latency_score", 0.5))
        except (TypeError, ValueError):
            latency_score = 0.5
        q = quality.get(host) or {}
        try:
            tps = float(q.get("tps") or 0.0)
        except (TypeError, ValueError):
            tps = 0.0
        throughput_score = min(1.0, tps / 100.0)
        try:
            freshness = float(sr.get("freshness", 0.5))
        except (TypeError, ValueError):
            freshness = 0.5
        stability = 0.5  # no per-IP variance in the Phase-1 record yet
        w = weights
        # Composite score: success, latency, throughput, stability, freshness.
        # Country and source are not score components.
        score = (
            w["success"] * success_rate
            + w["latency"] * latency_score
            + w["throughput"] * throughput_score
            + w["stability"] * stability
            + w["freshness"] * freshness
        )
        if host in has_200:
            score += 0.05
        seen.add(host)
        rows.append(
            {
                "host": host,
                "country": country,
                "source": source,
                "in_api": in_api,
                "success_rate": success_rate,
                "latency_score": latency_score,
                "throughput_tps": tps,
                "score": score,
                "load": load,
                "ip": entry_ip,
                "ips": ips,
                "last_200": last_200,
                # Metadata only: no country quota is applied.
                "country_used": country_counts.get(country.lower(), 0),
            }
        )

    rows: list[dict] = []
    api_by_host = {
        str(item.get("hostname") or ""): item
        for item in api
        if isinstance(item, dict)
    }
    # 0. Warm spares: hosts that produced a real 200 recently, with a live
    # egress in the ledger. They are tried before the raw catalog.
    for spare in _verified_spares(transport=transport):
        add(
            rows,
            {
                "hostname": spare["hostname"],
                "country": spare["country"],
                "station": spare["egress_ip"],
                "last_200": spare["last_200_at"],
            },
            "spare",
        )

    # 1. Every old 200 hostname that is still in the CURRENT public API and
    # supports the transport. [2026-11-03 any-country] API-missing legacy
    # hosts used to be injected with a hard-coded "Albania" country and got
    # priority 2 below, which kept the allocator on al36/al37/al46/... even
    # after the perimeter was opened to 150 countries. The embedded catalog
    # still lists them as a LAST-RESORT fallback (step 3), with their real
    # country, but they no longer outrank the live catalog.
    for host in _registry_hosts(transport):
        item = api_by_host.get(host)
        if item is None:
            # [2026-11-03] The hostname is proven in ip_state but absent from
            # the current API snapshot (legacy server). Keep it as a registry
            # candidate when the runtime manifest accepts it; the old code
            # dropped it here and pushed it to the embedded last-resort tier.
            if valid_hosts is None or host not in valid_hosts:
                continue
            item = {
                "hostname": host,
                "country": registry_countries.get(host, ""),
                "station": "",
            }
        elif not _matches_transport(item, transport):
            continue
        add(rows, item, "registry-200")

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

    # Round-robin the public tier by country. The API returns one country's
    # servers first (Canada in the 2026-11 catalog); testing its /24s in order
    # burned the whole 187.15.0.0/16 before reaching Germany or France. The
    # rank is metadata only: every country remains eligible.
    public_rows = [
        row
        for row in rows
        if str(row.get("source") or "") != "spare"
    ]
    by_country: dict[str, list[dict]] = {}
    for row in public_rows:
        by_country.setdefault(str(row.get("country") or "").lower(), []).append(row)
    country_order = list(by_country)
    for rank in range(max((len(v) for v in by_country.values()), default=0)):
        for country_index, country in enumerate(country_order):
            if rank < len(by_country[country]):
                by_country[country][rank]["_rr"] = (rank, country_index)

    def _base_key(row: dict) -> tuple:
        host = row["host"]
        source = str(row.get("source") or "")
        load = int(row.get("load") or 100)
        if source == "spare":
            return (
                -1,
                -float(row.get("last_200") or 0.0),
                0,
                load,
                0,
                (0, 0),
                host,
            )
        # Performance first; source is only a tiebreaker (API before
        # Gluetun-only), then API load, then the round-robin rank BEFORE the
        # hostname. The rank must precede ``host`` or the sort falls back to
        # alphabetical order and tests six Swiss hosts in a row.
        tier = 1 if source == "public-1-per-24" else 2 if source == "public-rest" else 0
        return (
            0,
            -float(row.get("score") or 0.0),
            tier,
            row.get("_rr", (0, 0)),
            0 if row.get("in_api") else 1,
            load,
            host,
        )

    def _candidate_key(row: dict) -> tuple:
        # No country quota: all countries are eligible. Country is metadata;
        # the public tier is only interleaved to sample distinct subnets
        # instead of exhausting one country's range first.
        return _base_key(row)

    rows.sort(key=_candidate_key)
    top = ", ".join(
        f"{row['host']}({row['source']})" for row in rows[:3]
    )
    log(
        f"candidate quality: {len(quality)} measured, {len(has_200)} known-200; "
        f"top={top or '-'}"
    )
    return rows


def build_auto_candidates(
    valid_by_transport: dict[str, dict[str, str]],
    station: int | None = None,
) -> list[dict]:
    """Merge per-transport candidate lists for ``--transport auto``.

    A hostname can support WireGuard, OpenVPN UDP and/or OpenVPN TCP. The
    returned row keeps the ordered ``transports`` list; the chain tries them
    in that order for the same server before moving to the next hostname.
    """

    merged: dict[str, dict] = {}
    for transport in _transport_order():
        valid = valid_by_transport.get(transport) or None
        for row in build_candidates(transport, valid, station):
            host = str(row.get("host") or "")
            if not host:
                continue
            existing = merged.get(host)
            if existing is None:
                row["transports"] = [transport]
                merged[host] = row
                continue
            existing.setdefault("transports", []).append(transport)
            # Keep the best score/provenance for the merged row, but preserve
            # the union of supported transports.
            if float(row.get("score") or 0.0) > float(existing.get("score") or 0.0):
                transports = existing["transports"]
                row["transports"] = transports
                merged[host] = row
    rows = list(merged.values())
    for row in rows:
        row["transports"] = list(dict.fromkeys(row.get("transports") or []))
    rows.sort(
        key=lambda row: (
            -float(row.get("score") or 0.0),
            0 if row.get("in_api") else 1,
            int(row.get("load") or 100),
            str(row.get("host") or ""),
        )
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
    """One chain per (station, transport); the guard lives in free.chain_repair."""

    return _acquire_lock(
        _lock_path(station, transport),
        pid_alive_fn=_pid_alive,
    )


def release_lock(station: int, transport: str = "wireguard") -> None:
    _release_lock(_lock_path(station, transport))


def load_state(path: Path) -> dict:
    from free.chain_state import load_state as _load_state

    return _load_state(path)


def save_state(path: Path, state: dict) -> None:
    from free.chain_state import save_state as _save_state

    _save_state(path, state)


def _persist_env_pin(station: int, host: str, country: str) -> None:
    """Write a probe-verified winner into ``.env`` immediately.

    The manager reads ``SERVER_HOSTNAMES_STATIONn`` on every compose. Waiting
    for the supervisor to persist it made the manager reconnect to a random
    server and lose the chain's bind, so the repair never converged.
    """
    if not host:
        return
    path = ROOT / ".env"
    values = {
        f"SERVER_HOSTNAMES_STATION{int(station)}": str(host).strip(),
        f"SERVER_COUNTRIES_STATION{int(station)}": str(country or "").strip(),
    }
    try:
        from ops.env_store import update_env

        update_env(path, values)
    except Exception:
        return
    log(f"pin persisted to .env: station {station} {host} ({country})")


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


def _fleet_usable() -> int:
    """Number of stations that can currently serve a model request."""

    try:
        data = requests.get(
            "http://127.0.0.1:4000/api/vpn-status", timeout=10
        ).json()
    except Exception:
        return 0
    count = 0
    for item in data.get("stations", []):
        if (
            item.get("model_usable")
            or item.get("client_usable")
            or item.get("station_usable")
        ):
            count += 1
    return count


def _serving_fleet(exclude_station: int | None = None) -> list[dict]:
    """Serving stations (connected/degraded) as ``{station, ip, country}``.

    Used by the diversity guard: the fleet must not put more than
    ``max_per_country`` stations in one country or ``max_per_24`` in one
    /24, otherwise a single upstream quota bucket exhausts the whole fleet
    at once (the 2026-11-03 0/6 incident).
    """
    try:
        data = requests.get("http://127.0.0.1:4000/api/vpn-status", timeout=10).json()
    except Exception:
        return []
    out: list[dict] = []
    for item in data.get("stations", []):
        try:
            sid = int(item.get("station") or 0)
        except (TypeError, ValueError):
            continue
        if exclude_station is not None and sid == int(exclude_station):
            continue
        vpn = item.get("vpn") if isinstance(item.get("vpn"), dict) else {}
        if str(vpn.get("status") or "") not in ("connected", "degraded"):
            continue
        ip = str(item.get("current_ip") or vpn.get("ip") or "").strip()
        country = str(
            item.get("current_country")
            or vpn.get("current_country")
            or vpn.get("country")
            or ""
        ).strip()
        out.append({"station": sid, "ip": ip, "country": country})
    return out


def _station_snapshot(api_base: str, station: int) -> dict:
    """Current host/egress of a station, captured before a scan."""

    try:
        data = requests.get(api_base + "/api/vpn-status", timeout=20).json()
    except Exception:
        return {}
    for item in data.get("stations", []):
        try:
            sid = int(item.get("station") or 0)
        except (TypeError, ValueError):
            continue
        if sid != int(station):
            continue
        vpn = item.get("vpn") if isinstance(item.get("vpn"), dict) else {}
        return {
            "host": str(vpn.get("current_host") or "").strip(),
            "ip": str(item.get("current_ip") or "").strip(),
            "country": str(
                item.get("current_country")
                or vpn.get("current_country")
                or vpn.get("country")
                or ""
            ).strip(),
            "status": str(vpn.get("status") or "").strip(),
        }
    return {}


def _should_restore_previous(
    snapshot: dict, *, exhausted: set[str], current_host: str = ""
) -> bool:
    """Restore the pre-scan host only when its egress is still alive."""

    host = str((snapshot or {}).get("host") or "").strip()
    ip = str((snapshot or {}).get("ip") or "").strip()
    if not host or not ip:
        return False
    if str((snapshot or {}).get("status") or "") not in ("connected", "degraded"):
        return False
    if ip in exhausted:
        return False
    if current_host and current_host == host:
        return False
    return True


def _exhausted_ips_from_buckets(buckets, now: float | None = None) -> set[str]:
    ts = float(now if now is not None else time.time())
    out: set[str] = set()
    for bucket in buckets or []:
        if not isinstance(bucket, dict):
            continue
        if bucket.get("exhausted") and float(bucket.get("reset_at") or 0) > ts:
            ip = str(bucket.get("ip") or "").strip()
            if ip:
                out.add(ip)
    return out


def _ledger_exhausted_ips(api_base: str) -> set[str]:
    buckets: list = []
    try:
        data = requests.get(api_base + "/api/pool-status", timeout=30).json()
        buckets = list((data.get("quota_ledger") or {}).get("buckets") or [])
    except Exception:
        buckets = []
    if not buckets:
        # Local fallback: registry.db is the quota source now. Keep the old
        # JSON path for a one-time migration window.
        try:
            conn = sqlite3.connect(
                str(ROOT / "logs" / "registry.db"), timeout=10.0
            )
            try:
                rows = conn.execute(
                    "SELECT ip FROM quota_buckets "
                    "WHERE last_status=429 AND reset_at > ?",
                    (time.time(),),
                ).fetchall()
            finally:
                conn.close()
            return {str(row[0]) for row in rows if row[0]}
        except Exception:
            pass
        try:
            raw = json.loads(
                (ROOT / "logs" / "quota_ledger.json").read_text(encoding="utf-8")
            )
            if isinstance(raw, dict):
                buckets = list(raw.get("buckets") or raw.get("items") or [])
        except Exception:
            buckets = []
    return _exhausted_ips_from_buckets(buckets)


def mark(
    api_base: str,
    ip: str,
    status: int,
    retry_after: float,
    station: int,
    model: str,
    hostname: str = "",
    transport: str = "",
) -> None:
    body: dict[str, Any] = {
        "ip": ip,
        "model": model,
        "status": status,
        "station": station,
    }
    if hostname:
        body["hostname"] = hostname
    if transport:
        body["transport"] = transport
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
    transport: str = "wireguard",
) -> None:
    """Persist the tested winner in the shared IP registry (test-then-keep).

    The catalog claims DB says who tested what; this makes the assignment
    authoritative for the allocator/dashboard: one station, one egress.
    """

    if not ip:
        return
    path = ROOT / "logs" / "registry.db"
    now = time.time()
    try:
        with sqlite3.connect(path, timeout=10.0) as conn:
            owner = conn.execute(
                "SELECT station FROM leases WHERE ip=? AND status='active' LIMIT 1",
                (str(ip),),
            ).fetchone()
            if owner is not None and int(owner[0]) != int(station):
                log(
                    f"registry: station {station} cannot claim {ip} "
                    f"(active lease on station {owner[0]})"
                )
                return
            conn.execute(
                "INSERT OR IGNORE INTO catalog_sources("
                "source_id, kind, fetched_at, stale, server_count, error"
                ") VALUES('fresh-chain','chain',?,0,0,'')",
                (now,),
            )
            conn.execute(
                "INSERT INTO servers(hostname, country, source_id, "
                "first_seen_at, last_seen_at) VALUES(?,?,'fresh-chain',?,?) "
                "ON CONFLICT(hostname) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (str(host), str(country or "unknown"), now, now),
            )
            conn.execute(
                "INSERT INTO egress_ips(ip, country, first_seen_at, "
                "last_seen_at, last_ok_at, successes, latency_ms, state) "
                "VALUES(?,?,?,?,?,1,?,'assigned') "
                "ON CONFLICT(ip) DO UPDATE SET last_seen_at=excluded.last_seen_at, "
                "last_ok_at=excluded.last_ok_at, successes=egress_ips.successes+1, "
                "latency_ms=COALESCE(excluded.latency_ms, egress_ips.latency_ms), "
                "reset_at=NULL, state='assigned'",
                (str(ip), str(country or ""), now, now, now, latency_ms),
            )
            conn.execute(
                "INSERT INTO ip_server_map(ip, hostname, transport, "
                "first_seen_at, last_seen_at, successes) "
                "VALUES(?,?,?,?,?,1) "
                "ON CONFLICT(ip, hostname, transport) DO UPDATE SET "
                "last_seen_at=excluded.last_seen_at, "
                "successes=ip_server_map.successes+1",
                (str(ip), str(host), str(transport), now, now),
            )
            conn.execute(
                "INSERT INTO observations(station, hostname, transport, ip, "
                "model, status, valid, latency_ms, tested_at) "
                "VALUES(?,?,?,?,'',200,1,?,?)",
                (int(station), str(host), str(transport), str(ip), latency_ms, now),
            )
            conn.execute(
                "UPDATE leases SET status='released', released_at=?, "
                "reason='replaced' WHERE station=? AND status='active' AND ip!=?",
                (now, int(station), str(ip)),
            )
            if owner is not None:
                conn.execute(
                    "UPDATE leases SET hostname=?, renewed_at=?, expires_at=? "
                    "WHERE ip=? AND station=? AND status='active'",
                    (str(host), now, now + 6 * 3600.0, str(ip), int(station)),
                )
            else:
                conn.execute(
                    "INSERT INTO leases(lease_id, station, ip, hostname, "
                    "transport, model, started_at, renewed_at, expires_at, status) "
                    "VALUES(?,?,?,?,?,'',?,?,?,'active')",
                    (
                        f"chain-{int(station)}-{str(ip)}-{int(now)}",
                        int(station),
                        str(ip),
                        str(host),
                        str(transport),
                        now,
                        now,
                        now + 6 * 3600.0,
                    ),
                )
            conn.commit()
        log(f"registry: station {station} keeps {ip} ({host})")
    except Exception as exc:  # noqa: BLE001 - the chain must continue
        log(f"registry assignment failed: {type(exc).__name__}: {exc}")


def _record_registry_429(
    ip: str, host: str, country: str, retry_after: float
) -> None:
    """Keep ``ip_state``'s 429/reset fresh so the chain stops retrying dead IPs.

    [2026-11-03] The 700+ known hostname IPs were all treated as candidates
    even after a 429 with a 3-hour retry: each retry wasted a bind (~80 s) and
    an AUTH token. Writing the reset back lets ``_registry_hosts`` skip them
    until the provider reopens the bucket.
    """
    ip = str(ip or "").strip()
    if not ip:
        return
    path = ROOT / "logs" / "registry.db"
    now = time.time()
    reset_at = now + max(60.0, float(retry_after or 0.0))
    try:
        with sqlite3.connect(path, timeout=10.0) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO catalog_sources("
                "source_id, kind, fetched_at, stale, server_count, error"
                ") VALUES('fresh-chain','chain',?,0,0,'')",
                (now,),
            )
            conn.execute(
                "INSERT INTO servers(hostname, country, source_id, "
                "first_seen_at, last_seen_at) VALUES(?,?,'fresh-chain',?,?) "
                "ON CONFLICT(hostname) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (str(host or ip), str(country or "unknown"), now, now),
            )
            conn.execute(
                "INSERT INTO egress_ips(ip, country, first_seen_at, "
                "last_seen_at, last_429_at, reset_at, failures, state) "
                "VALUES(?,?,?,?,?,?,1,'exhausted') "
                "ON CONFLICT(ip) DO UPDATE SET last_seen_at=excluded.last_seen_at, "
                "last_429_at=excluded.last_429_at, reset_at=excluded.reset_at, "
                "failures=egress_ips.failures+1, state='exhausted'",
                (str(ip), str(country or ""), now, now, now, reset_at),
            )
            if host:
                conn.execute(
                    "INSERT INTO ip_server_map(ip, hostname, transport, "
                    "first_seen_at, last_seen_at, failures) "
                    "VALUES(?,?,'wireguard',?,?,1) "
                    "ON CONFLICT(ip, hostname, transport) DO UPDATE SET "
                    "last_seen_at=excluded.last_seen_at, "
                    "failures=ip_server_map.failures+1",
                    (str(ip), str(host), now, now),
                )
            conn.execute(
                "UPDATE leases SET status='released', released_at=?, "
                "reason='quota_429' WHERE ip=? AND status='active'",
                (now, str(ip)),
            )
            conn.execute(
                "INSERT INTO observations(station, hostname, transport, ip, "
                "model, status, valid, retry_after_s, reset_at, tested_at) "
                "VALUES(0,?,'wireguard',?,'',429,1,?,?,?)",
                (str(host or ""), str(ip), float(retry_after or 0.0), reset_at, now),
            )
            conn.commit()
    except Exception:
        pass


def _should_mark_ledger_429(result) -> bool:
    """Only a real quota/rate-limit 429 should touch the IP ledger.

    A transient upstream overload is not a property of the egress IP; marking
    it until midnight would throw away a healthy address.
    """

    scope = str(getattr(result, "quota_scope", "") or "").strip().lower()
    return scope != "transient"


def _probe_attempt_status(result) -> str:
    """Map a probe result to the qualification attempt taxonomy."""
    try:
        code = int(getattr(result, "status", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    if code == 200:
        return "200" if bool(getattr(result, "valid", True)) else "dead"
    if code == 429:
        return "429"
    if code in (401, 407):
        return "auth"
    if code == 0:
        text = str(getattr(result, "error", "") or "").lower()
        return "timeout" if "timeout" in text or "timed out" in text else "no_egress"
    return "dead" if 500 <= code < 600 else "no_egress"


async def _probe_with_fallbacks(probe, station: int, primary: str, fallbacks: list[str]):
    """Try the primary free model, then the other buckets before condemning IP.

    A 429 is keyed by (model, IP bucket): an egress rejected on deepseek can
    still answer 200 on glm/mimo. One 200 is enough to keep the IP.
    """

    result = await probe.probe(station, primary)
    if result.status == 200 and getattr(result, "valid", True):
        return result, primary
    for model in fallbacks:
        if not model or model == primary:
            continue
        alt = await probe.probe(station, model)
        if alt.status == 200 and getattr(alt, "valid", True):
            return alt, model
    return result, primary


async def _restore_previous_host(
    args, binder, snapshot: dict
) -> bool:
    """Roll back to the pre-scan host when its egress is still alive.

    A failed scan must not leave a previously working station on a no-egress
    host. The restore is skipped when the old IP is burned or already bound.
    """

    current = _station_snapshot(args.api_base, args.station)
    exhausted = _ledger_exhausted_ips(args.api_base)
    if not _should_restore_previous(
        snapshot, exhausted=exhausted, current_host=current.get("host", "")
    ):
        return False
    host = str(snapshot.get("host") or "").strip()
    country = str(snapshot.get("country") or "").strip()
    log(f"  restoring previous host {host} ({snapshot.get('ip')})")
    transport = str(args.transport)
    if transport == "auto":
        transport = _transport_order()[0]
    vpn_type, openvpn_protocol = _transport_parts(transport)
    bind = await binder.assign(
        args.station,
        host,
        country=country,
        vpn_type=vpn_type,
        openvpn_protocol=openvpn_protocol,
    )
    if bind.ok and bind.egress_ip:
        log(f"  previous host restored: {host} egress={bind.egress_ip}")
        return True
    log(f"  previous host restore failed: {bind.error}")
    return False


async def _run_claims(
    args,
    candidates,
    binder,
    probe,
    state_path,
    state,
    exhausted: set[str],
    snapshot: dict | None = None,
) -> int:
    """Parallel-safe chain: one shared catalog, exclusive hostname claims."""
    from free.catalog_claims import CatalogClaims
    from free.ip_registry.dns import DnsFallback

    claims = CatalogClaims(
        args.claims_db,
        lease_s=600.0,
        rebind_limit_per_min=max(1, int(args.rebind_limit)),
        host_429_cooldown_s=float(
            yaml_get("ip_registry", "allocator.host_429_cooldown_s", 600) or 600
        ),
        no_egress_backoff_s=float(
            yaml_get("ip_registry", "allocator.no_egress_backoff_s", 300) or 300
        ),
        no_egress_backoff_max_s=float(
            yaml_get("ip_registry", "allocator.no_egress_backoff_max_s", 3600)
            or 3600
        ),
    )
    audit = None
    run_id = ""
    run_closed = False
    if bool(yaml_get("ip_registry", "allocator.scan_audit_enabled", True)):
        try:
            from free.scan_audit import ScanAudit

            audit = ScanAudit()
            run_id = audit.start_run(
                args.station,
                transport=args.transport,
                reason="repair",
                candidates=len(candidates),
            )
        except Exception:
            audit = None

    def _close_run(outcome: str, host: str = "", ip: str = "") -> None:
        nonlocal run_closed
        if audit is not None and not run_closed:
            run_closed = True
            try:
                audit.finish_run(run_id, outcome, winner_host=host, winner_ip=ip)
            except Exception:
                pass

    try:
        # [2026-11-03 stale-winner] A new run invalidates the previous winner:
        # keeping it made the supervisor wait on a host this run had already
        # abandoned, then relaunch a chain every grace period (rebind loop).
        if state.get("winner"):
            state["winner"] = None
            save_state(state_path, state)
        # [fleet-guard 2026-11-03] Never prune the shared catalog from a
        # candidate list that was filtered by ``_active_hosts``. A hostname
        # currently owned by another station was removed, then that station
        # could never reclaim it when it broke (al37/87.120.102.22 was lost
        # this way while it still had quota). Stale rows are harmless: the
        # claim status/429 reset already skips them.
        seeded = claims.seed(candidates, prune=False)
        log(
            f"claims mode: catalog={seeded} db={args.claims_db} "
            f"rebind_limit={args.rebind_limit}/min station={args.station} "
            f"first={[str(c.get('host')) for c in candidates[:3]]}"
        )
        by_host = {str(row.get("host") or ""): row for row in candidates}
        tested = 0
        active_hosts = _active_hosts(args.station)
        stop_after_429 = bool(
            yaml_get("ip_registry", "allocator.stop_after_429", True)
        )
        fast_discovery = bool(
            yaml_get("ip_registry", "allocator.fast_discovery", False)
        )
        if str(args.transport) == "auto":
            # FR-072: in auto mode the tri-stack is mandatory.  A single
            # transport shortcut would leave WireGuard (or OpenVPN) untested
            # and turn an infrastructure failure into a false ``dead``.
            fast_discovery = False
            log("fast discovery off: auto mode walks every transport")
        if fast_discovery and _fleet_usable() <= 0:
            # No station can answer a model request: one transport per attempt
            # would leave WireGuard untested forever when the containers are
            # currently on OpenVPN. Walk the full tri-stack until a station is
            # usable again; fast discovery resumes on the first repair.
            fast_discovery = False
            log("fast discovery off: 0 usable station - full tri-stack")
        discovery_transport = str(
            yaml_get(
                "ip_registry",
                "allocator.fast_discovery_transport",
                "openvpn-udp",
            )
            or "openvpn-udp"
        ).strip()
        if discovery_transport not in (
            "wireguard",
            "openvpn-udp",
            "openvpn-tcp",
        ):
            discovery_transport = "openvpn-udp"
        dns_fallback = DnsFallback(
            order=tuple(
                yaml_get(
                    "ip_registry", "dns.fallback_order", ["dot", "plain"]
                )
                or ["dot", "plain"]
            ),
            failures_before_fallback=int(
                yaml_get(
                    "ip_registry", "dns.failures_before_fallback", 2
                )
                or 2
            ),
        )
        while True:
            if args.max and tested >= args.max:
                log(f"claims mode: max={args.max} reached")
                _close_run("max_candidates")
                await _restore_previous_host(args, binder, snapshot or {})
                return 2
            claim = claims.claim_next(args.station, forbidden_ips=exhausted)
            if claim is None:
                log("catalog exhausted or every candidate is claimed/reset")
                _close_run("catalog_exhausted")
                await _restore_previous_host(args, binder, snapshot or {})
                return 2
            host = str(claim["host"])
            country = str(claim["country"])
            # The shared catalog keeps rows from older runs (no prune: an
            # active host must stay reclaimable). If this run's candidate
            # build filtered the host — exhausted /24, no longer in the live
            # manifest, or already owned — skip it without spending a bind.
            if host not in by_host:
                claims.record(
                    host, args.station, "no_egress", error="filtered-candidate"
                )
                log("  filtered candidate (exhausted /24 or not live) - skip")
                continue
            tested += 1
            attempt_id = (
                audit.begin_attempt(
                    run_id,
                    args.station,
                    host,
                    country=country,
                    transport=args.transport,
                )
                if audit is not None
                else ""
            )
            if tested % 20 == 0:
                exhausted = _ledger_exhausted_ips(args.api_base)
                active_hosts = _active_hosts(args.station)
            if host in active_hosts:
                log("  hostname already served by another station — skip")
                if audit is not None:
                    audit.finish_attempt(attempt_id, "active_host")
                claims.record(host, args.station, "no_egress", error="active-host")
                row = {
                    "host": host,
                    "country": country,
                    "status": "active-host",
                }
                state["results"] = (state.get("results") or [])[-499:] + [row]
                save_state(state_path, state)
                await asyncio.sleep(args.delay)
                continue

            row = by_host.get(host, {})
            burned = _exhausted_egress(row, exhausted)
            if burned:
                claims.record(
                    host,
                    args.station,
                    "no_egress",
                    ip=burned,
                    error="exhausted-egress",
                )
                log(f"  last egress {burned} exhausted - skip without bind")
                await asyncio.sleep(args.delay)
                continue
            transports = list(row.get("transports") or [])
            if not transports:
                transports = [args.transport]
            if str(args.transport) != "auto":
                # A qualification candidate may only support WireGuard or one
                # OpenVPN protocol.  Never spend a bind on a transport the
                # host does not advertise: the claim is released to pending
                # and the next candidate is selected.
                if transports and args.transport not in transports:
                    claims.record(
                        host,
                        args.station,
                        "no_egress",
                        error="transport-unsupported",
                    )
                    log(
                        f"  host does not support {args.transport} "
                        f"(has {','.join(str(item) for item in transports)}) - skip"
                    )
                    await asyncio.sleep(args.delay)
                    continue
                transports = [args.transport]
            # [2026-11-05] Try the transport already configured in the
            # container first: the hybrid binder can switch its hostname via
            # the control API in seconds instead of a ~2 min compose
            # recreate. Only do that in fast-discovery mode; when the fleet is
            # down the full configured order (WireGuard first) is the point.
            try:
                preferred = str(binder.current_transport(args.station) or "")
            except Exception:
                preferred = ""
            if fast_discovery and preferred and preferred in transports:
                transports = [preferred] + [
                    item for item in transports if item != preferred
                ]
            if fast_discovery and str(args.transport) == "auto":
                # Discovery mode: one transport per station and move to the
                # next hostname on any failure. Prefer the transport already
                # configured in the container (the control API then switches
                # hostnames in seconds); when the container is down, follow
                # the station's own stack so a WireGuard station does not
                # silently test only OpenVPN egresses. The full tri-stack
                # remains available with fast_discovery=false.
                #
                # An explicit ``--transport`` is an operator constraint: it
                # must win over discovery, otherwise a TCP repair silently
                # binds WireGuard and blocks every other station through the
                # shared key lease.
                discovery_pref = preferred if preferred in transports else ""
                if not discovery_pref:
                    configured = _station_transport(args.station)
                    discovery_pref = (
                        configured if configured in transports else ""
                    )
                transports = [discovery_pref or discovery_transport]
            log(
                f"claim {host} ({country}) transports="
                + ",".join(str(item) for item in transports)
            )

            winner = None
            last_error = "no-egress"
            quota_429 = False
            retry_after = 0.0
            ip = ""
            result = None
            model_used = ""
            for transport in transports:
                while not claims.rebind_allowed():
                    await asyncio.sleep(2)
                claims.renew(host, args.station)
                _clean_station_container(args.station)
                vpn_type, openvpn_protocol = _transport_parts(str(transport))
                dns_mode = dns_fallback.mode(args.station)
                bind = await binder.assign(
                    args.station,
                    host,
                    country=country,
                    vpn_type=vpn_type,
                    openvpn_protocol=openvpn_protocol,
                    dns_mode=(dns_mode if dns_mode != "dot" else None),
                )
                if not bind.ok:
                    dns_fallback.note_failure(
                        args.station, bind.error or ""
                    )
                    last_error = f"{transport}:{bind.error or 'bind-error'}"
                    log(f"  bind rejected ({transport}): {bind.error}")
                    _record_chain_attempt(
                        station=args.station,
                        hostname=host,
                        transport=str(transport),
                        status="bind_error",
                        error=bind.error or "bind-error",
                        db_path=args.claims_db,
                    )
                    continue
                dns_fallback.reset(args.station)
                # The bind may have consumed most of the initial lease; renew
                # before the (potentially slow) official probe.
                claims.renew(host, args.station)
                ip = str(bind.egress_ip or "")
                log(f"  egress {ip} ({transport})")
                if ip and ip in _active_egress_ips(args.api_base, args.station):
                    last_error = f"{transport}:duplicate"
                    log("  egress already used by a serving station — try next transport")
                    _record_chain_attempt(
                        station=args.station,
                        hostname=host,
                        transport=str(transport),
                        status="duplicate",
                        egress_ip=ip,
                        error="active-egress",
                        db_path=args.claims_db,
                    )
                    continue
                # [2026-11-05] No country or /24 quota: the operator's only
                # hard constraint is that two stations never share the same IP.
                if ip and ip in exhausted:
                    last_error = f"{transport}:ledger-429"
                    log("  egress already in the 429 ledger — try next transport")
                    _record_chain_attempt(
                        station=args.station,
                        hostname=host,
                        transport=str(transport),
                        status="429",
                        egress_ip=ip,
                        error="ledger-429",
                        db_path=args.claims_db,
                    )
                    continue
                result, model_used = await _probe_with_fallbacks(
                    probe, args.station, args.model, args.probe_models
                )
                log(
                    f"  free status={result.status} ra={result.retry_after_s} "
                    f"model={model_used} transport={transport} "
                    f"valid={getattr(result, 'valid', True)} "
                    f"validation={getattr(result, 'validation', None)} "
                    f"scope={getattr(result, 'quota_scope', '') or '-'} "
                    f"fp={str(getattr(result, 'request_fingerprint', '') or '')[:12]}"
                )
                _record_chain_attempt(
                    station=args.station,
                    hostname=host,
                    transport=str(transport),
                    status=_probe_attempt_status(result),
                    egress_ip=ip,
                    error=str(getattr(result, "error", "") or "")[:300],
                    latency_ms=float(getattr(result, "latency_ms", 0.0) or 0.0),
                    retry_after_s=float(getattr(result, "retry_after_s", 0.0) or 0.0),
                    round_n=0,
                    db_path=args.claims_db,
                )
                if result.status == 200 and bool(getattr(result, "valid", True)):
                    # [P0 2026-11-10] Un 200 isolé ne prouve pas un bucket
                    # utilisable : les IP partagées n'ont parfois qu'une
                    # requête de marge et 429 à la suivante. Confirmer avant
                    # de pinner évite un cycle de rotation complet (~2 min)
                    # pour une IP déjà morte.
                    confirm, confirm_model = await _probe_with_fallbacks(
                        probe,
                        args.station,
                        model_used or args.model,
                        args.probe_models,
                    )
                    log(
                        f"  confirmation status={confirm.status} "
                        f"model={confirm_model} transport={transport}"
                    )
                    _record_chain_attempt(
                        station=args.station,
                        hostname=host,
                        transport=str(transport),
                        status=_probe_attempt_status(confirm),
                        egress_ip=ip,
                        error=str(getattr(confirm, "error", "") or "")[:300],
                        latency_ms=float(
                            getattr(confirm, "latency_ms", 0.0) or 0.0
                        ),
                        retry_after_s=float(
                            getattr(confirm, "retry_after_s", 0.0) or 0.0
                        ),
                        round_n=1,
                        db_path=args.claims_db,
                    )
                    if confirm.status == 200 and bool(
                        getattr(confirm, "valid", True)
                    ):
                        winner = (str(transport), ip, confirm, confirm_model)
                        break
                    if confirm.status == 429 and _should_mark_ledger_429(confirm):
                        retry_after = max(
                            retry_after, float(confirm.retry_after_s or 0.0)
                        )
                        exhausted.add(ip)
                        mark(
                            args.api_base,
                            ip,
                            429,
                            confirm.retry_after_s,
                            args.station,
                            confirm_model,
                            transport=str(transport),
                        )
                        _record_registry_429(
                            ip, host, country, confirm.retry_after_s
                        )
                        last_error = f"{transport}:429-confirm"
                        log("  confirmation 429 - IP exhausted, next host")
                        continue
                    last_error = f"{transport}:confirm-{confirm.status}"
                    log("  confirmation failed - next host")
                    continue
                if result.status == 200:
                    last_error = (
                        f"{transport}:invalid-200:"
                        f"{getattr(result, 'validation', 'unknown')}"
                    )
                    continue
                if result.status == 429:
                    retry_after = max(retry_after, float(result.retry_after_s or 0.0))
                    if _should_mark_ledger_429(result):
                        quota_429 = True
                        exhausted.add(ip)
                        mark(
                            args.api_base,
                            ip,
                            429,
                            result.retry_after_s,
                            args.station,
                            model_used,
                            transport=str(transport),
                        )
                        _record_registry_429(ip, host, country, result.retry_after_s)
                        if stop_after_429:
                            # A real quota 429 is a property of the egress IP,
                            # not of the transport. Trying the other two
                            # transports on the same hostname usually lands
                            # in the same burned NAT range and costs two more
                            # AUTH slots; a new hostname is the higher-value
                            # next probe. Bind/no-egress still uses the full
                            # WireGuard -> OV UDP -> OV TCP order.
                            log("  real 429 - next host (tri-stack kept for bind/no-egress)")
                            last_error = f"{transport}:429"
                            break
                    else:
                        log(f"  transient 429 ({transport}) — IP left eligible")
                    last_error = f"{transport}:429"
                    continue
                last_error = f"{transport}:{result.status}"

            if audit is not None:
                attempt_status = (
                    str(winner[2].status)
                    if winner is not None
                    else ("429" if quota_429 else "no_egress")
                )
                audit.finish_attempt(
                    attempt_id,
                    attempt_status,
                    egress_ip=ip,
                    valid=getattr(result, "valid", None) if result else None,
                    validation=getattr(result, "validation", None) if result else None,
                    fingerprint=(
                        getattr(result, "request_fingerprint", "") if result else ""
                    ),
                    error=last_error,
                )

            if winner is None:
                # No country gate: a 429 or bind failure is a property of the
                # tested IP/transport, not of the country. The shared claim
                # backoff handles the host; another host is tried next.
                log(f"  {country}: no country gate; next host after {last_error}")

            if winner is not None:
                transport, ip, result, model_used = winner
                claims.record(host, args.station, "ok", ip=ip)
                mark(
                    args.api_base,
                    ip,
                    200,
                    0.0,
                    args.station,
                    model_used,
                    hostname=host,
                    transport=transport,
                )
                _record_registry_winner(
                    args.station,
                    host,
                    country,
                    ip,
                    getattr(result, "latency_ms", None),
                    transport,
                )
                _record_verified_spare(
                    args.station, host, country, ip, model_used, transport
                )
                state["winner"] = {
                    "host": host,
                    "country": country,
                    "ip": ip,
                    "transport": transport,
                    "model": model_used,
                    "fingerprint": getattr(result, "request_fingerprint", ""),
                    "winner_at": time.time(),
                }
                state["results"] = (state.get("results") or [])[-499:] + [
                    {
                        "host": host,
                        "country": country,
                        "ip": ip,
                        "transport": transport,
                        "status": 200,
                        "tested_at": time.time(),
                        "model": model_used,
                        "valid": True,
                        "validation": getattr(result, "validation", None),
                        "fingerprint": getattr(result, "request_fingerprint", ""),
                    }
                ]
                save_state(state_path, state)
                _persist_env_pin(args.station, host, country)
                log(
                    f"WINNER station={args.station} host={host} egress={ip} "
                    f"transport={transport} model={model_used}"
                )
                _close_run("recovered", host, ip)
                return 0

            claims.record(
                host,
                args.station,
                "429" if quota_429 else "no_egress",
                ip=ip,
                retry_after=retry_after,
                error=last_error,
            )
            state["results"] = (state.get("results") or [])[-499:] + [
                {
                    "host": host,
                    "country": country,
                    "ip": ip,
                    "status": "429" if quota_429 else "no-egress",
                    "transports": transports,
                    "error": last_error,
                    "tested_at": time.time(),
                }
            ]
            save_state(state_path, state)
            await asyncio.sleep(args.delay)
    finally:
        _close_run("interrupted")
        if audit is not None:
            audit.close()
        claims.close()


async def run(args) -> int:
    from free.ip_registry import (
        ComposeHostnameBinder,
        GluetunControlBinder,
        HybridBinder,
        StationEgress,
        StationProbe,
        host_public_ip,
    )

    if not acquire_lock(args.station, args.transport):
        log(f"chain already running for station {args.station} transport={args.transport} — exit")
        return 0
    atexit.register(release_lock, args.station, args.transport)

    state_path = _state_path(args.station, args.transport)
    state = load_state(state_path)
    candidates_cache = CANDIDATES_CACHE.with_name(
        f"fresh_chain_candidates_{args.transport}.json"
    )

    # Gluetun validates every hostname against its own manifest.  Select from
    # the newest manifest and install it in this station's volume *before* the
    # first recreate; otherwise old embedded hosts fail with "hostname not
    # valid" and the chain burns its whole budget without ever testing a tunnel.
    manifest = load_runtime_manifest(refresh=True)
    auto = str(args.transport).strip().lower() == "auto"
    transport_order = _transport_order()
    valid_by_transport: dict[str, dict[str, str]] = {}
    if auto:
        valid_by_transport = {
            transport: manifest_hosts(manifest, transport)
            for transport in transport_order
        }
        total_hosts = sum(len(hosts) for hosts in valid_by_transport.values())
        log(
            f"runtime manifest: {total_hosts} hosts over {len(transport_order)} "
            f"transports (ts={int(float((manifest or {}).get('timestamp') or 0))})"
        )
        if total_hosts:
            if sync_manifest_to_station(args.station, manifest):
                log(f"manifest synced to station {args.station}")
            else:
                log(f"manifest sync station {args.station} unavailable — using runtime set")
            station_manifest = read_station_manifest(args.station)
            if station_manifest is not None:
                station_by_transport = {
                    transport: manifest_hosts(station_manifest, transport)
                    for transport in transport_order
                }
                if any(station_by_transport.values()):
                    manifest = station_manifest
                    valid_by_transport = station_by_transport
                    log(
                        "station manifest: "
                        + ", ".join(
                            f"{transport}={len(hosts)}"
                            for transport, hosts in station_by_transport.items()
                        )
                        + f" (volume, ts={int(float((manifest or {}).get('timestamp') or 0))})"
                    )
                else:
                    log("station manifest has no host for these transports — using runtime set")
            else:
                log("station manifest unreadable — using runtime set")
        else:
            log("runtime manifest unavailable — unfiltered candidates (legacy mode)")
    else:
        valid_hosts = manifest_hosts(manifest, args.transport)
        valid_by_transport = {args.transport: valid_hosts}
        if valid_hosts:
            log(
                f"runtime manifest: {len(valid_hosts)} {args.transport} hosts "
                f"(ts={int(float((manifest or {}).get('timestamp') or 0))})"
            )
            if sync_manifest_to_station(args.station, manifest):
                log(f"manifest synced to station {args.station}")
            else:
                log(f"manifest sync station {args.station} unavailable — using runtime set")
            # [2026-11-05] Select from the manifest actually installed in the
            # station volume, not from the merged cache. This prevents selecting
            # a hostname the container will reject with "hostname not valid".
            station_manifest = read_station_manifest(args.station)
            if station_manifest is not None:
                station_hosts = manifest_hosts(station_manifest, args.transport)
                if station_hosts:
                    manifest = station_manifest
                    valid_hosts = station_hosts
                    valid_by_transport = {args.transport: valid_hosts}
                    log(
                        f"station manifest: {len(valid_hosts)} {args.transport} hosts "
                        f"(volume, ts={int(float((manifest or {}).get('timestamp') or 0))})"
                    )
                else:
                    log("station manifest has no host for this transport — using runtime set")
            else:
                log("station manifest unreadable — using runtime set")
        else:
            log("runtime manifest unavailable — unfiltered candidates (legacy mode)")

    cache_is_current = False
    if candidates_cache.exists():
        try:
            cache_is_current = (
                candidates_cache.stat().st_mtime
                >= GLUETUN_MANIFEST_CACHE.stat().st_mtime
            )
        except OSError:
            cache_is_current = False
    if args.candidates and Path(args.candidates).exists():
        candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    elif (
        candidates_cache.exists()
        and not args.refresh
        and cache_is_current
        and time.time() - candidates_cache.stat().st_mtime < 1800.0
    ):
        candidates = json.loads(candidates_cache.read_text(encoding="utf-8"))
    else:
        log("building candidate list")
        if auto:
            candidates = build_auto_candidates(valid_by_transport, args.station)
        else:
            candidates = build_candidates(
                args.transport, valid_by_transport.get(args.transport) or None, args.station
            )
        candidates_cache.write_text(json.dumps(candidates), encoding="utf-8")
    if not candidates:
        log("no candidates")
        return 1
    if str(args.transport) != "auto":
        # The candidate cache is shared between transports.  A list built for
        # ``auto``/WireGuard contains hosts that only advertise WireGuard;
        # spending a claim on them for a TCP chain just burns the rebind
        # budget.
        candidates = filter_candidates_for_transport(candidates, args.transport)
    log(f"candidates={len(candidates)} start={args.start or state.get('index', 0)}")

    if args.dry_run:
        for row in candidates[: args.max or 20]:
            print(row["host"], row["country"], row["source"])
        return 0

    # Multiple independent egress services + a longer tunnel settle: a single
    # 5 s ipify attempt after 8 s declared many healthy WireGuard servers
    # "no-egress" and burned the candidate catalog.
    host_ip = host_public_ip()
    if host_ip:
        log(f"host public ip {host_ip} is excluded from station egress")
    # A dead tunnel used to cost 3 schemes x 5 URLs x 8 s = 120 s BEFORE the
    # binder's own deadline was even checked. Two URLs and a 3 s timeout keep
    # one failed bind under ~20 s; the control-API/docker fallbacks below add
    # the rest only when they can actually answer.
    egress = StationEgress(
        timeout_s=3.0,
        urls=("https://api.ipify.org", "https://icanhazip.com"),
        forbidden_ips={host_ip} if host_ip else (),
    )
    probe = StationProbe(
        egress=egress,
        timeout_s=60.0,
        validate_sse=bool(
            yaml_get("ip_registry", "allocator.probe_validate_sse", True)
        ),
    )
    settle_s = float(yaml_get("ip_registry", "allocator.settle_s", 12) or 12)
    egress_wait_s = float(
        yaml_get("ip_registry", "allocator.egress_wait_s", 45) or 45
    )
    egress_poll_s = float(
        yaml_get("ip_registry", "allocator.egress_poll_s", 3) or 3
    )
    compose_binder = ComposeHostnameBinder(
        ROOT,
        override_path=f"logs/compose-hostname-{args.station}.yml",
        profile="multi-station",
        settle_s=settle_s,
        egress_wait_s=egress_wait_s,
        egress_poll_s=egress_poll_s,
        egress=egress,
        recreate_timeout_s=180.0,
    )
    container = "opencode-vpn" if int(args.station) == 1 else (
        f"opencode-vpn-{int(args.station)}"
    )
    binder = HybridBinder(
        control=GluetunControlBinder(
            container=container,
            egress=egress.control_only,
            settle_s=min(settle_s, 2.0),
            egress_wait_s=min(egress_wait_s, 12.0),
            egress_poll_s=1.0,
        ),
        compose=compose_binder,
    )
    start = int(args.start if args.start is not None else state.get("index", 0))
    vpn_type = "openvpn" if args.transport.startswith("openvpn") else "wireguard"
    openvpn_protocol = args.transport.split("-", 1)[1] if vpn_type == "openvpn" else None
    args.probe_models = [
        part.strip()
        for part in str(getattr(args, "models", "") or "").split(",")
        if part.strip() and part.strip() != args.model
    ]
    exhausted = _ledger_exhausted_ips(args.api_base)
    tested = 0
    snapshot = _station_snapshot(args.api_base, args.station)
    if args.claims or auto:
        return await _run_claims(
            args, candidates, binder, probe, state_path, state, exhausted, snapshot
        )
    # [2026-11-03 stale-winner] Invalidate the previous winner for this run.
    if state.get("winner"):
        state["winner"] = None
        save_state(state_path, state)
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
        # [2026-11-05] No /24 quota: exact-IP uniqueness is the only hard
        # constraint. The duplicate-active check above is the gate.
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
            f"model={model_used} valid={getattr(result, 'valid', True)} "
            f"validation={getattr(result, 'validation', None)} "
            f"scope={getattr(result, 'quota_scope', '') or '-'} "
            f"fp={str(getattr(result, 'request_fingerprint', '') or '')[:12]}"
        )
        state["results"].append(
            {
                "host": host,
                "country": country,
                "ip": ip,
                "status": result.status,
                "retry_after": result.retry_after_s,
                "model": model_used,
                "valid": bool(getattr(result, "valid", True)),
                "validation": getattr(result, "validation", None),
                "quota_scope": getattr(result, "quota_scope", ""),
                "fingerprint": getattr(result, "request_fingerprint", ""),
            }
        )
        state["index"] = index + 1
        save_state(state_path, state)
        if result.status == 200 and not bool(getattr(result, "valid", True)):
            log(
                f"  invalid 200 ({getattr(result, 'validation', 'unknown')}) "
                "— reject candidate"
            )
            continue
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
                args.station,
                host,
                country,
                ip,
                getattr(result, "latency_ms", None),
                args.transport,
            )
            _record_verified_spare(
                args.station, host, country, ip, model_used, args.transport
            )
            state["winner"] = {
                "host": host,
                "country": country,
                "ip": ip,
                "model": model_used,
                "winner_at": time.time(),
            }
            save_state(state_path, state)
            _persist_env_pin(args.station, host, country)
            log(f"WINNER station={args.station} host={host} egress={ip} model={model_used}")
            return 0
        if result.status == 429:
            if _should_mark_ledger_429(result):
                mark(
                    args.api_base,
                    ip,
                    429,
                    result.retry_after_s,
                    args.station,
                    args.model,
                )
                _record_registry_429(ip, host, country, result.retry_after_s)
                exhausted.add(ip)
            else:
                log("  transient 429 — IP left eligible")
        # no-egress/probe errors: move on
    log("exhausted without a 200")
    await _restore_previous_host(args, binder, snapshot)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", type=int, default=5)
    parser.add_argument(
        "--transport",
        default="wireguard",
        choices=("auto", "wireguard", "openvpn-tcp", "openvpn-udp"),
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
    parser.add_argument("--claims-db", default=str(ROOT / "logs" / "registry.db"))
    parser.add_argument("--rebind-limit", type=int, default=8)
    parser.add_argument("--sweep", action="store_true", help="qualification sweep mode (feature 014)")
    parser.add_argument("--epoch", default="", help="sweep epoch id")
    parser.add_argument("--shadow", action="store_true", help="sweep without binding a station")
    args = parser.parse_args()
    if args.sweep:
        return _run_qualification_sweep(args)
    return asyncio.run(run(args))


def _run_qualification_sweep(args) -> int:
    """Thin bridge from ``fresh_chain`` to the qualification sweep.

    Shadow mode only ingests/scores.  Live mode uses the same Gluetun/Compose
    adapter as the rest of the chain and rebinds the requested station.
    """

    import json

    import yaml

    from free.qualification import build_qualification, build_registry_station_adapter

    config_path = ROOT / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log(f"sweep config read failed: {type(exc).__name__}: {exc}")
        return 2
    adapter = None
    if not args.shadow:
        adapter = build_registry_station_adapter(config, station=int(args.station))
    runtime = build_qualification(
        config,
        db_path=args.claims_db or None,
        adapter=adapter,
    )
    if runtime is None:
        log("sweep disabled by egress_catalog.qualification.enabled")
        return 2
    try:
        runtime.sweep.config.station = int(args.station)
        runtime.sweep.config.max_hosts = max(0, int(args.max or 0))
        if str(args.transport) == "auto":
            runtime.sweep.config.strategy = "auto"
            runtime.sweep.config.manual_transport = ""
        else:
            runtime.sweep.config.strategy = "manual"
            runtime.sweep.config.manual_transport = str(args.transport)
        if args.shadow:
            ingested = runtime.ingest()
            payload = {
                "mode": "shadow",
                "ingested": ingested,
                "summary": {
                    **runtime.summary(),
                    "by_status": {
                        str(key): value for key, value in runtime.summary()["by_status"].items()
                    },
                },
            }
        else:
            result = asyncio.run(
                runtime.sweep.run_epoch(
                    epoch_id=str(args.epoch or ""),
                    max_hosts=max(0, int(args.max or 0)),
                    shadow=False,
                )
            )
            payload = {"mode": "live", "result": result.snapshot(), "summary": runtime.summary()}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
