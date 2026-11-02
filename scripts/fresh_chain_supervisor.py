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
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import yaml_get  # noqa: E402
from free.ip_registry.config import load_registry_config  # noqa: E402

INTERVAL_S = 20.0
HEALTH_GRACE_S = 600.0
LOG = ROOT / "logs" / "fresh_chain_supervisor.log"
LOCK = ROOT / "logs" / "fresh_chain_supervisor.lock"
ALLOCATOR_STATE = ROOT / "logs" / "allocator_state.json"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _recent_429(window_s: float) -> int:
    path = ROOT / "logs" / "catalog_claims.db"
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
    path = ROOT / "logs" / "catalog_claims.db"
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
        tmp = ALLOCATOR_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, ALLOCATOR_STATE)
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


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                timeout=10,
                creationflags=_NO_WINDOW,
            ).stdout
            return str(pid).encode() in out
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
            if pid and _pid_alive(pid):
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


def _lock_state(station: int) -> tuple[Path, int]:
    path = ROOT / "logs" / f"fresh_chain_{station}.lock"
    if not path.exists():
        return path, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return path, int(data.get("pid") or 0)
    except Exception:
        return path, 0


def _winner(station: int) -> dict | None:
    path = ROOT / "logs" / f"fresh_chain_state_{station}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    winner = state.get("winner")
    return winner if isinstance(winner, dict) and winner.get("ip") else None


def _reconcile_registry(stations: dict[int, dict]) -> None:
    """Align ``ip_state`` assignments with the live station egress.

    The registry is the shared source of truth, but a chain can be stopped by
    the supervisor just after the station became healthy. This reconciliation
    releases stale assignments and records a real winner (host + egress) when
    the chain state proves it.
    """

    try:
        from free.ip_registry import SqliteIpRegistry
        from free.ip_registry.config import load_registry_config

        registry = SqliteIpRegistry.from_config(load_registry_config())
    except Exception as exc:  # noqa: BLE001
        log(f"registry reconcile unavailable: {type(exc).__name__}: {exc}")
        return
    try:
        now = time.time()
        for sid, station in stations.items():
            ip = str(station.get("current_ip") or "").strip()
            vpn = station.get("vpn") if isinstance(station.get("vpn"), dict) else {}
            host = str(
                vpn.get("current_host")
                or station.get("current_host")
                or ""
            ).strip()
            assigned = registry.assigned_for_station(sid)
            if assigned is not None and (
                (ip and assigned.egress_ip != ip)
                or not bool(station.get("station_usable"))
            ):
                reason = "live_mismatch" if ip and assigned.egress_ip != ip else "live_unusable"
                registry.release_station(sid, reason, now=now)
                log(
                    f"registry: station {sid} released {assigned.egress_ip} "
                    f"({reason}; live {ip or '-'})"
                )
                assigned = None
            winner = _winner(sid)
            winner_host = str(winner.get("host") or "").strip() if winner else ""
            host = host or winner_host
            winner_ip = str(winner.get("ip") or "") if winner else ""
            functional = bool(
                station.get("station_usable") and station.get("model_usable")
            )
            if (
                ip
                and host
                and functional
                and (winner_ip in ("", ip))
                and (assigned is None or assigned.egress_ip != ip)
            ):
                record = registry.assign_observed(
                    egress_ip=ip,
                    hostname=host,
                    country=str((winner or {}).get("country") or ""),
                    station=sid,
                    tested_at=now,
                )
                if record is not None:
                    log(f"registry: station {sid} keeps live {ip} ({host})")
    finally:
        registry.close()


def _start_chain(
    station: int,
    rebind_limit: int = 4,
    max_candidates: int = 20,
    models: str = "",
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
    if models:
        command.extend(["--models", models])
    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=handle,
        stderr=handle,
        creationflags=_NO_WINDOW,
    )
    log(f"station {station} chain started pid={proc.pid} (slice {max_candidates})")


def _status() -> dict:
    try:
        return requests.get("http://127.0.0.1:4000/api/vpn-status", timeout=20).json()
    except Exception:
        return {}


def _need_rank(station: dict) -> int:
    """Repair priority: model-exhausted tunnel-up first, then connected, then down."""

    if station.get("station_usable") and not station.get("model_usable"):
        return 0
    if str(station.get("vpn_status") or "") == "connected":
        return 1
    return 2


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
        try:
            repairing_s = float(health.get("repairing_s") or 0.0)
        except (TypeError, ValueError):
            repairing_s = 0.0
        if repairing_s < HEALTH_GRACE_S:
            return False
    if not str(station.get("current_ip") or "").strip():
        return True
    if station.get("station_usable") and station.get("model_usable"):
        return False
    reason = str(station.get("model_usable_reason") or "")
    nonrout = str(station.get("non_routable_reason") or "")
    vpn_status = str(station.get("vpn_status") or "")
    if reason in (
        "daily-bucket-exhausted",
        "bad-ip",
        "station-unusable",
        "quota",
        "quota-429",
        "tunnel-down",
    ):
        return True
    if nonrout.startswith(
        ("quota=", "bad_until", "egress=", "status=", "health=", "auth=", "no-ip")
    ):
        return True
    if vpn_status in ("error", "disconnected", "connecting", "stopped"):
        return True
    if not str(station.get("current_ip") or "").strip():
        return True
    # model_usable can be false for a transient model cooldown: rotating the
    # IP would not help and would burn quota.
    return not bool(station.get("station_usable", False))


