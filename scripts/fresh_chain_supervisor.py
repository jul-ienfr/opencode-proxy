#!/usr/bin/env python3
"""Supervise the fresh-IP chains for every station.

Each broken station gets its own chain (shared catalog, exclusive hostname
claims). Chains run in parallel; a global rebind budget keeps the NordVPN AUTH
rate safe. A chain stops on a real 200; the winner is persisted in the shared
IP registry and pinned for the next proxy boot.

Usage:
    python scripts/fresh_chain_supervisor.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import yaml_get  # noqa: E402
from free.chain_state import (  # noqa: E402
    load_state as _load_chain_state,
    save_state as _save_chain_state,
)
from free.ip_registry.config import load_registry_config  # noqa: E402
from free.repair_queue import RepairQueue  # noqa: E402

INTERVAL_S = 20.0
HEALTH_GRACE_S = 600.0
# When the allocator is the only repair owner (station_repair disabled), it
# must not wait the watchdog's 10-minute grace: the station would stay out of
# routing for nothing. A short grace still absorbs boot transitions.
ALLOCATOR_ONLY_GRACE_S = 30.0
LOG = ROOT / "logs" / "fresh_chain_supervisor.log"
EVENTS_LOG = ROOT / "logs" / "allocator_events.jsonl"
LOCK = ROOT / "logs" / "fresh_chain_supervisor.lock"
ALLOCATOR_STATE = ROOT / "logs" / "allocator_state.json"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _recent_429(window_s: float) -> int:
    path = ROOT / "logs" / "registry.db"
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM claims "
                    "WHERE status='429' AND updated_at >= ?",
                    (time.time() - max(1.0, float(window_s)),),
                ).fetchone()["n"]
            )
        finally:
            conn.close()
    except Exception:
        return 0


def _total_429() -> int:
    path = ROOT / "logs" / "registry.db"
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM claims WHERE status='429'"
                ).fetchone()["n"]
            )
        finally:
            conn.close()
    except Exception:
        return 0


def _write_allocator_state(state: dict) -> None:
    try:
        from free.chain_state import save_json_state

        save_json_state(ALLOCATOR_STATE, state)
    except Exception:
        pass


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def event(kind: str, **fields: object) -> None:
    """Append a structured allocator event (JSONL, station-correlated)."""

    payload = {
        "ts": time.time(),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": str(kind),
    }
    payload.update(fields)
    try:
        with open(EVENTS_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _pid_alive(pid: int, cmdline_contains: str = "") -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        proc = psutil.Process(pid)
        if cmdline_contains:
            cmdline = " ".join(proc.cmdline()).replace("\\", "/")
            return cmdline_contains.replace("\\", "/") in cmdline
        return proc.is_running()
    except Exception:
        pass
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=_NO_WINDOW,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _acquire_supervisor_lock() -> bool:
    """One supervisor per machine — lancer-gui.bat and lancer-proxy.bat both
    spawn one, and two supervisors race the same stations/config.yaml."""
    if LOCK.exists():
        try:
            data = json.loads(LOCK.read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
            if pid and _pid_alive(pid, "fresh_chain_supervisor"):
                return False
        except Exception:
            pass
    try:
        LOCK.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
        return True
    except Exception:
        return False


def _release_supervisor_lock() -> None:
    try:
        data = json.loads(LOCK.read_text(encoding="utf-8"))
        if int(data.get("pid") or 0) == os.getpid():
            LOCK.unlink()
    except Exception:
        pass


TRANSPORTS = ("auto", "wireguard", "openvpn-udp", "openvpn-tcp")
# [2026-11-03 auto protocol change] A station that fails N consecutive
# OpenVPN chains is stuck on the AUTH path, not on a bad hostname. Retrying
# the same protocol burns candidates and leaves the station down; switch it
# to WireGuard until it wins again.
_TRANSPORT_FAILURES: dict[int, int] = {}
_TRANSPORT_OVERRIDE: dict[int, str] = {}


def _lock_state(station: int, transport: str = "wireguard") -> tuple[Path, int]:
    suffix = "" if transport == "wireguard" else f"_{transport}"
    path = ROOT / "logs" / f"fresh_chain_{station}{suffix}.lock"
    if not path.exists():
        return path, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return path, int(data.get("pid") or 0)
    except Exception:
        return path, 0


def _live_lock(station: int) -> tuple[str, Path, int]:
    """Return the first live chain lock for a station (any transport)."""

    for transport in TRANSPORTS:
        path, pid = _lock_state(station, transport)
        if path.exists() and pid and _pid_alive(pid, "fresh_chain.py"):
            return transport, path, pid
    return "wireguard", _lock_state(station)[0], 0


def _winner(station: int, transport: str = "wireguard") -> dict | None:
    suffix = "" if transport == "wireguard" else f"_{transport}"
    path = ROOT / "logs" / f"fresh_chain_state_{station}{suffix}.json"
    state = _load_chain_state(path)
    winner = state.get("winner")
    return winner if isinstance(winner, dict) and winner.get("ip") else None


def _winner_any(station: int) -> dict | None:
    for transport in TRANSPORTS:
        winner = _winner(station, transport)
        if winner is not None:
            return winner
    return None


def _clear_winner(station: int, transport: str = "wireguard") -> None:
    """Drop a winner that the live tunnel contradicts.

    [2026-11-03] A persisted winner from an earlier run kept overwriting the
    `.env` pin of a station that had since landed on another egress (s5 was
    live on nl1055 and got re-pinned to the old al45). The live tunnel wins.
    """
    suffix = "" if transport == "wireguard" else f"_{transport}"
    path = ROOT / "logs" / f"fresh_chain_state_{station}{suffix}.json"
    state = _load_chain_state(path)
    if not state.get("winner"):
        return
    state["winner"] = None
    _save_chain_state(path, state)
    log(f"station {station} stale winner cleared (live egress differs)")


def _reconcile_registry(stations: dict[int, dict]) -> None:
    """Align the target registry leases with the live station egress.

    ``registry.db`` is the shared source of truth; a chain can be stopped by
    the supervisor just after the station became healthy. This reconciliation
    releases stale assignments and records a real winner (host + egress) when
    the chain state proves it.
    """

    try:
        from free.ip_registry.registry_db import (
            assign_observed,
            assigned_for_station,
            reconcile as reconcile_leases,
            release_station,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"registry reconcile unavailable: {type(exc).__name__}: {exc}")
        return
    try:
        lease_cleanup = reconcile_leases()
        if any(lease_cleanup.values()):
            log(
                "registry leases: "
                f"expired={lease_cleanup.get('released_leases', 0)} "
                f"revived_ips={lease_cleanup.get('revived_ips', 0)} "
                f"orphan_ips={lease_cleanup.get('released_ips', 0)}"
            )
    except Exception as exc:  # noqa: BLE001
        log(f"lease reconcile failed (continuing): {type(exc).__name__}: {exc}")
    now = time.time()
    for sid, station in stations.items():
        ip = str(station.get("current_ip") or "").strip()
        vpn = station.get("vpn") if isinstance(station.get("vpn"), dict) else {}
        host = str(
            vpn.get("current_host")
            or station.get("current_host")
            or ""
        ).strip()
        assigned = assigned_for_station(sid)
        assigned_ip = str((assigned or {}).get("ip") or "")
        if assigned is not None and (
            (ip and assigned_ip != ip)
            or not bool(station.get("station_usable"))
        ):
            reason = "live_mismatch" if ip and assigned_ip != ip else "live_unusable"
            release_station(sid, reason, now=now)
            log(
                f"registry: station {sid} released {assigned_ip} "
                f"({reason}; live {ip or '-'})"
            )
            assigned = None
        winner = _winner_any(sid)
        winner_host = str(winner.get("host") or "").strip() if winner else ""
        host = host or winner_host
        winner_ip = str(winner.get("ip") or "") if winner else ""
        functional = bool(station.get("station_usable"))
        if (
            ip
            and host
            and functional
            and (winner_ip in ("", ip))
            and (assigned is None or assigned_ip != ip)
        ):
            record = assign_observed(
                egress_ip=ip,
                hostname=host,
                country=str((winner or {}).get("country") or ""),
                station=sid,
                tested_at=now,
            )
            if record is not None:
                log(f"registry: station {sid} keeps live {ip} ({host})")


def _start_chain(
    station: int,
    rebind_limit: int = 4,
    max_candidates: int = 20,
    models: str = "",
    transport: str = "wireguard",
) -> None:
    log_path = ROOT / "logs" / f"fresh_chain_{station}.log"
    handle = open(log_path, "ab")
    command = [
        sys.executable,
        "-X",
        "utf8",
        str(ROOT / "scripts" / "fresh_chain.py"),
        "--station",
        str(station),
        "--claims",
        "--delay",
        "8",
        "--rebind-limit",
        str(max(1, int(rebind_limit))),
        "--max",
        str(max(1, int(max_candidates))),
        "--refresh",
    ]
    if transport and transport != "wireguard":
        command.extend(["--transport", transport])
    if models:
        command.extend(["--models", models])
    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=handle,
        stderr=handle,
        creationflags=_NO_WINDOW,
    )
    log(
        f"station {station} chain started pid={proc.pid} "
        f"(slice {max_candidates}, transport {transport})"
    )
    event(
        "chain_started",
        station=station,
        pid=proc.pid,
        transport=transport,
        slice=max_candidates,
    )


def _status() -> dict:
    try:
        return requests.get("http://127.0.0.1:4000/api/vpn-status", timeout=20).json()
    except Exception:
        return {}


def _free_health() -> dict:
    try:
        return requests.get("http://127.0.0.1:4000/api/free-health", timeout=20).json()
    except Exception:
        return {}


def _payload_age_s(payload: dict, fields: tuple[str, ...]) -> float | None:
    """Age of a control-plane snapshot, in seconds (None when unknown)."""

    if not isinstance(payload, dict):
        return None
    for field in fields:
        value = payload.get(field)
        if value is None or value == "":
            continue
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            pass
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            return max(0.0, (datetime.now(UTC) - stamp.astimezone(UTC)).total_seconds())
        except (TypeError, ValueError):
            continue
    return None


def _control_state_stale(
    status: dict, health: dict, *, max_age_s: float
) -> tuple[bool, str]:
    """Fail-safe: never act on an empty or stale control-plane snapshot."""

    if not isinstance(status, dict) or not status.get("stations"):
        return True, "vpn-status unavailable"
    if not isinstance(health, dict) or not health:
        return True, "free-health unavailable"
    age = _payload_age_s(status, ("refreshed_at", "status_age_s"))
    if age is not None and age > max_age_s:
        return True, f"vpn-status stale {age:.0f}s"
    h_age = _payload_age_s(health, ("generated_at", "status_age_s"))
    if h_age is not None and h_age > max_age_s:
        return True, f"free-health stale {h_age:.0f}s"
    return False, ""


def _need_rank(station: dict) -> int:
    """Repair priority: model-exhausted tunnel-up first, then connected, then down."""

    if station.get("station_usable") and not station.get("model_usable"):
        return 0
    if str(station.get("vpn_status") or "") == "connected":
        return 1
    return 2


def _manager_repair_enabled() -> bool:
    """True when the proxy watchdog parks/repairs stations by itself.

    With ``ip_rotation.station_repair.enabled: false`` the allocator is the
    sole repair owner and must act after a short grace, not the watchdog's
    10-minute one.
    """

    try:
        return bool(yaml_get("ip_rotation", "station_repair.enabled", True))
    except Exception:
        return True


def _preferred_transport(
    station: dict, configured: str, fallback: str = "wireguard"
) -> str:
    """Use WireGuard when OpenVPN is stuck in AUTH throttling.

    AUTH_FAILED is a credential/rate limit on the OpenVPN path; rebinding the
    same hostname over WireGuard avoids it without touching the request layer.
    """

    configured = str(configured or "wireguard").strip() or "wireguard"
    fallback = str(fallback or "").strip()
    if configured == "auto":
        # [contrat test_station_allocator_supervisor] `auto` garde la boucle
        # tri-stack même sur une station auth-throttled. Le forçage WireGuard
        # se fait au niveau du spawn/claim (`--transport wireguard`), pas ici.
        return "auto"
    if not fallback or configured == fallback:
        return configured
    if not configured.startswith("openvpn"):
        return configured
    # The manager may already have flipped this station to WireGuard (manual
    # or automatic AUTH recovery). Rebuilding it as OpenVPN would undo that
    # and fail with no-egress, so the live effective stack wins.
    vpn = station.get("vpn") if isinstance(station.get("vpn"), dict) else {}
    effective = str(
        vpn.get("stack_effective") or vpn.get("assigned_stack") or ""
    ).strip().lower()
    if effective == "wireguard":
        return fallback
    health = station.get("health") if isinstance(station.get("health"), dict) else {}
    text = f"{health.get('cause') or ''} {health.get('last_error') or ''}".lower()
    if "auth" in text or "credential" in text:
        return fallback
    return configured


def _needs_chain(station: dict) -> bool:
    """A station that cannot serve a free request needs a fresh egress.

    The shared catalog/claims layer prevents duplicate hostnames; this predicate
    only answers “does this station need a new IP right now?”.

    The proxy watchdog is the first repair owner. While it is repairing or
    probing a station, the allocator must not recreate the same container:
    doing so was the main source of mass ``no-egress`` failures.
    """
    health = station.get("health") if isinstance(station.get("health"), dict) else {}
    hstate = str(health.get("state") or "")
    vpn_status = str(station.get("vpn_status") or "")
    if hstate == "parked":
        cause_text = (
            f"{health.get('cause') or ''} {health.get('last_error') or ''}"
        ).lower()
        if "auth" in cause_text or "credential" in cause_text:
            return False
        # An egress/deadline park is exactly what an IP rotation can fix; the
        # manager will not touch the station until the park expires.
        if "egress" in cause_text or "deadline" in cause_text:
            return True
        return False
    if hstate in ("repairing", "probing"):
        # A hard tunnel error is not a transition: when the allocator owns
        # repairs, no amount of grace makes it recover, so rebind immediately
        # while the other stations keep serving. When the watchdog owns
        # repairs, it gets the grace and the allocator stays out of its way.
        if vpn_status in ("error", "disconnected", "stopped") and not _manager_repair_enabled():
            return True
        try:
            repairing_s = float(health.get("repairing_s") or 0.0)
        except (TypeError, ValueError):
            repairing_s = 0.0
        if repairing_s <= 0.0:
            # Restored health state has no repairing_since timestamp. Use the
            # age of the last success/probe instead of waiting forever.
            try:
                repairing_s = float(
                    health.get("last_ok_age_s")
                    or health.get("last_probe_age_s")
                    or 0.0
                )
            except (TypeError, ValueError):
                repairing_s = 0.0
        _grace = HEALTH_GRACE_S if _manager_repair_enabled() else ALLOCATOR_ONLY_GRACE_S
        if repairing_s < _grace:
            return False
    if not str(station.get("current_ip") or "").strip():
        return True
    if station.get("station_usable") and station.get("model_usable"):
        return False
    # [2026-11-03] Operator policy: an unusable station is an unusable IP.
    # Change it, whatever the reason or the tunnel state. The only exception
    # (auth/credentials) is handled by the parked branch above; no IP change
    # can fix it. This removes the old "proven IP" protection that kept
    # stations connected on a dead daily bucket for hours.
    return True


def _station_recovered(station: dict) -> bool:
    """True only when the station can actually serve a free request.

    ``not _needs_chain`` is not the same thing: the watchdog reports
    ``repairing``/``probing`` while it works, which made the supervisor stop a
    chain that was 20 s old, then restart it on the next poll.  That stop/start
    loop was the observed 20 s churn.
    """

    health = station.get("health") if isinstance(station.get("health"), dict) else {}
    if str(health.get("state") or "") in ("repairing", "probing", "suspect", "parked"):
        return False
    if not bool(station.get("station_usable")):
        return False
    if bool(station.get("model_usable")):
        return True
    reason = str(station.get("model_usable_reason") or "")
    # model_usable is evaluated for one model only.  A per-model quota block
    # still needs a fresh IP, so the chain must keep going.
    return reason not in (
        "daily-bucket-exhausted",
        "bad-ip",
        "quota",
        "quota-429",
        "tunnel-down",
        "station-unusable",
    )


def _station_hopeless(station: dict) -> bool:
    """Repair needs credentials, not a new IP: stop the chain."""

    health = station.get("health") if isinstance(station.get("health"), dict) else {}
    cause = f"{health.get('cause') or ''} {health.get('last_error') or ''}".lower()
    return str(health.get("state") or "") == "parked" and (
        "auth" in cause or "credential" in cause
    )


def _ledger_ip_exhausted(free_health: dict, ip: str, models: list[str] | None = None) -> bool:
    """Is this egress IP's daily bucket exhausted in the authoritative ledger?

    The quota lease is a quality/headroom estimate that expires; the ledger is
    the ``(IP, UTC day)`` truth. Using only the lease made a 429 station look
    healthy again after ``bad_ip_ttl``, so the allocator never replaced its
    burned IP and it stayed out of routing until midnight.
    """

    target = str(ip or "").strip()
    if not target:
        return False
    models_set = {str(m or "").strip() for m in (models or []) if str(m or "").strip()}
    ledger = (free_health or {}).get("quota_ledger") or {}
    exhausted: set[str] = set()
    for bucket in ledger.get("buckets") or []:
        if not isinstance(bucket, dict):
            continue
        if str(bucket.get("ip") or "").strip() != target:
            continue
        if not bucket.get("exhausted"):
            continue
        exhausted.add(str(bucket.get("bucket") or "default").strip())
    # The default bucket is shared by every free model: one exhausted entry
    # burns the whole IP for the day.
    if "default" in exhausted:
        return True
    # Model-scoped ledgers: only exhausted when every model we care about has
    # its own exhausted bucket. A missing/untested bucket is not exhaustion.
    return bool(models_set) and models_set.issubset(exhausted)


def _warm_spares_count() -> int:
    """Verified spare egresses still inside their TTL (warm-pool size)."""
    path = ROOT / "logs" / "registry.db"
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM warm_pool WHERE expires_at > ?",
                    (time.time(),),
                ).fetchone()[0]
            )
        finally:
            conn.close()
    except Exception:
        return 0


def _sweep_state_age_s() -> float | None:
    """Age of the newest chain-state write, or None when no chain exists."""

    path = ROOT / "logs" / "registry.db"
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT MAX(updated_at) FROM chain_state "
                "WHERE namespace LIKE 'fresh_chain_state_%'"
            ).fetchone()
        finally:
            conn.close()
        if not row or row[0] is None:
            return None
        return max(0.0, time.time() - float(row[0]))
    except Exception:
        return None


def _sync_fleet_incidents(
    data: dict, stations: dict[int, dict], warm_count: int
) -> None:
    """Persist the fleet-level alerts in ``registry.db`` (T051)."""

    try:
        from observability.incidents import sync_incidents

        if not bool(yaml_get("ip_registry", "alerts.enabled", True)):
            sync_incidents([], now=time.time())
            return
        usable_min = int(
            yaml_get("ip_registry", "alerts.client_usable_min", 5) or 5
        )
        warm_min = int(yaml_get("ip_registry", "alerts.warm_pool_min", 3) or 3)
        stalled_s = float(
            yaml_get("ip_registry", "alerts.sweep_stalled_s", 900) or 900
        )
        usable = sum(
            1 for station in stations.values() if station.get("model_usable")
        )
        alerts: list[dict] = []
        if usable < usable_min:
            alerts.append(
                {
                    "scope": "fleet",
                    "cause": "client_usable_low",
                    "remediation": "repair chains run; see /api/free/chain",
                    "details": {
                        "usable": usable,
                        "stations": len(stations),
                        "minimum": usable_min,
                    },
                }
            )
        if warm_count < warm_min:
            alerts.append(
                {
                    "scope": "fleet",
                    "cause": "warm_pool_low",
                    "remediation": "keep scanning winners; warm_pool is a cache",
                    "details": {"warm_pool": warm_count, "minimum": warm_min},
                }
            )
        capacity = data.get("capacity") if isinstance(data, dict) else None
        if not isinstance(capacity, dict):
            capacity = data.get("capacity_snapshot") if isinstance(data, dict) else {}
        capacity = capacity if isinstance(capacity, dict) else {}
        if capacity.get("capacity_degraded") or capacity.get("degraded"):
            alerts.append(
                {
                    "scope": "fleet",
                    "cause": "capacity_degraded",
                    "remediation": "capacity below configured station count",
                    "details": {
                        "reason": capacity.get("capacity_reason"),
                        "configured": capacity.get("configured_stations"),
                        "usable": capacity.get("usable_stations"),
                    },
                }
            )
        by_ip: dict[str, list[int]] = {}
        for sid, station in stations.items():
            ip = str(station.get("current_ip") or "").strip()
            if ip:
                by_ip.setdefault(ip, []).append(int(sid))
        for ip, sids in by_ip.items():
            if len(sids) > 1:
                alerts.append(
                    {
                        "scope": "fleet",
                        "cause": "duplicate_ip",
                        "remediation": "rotate one of the stations to a new egress",
                        "details": {"ip": ip, "stations": sorted(sids)},
                    }
                )
        age = _sweep_state_age_s()
        if age is not None and age > stalled_s and any(
            _needs_chain(station) for station in stations.values()
        ):
            alerts.append(
                {
                    "scope": "fleet",
                    "cause": "sweep_stalled",
                    "remediation": "restart or inspect the chain supervisor",
                    "details": {"state_age_s": round(age, 1), "limit_s": stalled_s},
                }
            )
        result = sync_incidents(alerts, now=time.time())
        if result.get("opened") or result.get("closed"):
            log(
                f"incidents: +{result.get('opened', 0)} "
                f"-{result.get('closed', 0)} open={result.get('open', 0)}"
            )
            event("incidents_synced", **result)
    except Exception as exc:  # noqa: BLE001
        log(f"incident sync failed (continuing): {type(exc).__name__}: {exc}")


def _ledger_reset_at(free_health: dict) -> float:
    """Earliest future daily-bucket reset in the authoritative ledger.

    [2026-11-03] When every bucket is exhausted, probing every 15 min burns
    AUTH and candidates for nothing: the free quota resets at a fixed UTC
    time. Pause until that reset instead, then let the normal repair resume.
    """
    ledger = (free_health or {}).get("quota_ledger") or {}
    if not ledger.get("exhausted"):
        return 0.0
    now = time.time()
    candidates: list[float] = []
    try:
        earliest = float(ledger.get("earliest_reset_at") or 0.0)
    except (TypeError, ValueError):
        earliest = 0.0
    if earliest > now:
        candidates.append(earliest)
    for bucket in ledger.get("buckets") or []:
        if not isinstance(bucket, dict) or not bucket.get("exhausted"):
            continue
        try:
            reset = float(bucket.get("reset_at") or 0.0)
        except (TypeError, ValueError):
            reset = 0.0
        if reset > now:
            candidates.append(reset)
    return min(candidates) if candidates else 0.0


def _station_primary_available(
    station: dict, free_health: dict, model: str
) -> bool:
    """Can this station serve ``model`` on its current egress?"""

    if not isinstance(free_health, dict) or not model:
        return True
    try:
        sid = int(station.get("station") or 0)
    except (TypeError, ValueError):
        return True
    ip = str(station.get("current_ip") or "").strip()
    if not sid or not ip:
        return False
    if _ledger_ip_exhausted(free_health, ip, [model]):
        return False
    lease = None
    for item in free_health.get("quota_leases") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("model") or "") != model:
            continue
        try:
            item_sid = int(item.get("station") or 0)
        except (TypeError, ValueError):
            continue
        if item_sid == sid and str(item.get("ip") or "").strip() == ip:
            lease = item
            break
    if lease is not None:
        state = str(lease.get("state") or "")
        try:
            blocked = float(lease.get("blocked_remaining_s") or 0.0)
        except (TypeError, ValueError):
            blocked = 0.0
        return bool(
            lease.get("selectable")
            and state not in ("bad", "exhausted", "draining")
            and blocked <= 0.0
        )
    observed = None
    for item in free_health.get("model_ip_health") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("model") or "") != model:
            continue
        try:
            item_sid = int(item.get("station") or 0)
        except (TypeError, ValueError):
            continue
        if item_sid == sid and str(item.get("ip") or "").strip() == ip:
            observed = item
            break
    if observed is not None:
        try:
            blocked = float(observed.get("blocked_s") or 0.0)
        except (TypeError, ValueError):
            blocked = 0.0
        return int(observed.get("last_status") or 0) == 200 and blocked <= 0.0
    # No data for this IP: a fresh/untested bucket is not exhaustion.
    return True


def _primary_quota_count(
    free_health: dict, stations: dict[int, dict], scope: list[int], model: str
) -> int:
    """How many stations can still serve ``model`` right now."""

    if not isinstance(free_health, dict) or not model:
        return len(scope)
    return sum(
        1
        for sid in scope
        if _station_primary_available(stations.get(sid, {}), free_health, model)
    )


def _proven_live_quota_ips(
    free_health: dict, *, now: float | None = None, within_s: float = 180.0
) -> set[str]:
    """IPs that served a real 200 recently and still have a live bucket.

    A synthetic probe can time out while a busy tunnel is still answering real
    requests.  Rotating such an IP costs a proven winner and, when the catalog
    is dry, replaces it with a 429.  Those IPs are therefore protected from
    repair until the grace window expires.
    """

    ts = float(now if now is not None else time.time())
    out: set[str] = set()
    ledger = (free_health or {}).get("quota_ledger") or {}
    for bucket in ledger.get("buckets") or []:
        if not isinstance(bucket, dict) or bucket.get("exhausted"):
            continue
        ip = str(bucket.get("ip") or "").strip()
        if not ip:
            continue
        try:
            successes = int(bucket.get("successes") or 0)
            last_seen = float(bucket.get("last_seen") or 0.0)
        except (TypeError, ValueError):
            continue
        if successes > 0 and ts - last_seen <= max(30.0, float(within_s)):
            out.add(ip)
    return out


def _proven_quota_station(station: dict, proven_ips: set[str]) -> bool:
    ip = str(station.get("current_ip") or "").strip()
    if not ip or ip not in proven_ips:
        return False
    return str(station.get("vpn_status") or "") in ("connected", "degraded")


def _ip_shared_with_other(stations: dict[int, dict], sid: int, ip: str) -> bool:
    """True when another station currently owns the same egress IP."""

    target = str(ip or "").strip()
    if not target:
        return False
    for other_sid, other in (stations or {}).items():
        try:
            if int(other_sid) == int(sid):
                continue
        except (TypeError, ValueError):
            continue
        if str(other.get("current_ip") or "").strip() == target:
            return True
    return False


def _winner_protected(station: dict, winner: dict | None) -> bool:
    """Keep a proven egress while health probes flap.

    A winner stays protected as long as the tunnel is connected, egress is not
    declared down, and the live IP is still the winner IP. No IP range or
    country gets a special protection.
    """

    if not isinstance(winner, dict) or not winner.get("ip"):
        return False
    live_ip = str(station.get("current_ip") or "").strip()
    if not live_ip:
        return False
    vpn_status = str(station.get("vpn_status") or "")
    if vpn_status not in ("connected", "degraded"):
        return False
    vpn = station.get("vpn") if isinstance(station.get("vpn"), dict) else {}
    egress = str(
        station.get("egress_state") or vpn.get("egress_state") or ""
    ).strip()
    if egress == "tunnel_down":
        return False
    if live_ip != str(winner.get("ip") or "").strip():
        return False
    return bool(station.get("station_usable"))


def _winner_cooldown_applies(
    live_ip: str,
    winner: dict | None,
    elapsed: float,
    cooldown: float,
    *,
    duplicate: bool = False,
    grace: float = 0.0,
) -> bool:
    """Keep the winner cooldown only when the station is actually on it.

    A stale ``fresh_chain_state`` file from an earlier repair used to freeze a
    broken station for 5 minutes even though its live IP was a different one.
    A duplicate egress (two stations on one IP) is never a reason to wait.

    [2026-11-03] ``grace`` is the settle window after a fresh winner. The
    manager needs to recreate the container and reconnect before ``live_ip``
    can match the winner; relaunching a chain at t+0 burned every winner and
    produced an endless rebind loop (the supervisor's own churn).
    """

    if duplicate:
        return False
    if float(elapsed) < max(0.0, float(grace)):
        return True
    if float(elapsed) >= max(0.0, float(cooldown)):
        return False
    live = str(live_ip or "").strip()
    if not live:
        return False
    target = str((winner or {}).get("ip") or "").strip()
    return live == target


def _station_exhausted(
    station: dict, free_health: dict, probe_models: list[str]
) -> bool:
    """True when every configured probe bucket is dead on the current IP.

    One draining model is normal and must not rotate the IP.  All probe buckets
    non-selectable means the egress itself is burned, so a fresh hostname is
    required even though the tunnel is technically up.
    """

    try:
        sid = int(station.get("station") or 0)
    except (TypeError, ValueError):
        sid = 0
    ip = str(station.get("current_ip") or "").strip()
    models = [str(model).strip() for model in probe_models if str(model).strip()]
    if not sid or not ip or not models:
        return False
    if _ledger_ip_exhausted(free_health, ip, models):
        return True
    leases: dict[str, dict] = {}
    for item in (free_health or {}).get("quota_leases") or []:
        if not isinstance(item, dict):
            continue
        try:
            item_sid = int(item.get("station") or 0)
        except (TypeError, ValueError):
            continue
        if item_sid != sid or str(item.get("ip") or "").strip() != ip:
            continue
        leases[str(item.get("model") or "").strip()] = item
    if not leases:
        return False
    for model in models:
        item = leases.get(model)
        if item is None:
            # An untried bucket can still be fresh: do not rotate on partial
            # information.
            return False
        state = str(item.get("state") or "")
        try:
            blocked = float(item.get("blocked_remaining_s") or 0.0)
        except (TypeError, ValueError):
            blocked = 0.0
        if (
            bool(item.get("selectable"))
            and state not in ("bad", "exhausted")
            and blocked <= 0.0
        ):
            return False
    return True


def _exhaustion_due(zero_polls: int, total_429: int, threshold: int) -> bool:
    """Require a sustained zero-usable fleet before pausing the catalog burn.

    A single poll can catch a transient 200 between two 429s; the old
    immediate check never fired on a flapping fleet. Three consecutive polls
    (~60 s at the default interval) are enough to distinguish a real outage
    from a one-tick recovery.
    """
    return zero_polls >= 3 and total_429 >= max(1, int(threshold))


def _stop_chain(station: int, transport: str = "wireguard") -> None:
    path, pid = _lock_state(station, transport)
    if not pid or not _pid_alive(pid, "fresh_chain.py"):
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                timeout=20,
                creationflags=_NO_WINDOW,
            )
        else:
            os.kill(pid, 15)
        log(f"station {station} chain stopped (station settled)")
        event("chain_stopped", station=station, pid=pid)
    except Exception as exc:
        log(f"station {station} chain stop failed: {type(exc).__name__}: {exc}")
    try:
        path.unlink()
    except Exception:
        pass


def _set_env(key: str, value: str) -> None:
    path = ROOT / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        lines = []
    found = False
    for index, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[index] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    except Exception as exc:
        log(f"env update {key} failed: {type(exc).__name__}: {exc}")


def _persist_pin(station: int, winner: dict) -> None:
    host = str(winner.get("host") or "").strip()
    country = str(winner.get("country") or "").strip()
    if not host:
        return
    _set_env(f"SERVER_HOSTNAMES_STATION{station}", host)
    if country:
        _set_env(f"SERVER_COUNTRIES_STATION{station}", country)
    log(f"station {station} pinned to {host} ({country}) egress={winner.get('ip')}")


def _env_value(key: str) -> str:
    """Read one key from the repo ``.env`` (empty when absent)."""

    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _clear_stale_pins(stations: dict[int, dict]) -> None:
    """Drop a compose pin that no longer matches the live tunnel.

    A pin is meant to preserve a proven egress.  When the station is broken or
    already serving a different host, keeping the old pin makes the next start
    reconnect to the dead server first — exactly the loop that kept stations
    unavailable.  The fresh chain will write a new winner.
    """

    for sid, station in stations.items():
        pin = _env_value(f"SERVER_HOSTNAMES_STATION{int(sid)}")
        if not pin:
            # [2026-11-03] A country pin without a hostname pin only narrows
            # the global perimeter: it is a leftover from an earlier winner.
            # Keeping it trapped every repaired station in Albania and
            # recreated the correlated quota exhaustion (0/6 incident).
            orphan = _env_value(f"SERVER_COUNTRIES_STATION{int(sid)}")
            if orphan:
                _set_env(f"SERVER_COUNTRIES_STATION{int(sid)}", "")
                log(f"station {sid} orphan country pin {orphan} cleared")
            continue
        vpn = station.get("vpn") if isinstance(station.get("vpn"), dict) else {}
        live_host = str(vpn.get("current_host") or "").strip()
        # A missing live host is not proof that the pin is wrong: the manager
        # may not have resolved it yet.  Only a known different host invalidates
        # the pin; the chain will overwrite it after the next winner.
        if live_host and pin != live_host:
            _set_env(f"SERVER_HOSTNAMES_STATION{int(sid)}", "")
            _set_env(f"SERVER_COUNTRIES_STATION{int(sid)}", "")
            log(f"station {sid} stale pin {pin} cleared (live={live_host})")


def _scope() -> list[int]:
    """Stations the allocator may repair (station_count minus operator opt-outs)."""

    try:
        count = max(1, int(yaml_get("ip_rotation", "station_count", 6) or 6))
    except Exception:
        count = 6
    raw_disabled = yaml_get("ip_rotation", "disabled_stations", []) or []
    disabled: set[int] = set()
    if isinstance(raw_disabled, (list, tuple, set)):
        for value in raw_disabled:
            try:
                disabled.add(int(value))
            except (TypeError, ValueError):
                continue
    return [sid for sid in range(1, count + 1) if sid not in disabled]


def _supervise() -> None:
    log("supervisor started (all stations, one chain per down station)")
    event("supervisor_started", stations=len(_scope()))
    last_winner: dict[int, str] = {}
    consumed_winner: dict[int, str] = {}
    pinned_winner: dict[int, str] = {}
    winner_at: dict[int, float] = {}
    protected_logged: set[int] = set()
    refresh_at: dict[int, float] = {}
    refresh_pending: set[int] = set()
    last_failed_at: dict[int, float] = {}
    healthy_since: dict[int, float] = {}
    previous_running: dict[int, bool] = {}
    last_mode: str | None = None
    repair_queue = RepairQueue()
    paused_until = 0.0
    stale_logged_at = 0.0
    last_alert_at = 0.0
    last_heartbeat = 0.0
    last_maintenance_at = time.time()
    last_cycle_log = 0.0
    zero_functional_polls = 0
    observe_logged: dict[int, str] = {}
    while True:
        poll = INTERVAL_S
        try:
            cfg = load_registry_config()
            alloc = cfg.get("allocator") if isinstance(cfg.get("allocator"), dict) else {}
            mode = str(alloc.get("mode") or "off")
            observe_only = mode == "observe"
            poll = max(5.0, float(alloc.get("poll_interval_s", INTERVAL_S) or INTERVAL_S))
            if mode == "off":
                if last_mode != "off":
                    log("allocator mode=off — supervisor idle")
                last_mode = "off"
                time.sleep(poll)
                continue
            now_heartbeat = time.time()
            if now_heartbeat - last_heartbeat >= 60.0:
                last_heartbeat = now_heartbeat
                log(f"heartbeat: mode={mode}")
            try:
                maint_interval = float(
                    yaml_get("ip_registry", "maintenance_interval_s", 21600)
                    or 21600
                )
            except (TypeError, ValueError):
                maint_interval = 21600.0
            if now_heartbeat - last_maintenance_at >= max(3600.0, maint_interval):
                last_maintenance_at = now_heartbeat
                try:
                    from rotation.infrastructure.db.maintenance import run_once

                    _maint = run_once(
                        ROOT / "logs" / "registry.db",
                        backup_dir=ROOT / "logs" / "backups",
                    )
                    log(
                        "periodic maintenance: version=%s integrity=%s "
                        "deleted=%s"
                        % (
                            _maint.get("version"),
                            _maint.get("integrity_ok"),
                            _maint.get("deleted"),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"periodic maintenance failed: {exc}")
            # [fleet-guard 2026-11-03] Repair every down station in parallel,
            # but the loop only ever touches a station that is actually down.
            # A station with a proven live bucket is excluded earlier, so a
            # repair can never take a serving station out of routing. The
            # shared claims budget paces the actual rebinds.
            scope = _scope()
            max_parallel = max(
                1, min(len(scope), int(alloc.get("max_parallel_repairs", 6) or 6))
            )
            max_candidates = max(
                1, int(alloc.get("max_candidates_per_chain", 20) or 20)
            )
            probe_models = ",".join(
                str(item).strip()
                for item in (alloc.get("probe_models") or [])
                if str(item).strip()
            )
            raw_transports = alloc.get("transports")
            transports: dict[str, str] = {}
            if isinstance(raw_transports, list):
                for item in raw_transports:
                    if not isinstance(item, dict):
                        continue
                    try:
                        sid = int(item.get("station") or 0)
                    except (TypeError, ValueError):
                        continue
                    transport = str(item.get("transport") or "").strip()
                    if sid and transport:
                        transports[str(sid)] = transport
            elif isinstance(raw_transports, dict):
                transports = {
                    str(key): str(value)
                    for key, value in raw_transports.items()
                    if str(value).strip()
                }

            auth_fallback = str(
                yaml_get(
                    "ip_registry",
                    "allocator.auth_fallback_transport",
                    "wireguard",
                )
                or ""
            ).strip()

            def _winner_for(station_id: int) -> dict | None:
                configured = transports.get(str(station_id), "wireguard")
                preferred = _preferred_transport(
                    stations.get(station_id, {}), configured, auth_fallback
                )
                return _winner(station_id, preferred)
            rebind_limit = max(
                1, int(alloc.get("rebind_limit_per_min", 4) or 4)
            )
            cooldown = max(0.0, float(alloc.get("winner_cooldown_s", 120) or 120))
            refresh_enabled = bool(alloc.get("refresh_when_exhausted", True))
            refresh_cooldown = max(
                60.0, float(alloc.get("refresh_cooldown_s", 900) or 900)
            )
            refresh_min_available = max(
                1, int(alloc.get("refresh_min_available", 3) or 3)
            )
            winner_grace = max(
                0.0, float(alloc.get("winner_grace_s", 20) or 20)
            )
            failure_cooldown = max(
                0.0, float(alloc.get("failure_cooldown_s", 300) or 300)
            )
            g_window = max(
                30.0, float(alloc.get("global_429_window_s", 600) or 600)
            )
            g_threshold = max(
                1, int(alloc.get("global_429_threshold", 20) or 20)
            )
            g_pause = max(60.0, float(alloc.get("global_429_pause_s", 900) or 900))
            ex_threshold = max(
                1, int(alloc.get("exhausted_threshold", 40) or 40)
            )
            ex_pause = max(300.0, float(alloc.get("exhausted_pause_s", 1800) or 1800))
            max_state_age_s = max(
                10.0,
                float(
                    yaml_get("ip_registry", "allocator.max_state_age_s", 90) or 90
                ),
            )
            now = time.time()
            recent_429 = _recent_429(g_window)
            if paused_until and now >= paused_until:
                log("global 429 pause expired — resuming chains")
                paused_until = 0.0
                _write_allocator_state(
                    {"paused_until": 0.0, "reason": None, "recent_429": recent_429}
                )
            if not paused_until and recent_429 >= g_threshold:
                paused_until = now + g_pause
                reason = f"global_429:{recent_429}/{int(g_window)}s"
                _write_allocator_state(
                    {
                        "paused_until": paused_until,
                        "reason": reason,
                        "recent_429": recent_429,
                    }
                )
                log(
                    f"{recent_429} 429 in {int(g_window)}s — pausing new chains "
                    f"{int(g_pause)}s ({reason})"
                )

            data = _status()
            free_health = _free_health()
            stale, stale_reason = _control_state_stale(
                data, free_health, max_age_s=max_state_age_s
            )
            if stale:
                if now - stale_logged_at >= 60.0:
                    stale_logged_at = now
                    log(f"control state stale — scans paused ({stale_reason})")
                time.sleep(min(poll, 10.0))
                continue
            stations = {
                int(s.get("station") or 0): s for s in data.get("stations", [])
            }
            if not observe_only:
                _reconcile_registry(stations)
                _clear_stale_pins(stations)
            functional = any(
                bool(station.get("model_usable"))
                for station in stations.values()
            )
            if functional:
                zero_functional_polls = 0
            else:
                zero_functional_polls += 1
            # [2026-11-03] Honest capacity alert: below 2 usable stations the
            # fleet is effectively down even if some tunnels are connected.
            usable_count = sum(
                1 for station in stations.values() if station.get("model_usable")
            )
            if usable_count < 2 and now - last_alert_at >= 300.0:
                last_alert_at = now
                log(
                    f"ALERT client_usable={usable_count}/{len(stations)} (<2) — "
                    f"warm_pool={_warm_spares_count()} — repair active"
                )
            try:
                _sync_fleet_incidents(data, stations, _warm_spares_count())
            except Exception as exc:  # noqa: BLE001
                log(f"fleet incident sync unavailable: {type(exc).__name__}: {exc}")
            total_429 = _total_429()
            if (
                not paused_until
                and _exhaustion_due(zero_functional_polls, total_429, ex_threshold)
            ):
                paused_until = now + ex_pause
                zero_functional_polls = 0
                reason = f"exhausted_429:{total_429}"
                _write_allocator_state(
                    {
                        "paused_until": paused_until,
                        "reason": reason,
                        "recent_429": total_429,
                    }
                )
                log(
                    f"{total_429} 429 cumulés et aucune station model-usable — "
                    f"pause {int(ex_pause)}s ({reason})"
                )
            broken = [sid for sid in scope if _needs_chain(stations.get(sid, {}))]
            forced: set[int] = set()
            try:
                for request in repair_queue.pending():
                    try:
                        sid = int(request.get("station") or 0)
                    except (TypeError, ValueError):
                        continue
                    if sid not in scope:
                        continue
                    forced.add(sid)
                    if sid not in broken:
                        broken.append(sid)
                        log(
                            f"station {sid} repair request: "
                            f"{request.get('reason')} model={request.get('model') or '-'} "
                            f"ip={request.get('ip') or '-'}"
                        )
            except Exception as exc:
                log(f"repair queue read failed: {type(exc).__name__}: {exc}")
            for sid in scope:
                if sid not in broken and _station_recovered(stations.get(sid, {})):
                    try:
                        repair_queue.complete(sid)
                    except Exception:
                        pass
            for sid in list(observe_logged):
                if sid not in broken:
                    observe_logged.pop(sid, None)
            proven_ips = _proven_live_quota_ips(free_health)
            probe_model_list = [
                part.strip() for part in probe_models.split(",") if part.strip()
            ]
            for sid in scope:
                if sid in broken:
                    continue
                station = stations.get(sid, {})
                current_ip = str(station.get("current_ip") or "")
                ledger_dead = _ledger_ip_exhausted(
                    free_health, current_ip, probe_model_list
                )
                duplicate = _ip_shared_with_other(stations, sid, current_ip)
                if (
                    _winner_protected(station, _winner_for(sid))
                    and not ledger_dead
                    and not duplicate
                    and bool(station.get("model_usable"))
                ):
                    continue
                if (
                    _proven_quota_station(station, proven_ips)
                    and not duplicate
                    and bool(station.get("model_usable"))
                ):
                    continue
                if _station_exhausted(station, free_health, probe_model_list):
                    broken.append(sid)
                    log(f"station {sid} every probe bucket is exhausted — new chain")

            for sid in list(broken):
                if sid in forced:
                    # An explicit pool 429 must not be cancelled by a
                    # model_usable flag from another model.
                    continue
                station = stations.get(sid, {})
                current_ip = str(station.get("current_ip") or "")
                ledger_dead = _ledger_ip_exhausted(
                    free_health, current_ip, probe_model_list
                )
                duplicate = _ip_shared_with_other(stations, sid, current_ip)
                if (
                    _winner_protected(station, _winner_for(sid))
                    and not ledger_dead
                    and not duplicate
                    and bool(station.get("model_usable"))
                ):
                    broken.remove(sid)
                    if sid not in protected_logged:
                        protected_logged.add(sid)
                        log(
                            f"station {sid} keeps winner egress "
                            f"{_winner_for(sid).get('ip')} while health probes flap"
                        )
                    continue
                if (
                    _proven_quota_station(station, proven_ips)
                    and not duplicate
                    and bool(station.get("model_usable"))
                ):
                    broken.remove(sid)
                    if sid not in protected_logged:
                        protected_logged.add(sid)
                        log(
                            f"station {sid} keeps proven egress "
                            f"{station.get('current_ip')} (real 200 < 180s)"
                        )

            def _rank(sid: int) -> int:
                return _need_rank(stations.get(sid, {}))

            broken.sort(key=lambda sid: (_rank(sid), sid))
            running: dict[int, bool] = {}
            running_transport: dict[int, str] = {}
            for sid in scope:
                transport, _path, pid = _live_lock(sid)
                running[sid] = bool(pid and _pid_alive(pid, "fresh_chain.py"))
                running_transport[sid] = transport
            if time.time() - last_cycle_log >= 60.0:
                last_cycle_log = time.time()
                log(
                    "cycle: broken="
                    f"{sorted(broken)} running="
                    f"{sorted(sid for sid, alive in running.items() if alive)}"
                )
            for sid in scope:
                # A chain that ended while the station is still broken did not
                # find a winner: back off before relaunching (catalog exhaustion
                # must not become a hot loop).
                if previous_running.get(sid) and not running.get(sid) and sid in broken:
                    last_failed_at[sid] = time.time()
                    _TRANSPORT_FAILURES[sid] = _TRANSPORT_FAILURES.get(sid, 0) + 1
                    _used = str(running_transport.get(sid) or "wireguard")
                    if (
                        _TRANSPORT_FAILURES[sid] >= 2
                        and _used.startswith("openvpn")
                        and str(auth_fallback or "").strip()
                    ):
                        _TRANSPORT_OVERRIDE[sid] = str(auth_fallback).strip()
                        log(
                            f"station {sid}: {_TRANSPORT_FAILURES[sid]} failed {_used} "
                            f"chains — switching transport to {_TRANSPORT_OVERRIDE[sid]}"
                        )
                    log(f"station {sid} chain ended without recovery - backoff")

            # Stop a chain only when the station is genuinely serving again (or
            # needs credentials, which no IP rotation can fix).  Treating
            # ``not _needs_chain`` as healthy killed chains during watchdog
            # repair and produced a 20 s stop/start loop.
            for sid in scope:
                station = stations.get(sid, {})
                if running.get(sid) and (
                    _station_recovered(station) or _station_hopeless(station)
                ):
                    since = healthy_since.setdefault(sid, time.time())
                    if _winner_for(sid) or _station_hopeless(station):
                        _stop_chain(sid, running_transport.get(sid, "wireguard"))
                        running[sid] = False
                        healthy_since.pop(sid, None)
                        continue
                    if time.time() - since >= winner_grace:
                        _stop_chain(sid, running_transport.get(sid, "wireguard"))
                        running[sid] = False
                        healthy_since.pop(sid, None)
                elif sid in broken:
                    healthy_since.pop(sid, None)

            # Persist each winner as soon as the chain proves a 200. Waiting
            # for `station_usable` deadlocked the repair: the manager could
            # not adopt the chain's bind, so the live IP never matched and the
            # pin was never written. The winner is already probe-verified;
            # `_clear_stale_pins` removes it if the tunnel later lands
            # elsewhere.
            for sid in scope:
                winner = _winner_for(sid)
                if not winner:
                    continue
                signature = f"{winner.get('host')}|{winner.get('ip')}"
                # [2026-11-03] Only a fresh winner may be persisted. A winner
                # loaded from an earlier run (no ``winner_at`` or older than
                # 5 min) is stale: persisting it re-pinned stations to dead
                # servers after every supervisor restart.
                winner_ts = float(winner.get("winner_at") or 0.0)
                if not winner_ts or time.time() - winner_ts > 300.0:
                    for _t in TRANSPORTS:
                        _clear_winner(sid, _t)
                    continue
                station = stations.get(sid, {})
                live_ip = str(station.get("current_ip") or "").strip()
                if live_ip and live_ip != str(winner.get("ip") or ""):
                    # The station is already serving from another egress: the
                    # old winner file must not overwrite the live pin.
                    for _t in TRANSPORTS:
                        _clear_winner(sid, _t)
                    continue
                if pinned_winner.get(sid) != signature:
                    _persist_pin(sid, winner)
                    pinned_winner[sid] = signature
                    _TRANSPORT_FAILURES.pop(sid, None)
                    _TRANSPORT_OVERRIDE.pop(sid, None)

            for sid in list(broken):
                winner = _winner_for(sid)
                if not winner:
                    continue
                signature = f"{winner.get('host')}|{winner.get('ip')}"
                if consumed_winner.get(sid) == signature:
                    continue
                station = stations.get(sid, {})
                live_ip = str(station.get("current_ip") or "").strip()
                if (
                    bool(station.get("station_usable"))
                    and live_ip
                    and live_ip != str(winner.get("ip") or "")
                ):
                    # The station is already serving from another egress: the
                    # old winner file must not overwrite the live pin.
                    consumed_winner[sid] = signature
                    continue
                if last_winner.get(sid) != signature:
                    last_winner[sid] = signature
                    winner_at[sid] = time.time()
                elapsed = time.time() - winner_at.get(sid, 0.0)
                # The cooldown protects a winner that is actually bound. If
                # the station is on another IP (or has no IP), the old winner
                # cannot serve it: waiting 5 min just leaves the station down.
                if _winner_cooldown_applies(
                    live_ip,
                    winner,
                    elapsed,
                    cooldown,
                    duplicate=_ip_shared_with_other(stations, sid, live_ip),
                    grace=winner_grace,
                ):
                    broken.remove(sid)
                else:
                    log(
                        f"station {sid} still broken {int(elapsed)}s after winner "
                        f"{signature} — new chain"
                    )
                    consumed_winner[sid] = signature

            # Fleet-wide quota refresh: only when no station has a live bucket
            # for the primary free model.  This is the safe moment to rotate a
            # good IP, because it can no longer serve that model anyway.
            if refresh_enabled and probe_model_list:
                primary = probe_model_list[0]
                available = _primary_quota_count(
                    free_health, stations, scope, primary
                )
                need_refresh = available < len(scope) and (
                    available >= refresh_min_available or available <= 0
                )
                if need_refresh:
                    now_ts = time.time()
                    eligible = [
                        sid
                        for sid in scope
                        if sid not in broken
                        and not running.get(sid)
                        and str(
                            stations.get(sid, {}).get("vpn_status") or ""
                        )
                        in ("connected", "degraded")
                        and str(
                            stations.get(sid, {}).get("current_ip") or ""
                        ).strip()
                        and not _station_primary_available(
                            stations.get(sid, {}), free_health, primary
                        )
                        and now_ts - refresh_at.get(sid, 0.0)
                        >= refresh_cooldown
                    ]
                    if eligible:
                        sid = min(eligible)
                        broken.append(sid)
                        refresh_at[sid] = now_ts
                        refresh_pending.add(sid)
                        log(
                            f"{primary}: {available}/{len(scope)} stations "
                            f"still have quota — refreshing station {sid}"
                        )

            # Launch every broken station; the shared claims DB serializes
            # rebinds (global token bucket) while probes run in parallel.
            if paused_until and time.time() < paused_until:
                previous_running = dict(running)
                last_mode = mode
                time.sleep(min(poll, max(5.0, paused_until - time.time())))
                continue
            active = sum(1 for sid in scope if running.get(sid))
            for sid in broken:
                if running.get(sid):
                    continue
                if observe_only:
                    reason = str(
                        stations.get(sid, {}).get("non_routable_reason")
                        or stations.get(sid, {}).get("vpn_status")
                        or "unusable"
                    )
                    if observe_logged.get(sid) != reason:
                        observe_logged[sid] = reason
                        log(f"observe: station {sid} would repair ({reason})")
                    continue
                failed_ago = time.time() - last_failed_at.get(sid, 0.0)
                if last_failed_at.get(sid) and failed_ago < failure_cooldown:
                    continue
                if active >= max_parallel:
                    break
                configured_transport = transports.get(str(sid), "wireguard")
                transport = _TRANSPORT_OVERRIDE.get(sid) or _preferred_transport(
                    stations.get(sid, {}), configured_transport, auth_fallback
                )
                if transport != configured_transport:
                    log(
                        f"station {sid} AUTH on {configured_transport} — "
                        f"repair over {transport}"
                    )
                if transport not in TRANSPORTS:
                    transport = "wireguard"
                path, _pid = _lock_state(sid, transport)
                if path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass
                _start_chain(
                    sid,
                    rebind_limit,
                    5 if sid in refresh_pending else max_candidates,
                    probe_models,
                    transport=transport,
                )
                refresh_pending.discard(sid)
                running[sid] = True
                running_transport[sid] = transport
                active += 1
            last_mode = mode
            previous_running = dict(running)
        except Exception as exc:  # noqa: BLE001
            log(f"supervisor error: {type(exc).__name__}: {exc}")
        time.sleep(poll)


def _reconcile_open_runs(audit, stations: dict[int, dict], running_sids: set[int]) -> int:
    """Close audit runs left open by a supervisor restart.

    If the station is already serving again, the run is a recovery; otherwise
    it is interrupted and the normal need detection will start a new chain.
    A chain that is still alive is left untouched.
    """

    closed = 0
    try:
        runs = audit.open_runs()
    except Exception:
        return 0
    # A supervisor restart can leave more than one open run for a station.
    # Keep only the newest (the one a live chain may own); close the orphans.
    by_station: dict[int, list[dict]] = {}
    for run in runs:
        try:
            sid = int(run.get("station") or 0)
        except (TypeError, ValueError):
            continue
        by_station.setdefault(sid, []).append(run)
    for sid, station_runs in by_station.items():
        station_runs.sort(key=lambda r: float(r.get("started_at") or 0.0))
        for old in station_runs[:-1]:
            try:
                audit.finish_run(old["run_id"], "superseded")
                audit.interrupt_attempts(old["run_id"])
                closed += 1
            except Exception:
                continue
        run = station_runs[-1]
        if sid in running_sids:
            continue
        station = stations.get(sid, {})
        vpn = station.get("vpn") if isinstance(station.get("vpn"), dict) else {}
        host = str(vpn.get("current_host") or "").strip()
        ip = str(station.get("current_ip") or "").strip()
        try:
            if (
                bool(station.get("station_usable"))
                and bool(station.get("model_usable"))
                and ip
            ):
                audit.finish_run(
                    run["run_id"], "recovered", winner_host=host, winner_ip=ip
                )
            else:
                audit.finish_run(run["run_id"], "interrupted")
            audit.interrupt_attempts(run["run_id"])
            closed += 1
        except Exception:
            continue
    return closed


def main() -> None:
    if not _acquire_supervisor_lock():
        log("supervisor already running — exit")
        return
    try:
        try:
            from rotation.infrastructure.db.maintenance import run_once

            _maint = run_once(
                ROOT / "logs" / "registry.db",
                backup_dir=ROOT / "logs" / "backups",
            )
            log(
                "registry maintenance: version=%s integrity=%s backup=%s"
                % (
                    _maint.get("version"),
                    _maint.get("integrity_ok"),
                    _maint.get("backup"),
                )
            )
        except Exception as exc:
            log(f"registry maintenance failed (continuing): {exc}")
        try:
            from free.scan_audit import ScanAudit

            audit = ScanAudit()
            try:
                stale = audit.reconcile_stale()
                data = _status()
                stations = {
                    int(s.get("station") or 0): s
                    for s in data.get("stations", [])
                    if isinstance(s, dict)
                }
                running_sids: set[int] = set()
                for sid in _scope():
                    _transport, _path, pid = _live_lock(sid)
                    if pid and _pid_alive(pid):
                        running_sids.add(sid)
                reconciled = _reconcile_open_runs(audit, stations, running_sids)
            finally:
                audit.close()
            if stale or reconciled:
                log(
                    f"scan audit: {stale} stale run(s) closed, "
                    f"{reconciled} open run(s) reconciled"
                )
        except Exception:
            pass
        _supervise()
    finally:
        _release_supervisor_lock()


if __name__ == "__main__":
    main()
