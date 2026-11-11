"""Bridge from the fresh-chain repair to the qualification store.

[P0 2026-11-10] The repair chain is the only production path that really
binds OpenVPN UDP/TCP.  Before this bridge, ``transport_health`` only saw
shadow-sweep attempts and stayed ``unproven`` for both OpenVPN transports
even though the chain tested them.  Recording the real attempts keeps the
release evidence truthful without changing the candidate state: the chain
owns its ``claims`` row.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = ROOT / "logs" / "registry.db"

_STORES: dict[str, Any] = {}
_LOCK = threading.Lock()

_STATUS_BY_NAME = {
    "ok": "200",
    "200": "200",
    "429": "429",
    "quota": "429",
    "no_egress": "no_egress",
    "no-egress": "no_egress",
    "bind_error": "bind_error",
    "bind-error": "bind_error",
    "timeout": "timeout",
    "duplicate": "duplicate",
    "auth": "auth",
    "dead": "dead",
}


def _store(db_path: str | Path | None):
    path = str(Path(db_path or DEFAULT_DB).resolve())
    with _LOCK:
        store = _STORES.get(path)
        if store is None:
            from .store import QualificationStore

            store = QualificationStore(path)
            _STORES[path] = store
        return store


def record_chain_attempt(
    *,
    station: int,
    hostname: str,
    transport: str,
    status: str,
    egress_ip: str = "",
    error: str = "",
    latency_ms: float = 0.0,
    retry_after_s: float = 0.0,
    started_at: float = 0.0,
    round_n: int = 0,
    db_path: str | Path | None = None,
) -> str:
    """Record one real ``(hostname, transport)`` attempt from the chain.

    Best-effort by design: a qualification bookkeeping failure must never
    stop a repair.  ``epoch_id`` is scoped to the chain process so a restart
    does not collide with the previous run's attempts.
    """

    try:
        from .domain import Attempt, AttemptStatus

        raw = _STATUS_BY_NAME.get(str(status or "").strip().lower(), "no_egress")
        attempt = Attempt(
            hostname=str(hostname or ""),
            station=int(station or 0),
            transport=str(transport or ""),
            status=AttemptStatus(raw),
            round=int(round_n or 0),
            egress_ip=str(egress_ip or ""),
            error=str(error or "")[:500],
            latency_ms=max(0.0, float(latency_ms or 0.0)),
            retry_after_s=max(0.0, float(retry_after_s or 0.0)),
            started_at=float(started_at or time.time()),
            finished_at=time.time(),
            epoch_id=f"chain-{int(station or 0)}-{os.getpid()}",
            provider="nordvpn",
        )
        return _store(db_path).record_chain_attempt(attempt)
    except Exception:
        return ""


__all__ = ["record_chain_attempt"]