def _exhaustion_due(zero_polls: int, total_429: int, threshold: int) -> bool:
    """Require a sustained zero-usable fleet before pausing the catalog burn.

    A single poll can catch a transient 200 between two 429s; the old
    immediate check never fired on a flapping fleet. Three consecutive polls
    (~60 s at the default interval) are enough to distinguish a real outage
    from a one-tick recovery.
    """
    return zero_polls >= 3 and total_429 >= max(1, int(threshold))


def _stop_chain(station: int) -> None:
    path, pid = _lock_state(station)
    if not pid or not _pid_alive(pid):
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
        log(f"station {station} chain stopped (station healthy)")
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
    log("supervisor started (all stations, parallel chains)")
    last_winner: dict[int, str] = {}
    winner_at: dict[int, float] = {}
    last_failed_at: dict[int, float] = {}
    healthy_since: dict[int, float] = {}
    previous_running: dict[int, bool] = {}
    last_mode: str | None = None
    paused_until = 0.0
    zero_functional_polls = 0
    while True:
        poll = INTERVAL_S
        try:
            cfg = load_registry_config()
            alloc = cfg.get("allocator") if isinstance(cfg.get("allocator"), dict) else {}
            mode = str(alloc.get("mode") or "off")
            poll = max(5.0, float(alloc.get("poll_interval_s", INTERVAL_S) or INTERVAL_S))
            if mode == "off":
                if last_mode != "off":
                    log("allocator mode=off — supervisor idle")
                last_mode = "off"
                time.sleep(poll)
                continue
            max_parallel = max(1, int(alloc.get("max_parallel_repairs", 6) or 6))
            max_candidates = max(
                1, int(alloc.get("max_candidates_per_chain", 20) or 20)
            )
            probe_models = ",".join(
                str(item).strip()
                for item in (alloc.get("probe_models") or [])
                if str(item).strip()
            )
            rebind_limit = max(
                1, int(alloc.get("rebind_limit_per_min", 4) or 4)
            )
            cooldown = max(0.0, float(alloc.get("winner_cooldown_s", 120) or 120))
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
            scope = _scope()
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
            stations = {
                int(s.get("station") or 0): s for s in data.get("stations", [])
            }
            _reconcile_registry(stations)
            functional = any(
                bool(station.get("model_usable"))
                for station in stations.values()
            )
            if functional:
                zero_functional_polls = 0
            else:
                zero_functional_polls += 1
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

            def _rank(sid: int) -> int:
                return _need_rank(stations.get(sid, {}))

            broken.sort(key=lambda sid: (_rank(sid), sid))
            running: dict[int, bool] = {}
            for sid in scope:
                _path, pid = _lock_state(sid)
                running[sid] = bool(pid and _pid_alive(pid))
            for sid in scope:
                # A chain that ended while the station is still broken did not
                # find a winner: back off before relaunching (catalog exhaustion
                # must not become a hot loop).
                if (
                    previous_running.get(sid)
                    and not running.get(sid)
                    and sid in broken
                    and not _winner(sid)
                ):
                    last_failed_at[sid] = time.time()
                    log(f"station {sid} chain ended without a winner — backoff")

            # A station that recovered must not keep a chain. Give the chain a
            # short grace period to finish its probe and persist the winner;
            # killing it immediately lost the hostname attribution.
            for sid in scope:
                if sid not in broken and running.get(sid):
                    if _winner(sid):
                        _stop_chain(sid)
                        running[sid] = False
                        healthy_since.pop(sid, None)
                        continue
                    since = healthy_since.setdefault(sid, time.time())
                    if time.time() - since >= winner_grace:
                        _stop_chain(sid)
                        running[sid] = False
                        healthy_since.pop(sid, None)
                elif sid in broken:
                    healthy_since.pop(sid, None)

            # Persist the winner once, give the proxy time to publish it, then
            # only relaunch if the station is still broken after the cooldown.
            for sid in list(broken):
                winner = _winner(sid)
                if not winner:
                    continue
                signature = f"{winner.get('host')}|{winner.get('ip')}"
                if last_winner.get(sid) != signature:
                    _persist_pin(sid, winner)
                    last_winner[sid] = signature
                    winner_at[sid] = time.time()
                if running.get(sid):
                    _stop_chain(sid)
                    running[sid] = False
                elapsed = time.time() - winner_at.get(sid, 0.0)
                if elapsed < cooldown:
                    broken.remove(sid)
                else:
                    log(
                        f"station {sid} still broken {int(elapsed)}s after winner "
                        f"{signature} — new chain"
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
                failed_ago = time.time() - last_failed_at.get(sid, 0.0)
                if last_failed_at.get(sid) and failed_ago < failure_cooldown:
                    continue
                if active >= max_parallel:
                    break
                path, _pid = _lock_state(sid)
                if path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass
                _start_chain(sid, rebind_limit, max_candidates, probe_models)
                running[sid] = True
                active += 1
            last_mode = mode
            previous_running = dict(running)
        except Exception as exc:  # noqa: BLE001
            log(f"supervisor error: {type(exc).__name__}: {exc}")
        time.sleep(poll)


def main() -> None:
    if not _acquire_supervisor_lock():
        log("supervisor already running — exit")
        return
    try:
        _supervise()
    finally:
        _release_supervisor_lock()


if __name__ == "__main__":
    main()
