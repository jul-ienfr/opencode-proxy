"""Pure guards for the fresh-chain repair process.

Extracted from ``scripts/fresh_chain.py`` so the legacy monolith does not grow
and the rules are testable without Docker or a station.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a lock owner."""

    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=0x08000000,
            )
            return str(pid) in (result.stdout or "")
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def filter_candidates_for_transport(
    candidates: list[dict[str, Any]], transport: str
) -> list[dict[str, Any]]:
    """Drop hosts that do not advertise the requested transport.

    The candidate cache is shared across transports.  Rows without transport
    metadata (legacy manifests) stay eligible; when nothing matches, the
    original list is kept so the caller can surface the real bind failure.
    """

    if str(transport) == "auto":
        return list(candidates)
    wanted = str(transport)
    supported = [
        row
        for row in candidates
        if wanted in (row.get("transports") or [wanted])
    ]
    return supported or list(candidates)


def acquire_lock(
    path: Path,
    *,
    pid_alive_fn: Callable[[int], bool] = pid_alive,
    now: Callable[[], float] = time.time,
) -> bool:
    """One chain per lock path; a dead owner releases the lock."""

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
            age = now() - float(data.get("ts") or 0.0)
            if pid_alive_fn(pid) and age < 3600.0:
                return False
        except Exception:
            pass
    path.write_text(
        json.dumps({"pid": os.getpid(), "ts": now()}),
        encoding="utf-8",
    )
    return True


def release_lock(path: Path, *, pid: int | None = None) -> None:
    """Delete the lock only when it belongs to this process."""

    owner = os.getpid() if pid is None else int(pid)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if int(data.get("pid") or 0) != owner:
            return
    except Exception:
        pass
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def exhausted_egress(
    row: dict[str, Any], exhausted: set[str]
) -> str:
    """Return the row's last egress when the ledger already burned it.

    A hostname can be re-linked to the same NAT IP; binding it again just
    spends the rebind budget and reaches the same 429.  An empty result means
    the candidate is still worth a bind (no known IP, or IP not exhausted).
    """

    ip = str(row.get("ip") or "").strip()
    return ip if ip and ip in set(exhausted or ()) else ""


def transport_parts(transport: str) -> tuple[str, str | None]:
    """Map a chain transport name to Gluetun ``VPN_TYPE`` + OVPN protocol."""

    value = str(transport or "wireguard").strip().lower()
    if value == "openvpn-tcp":
        return "openvpn", "tcp"
    if value == "openvpn-udp":
        return "openvpn", "udp"
    return "wireguard", None


__all__ = [
    "acquire_lock",
    "exhausted_egress",
    "filter_candidates_for_transport",
    "pid_alive",
    "release_lock",
    "transport_parts",
]
