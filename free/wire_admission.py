"""Admission wire — 023 (T007/T025).

Un endpoint ``proven``/``serving`` ne reçoit que le ``wire_fp`` qui l'a prouvé.
Le mode ``compat`` ne cible jamais un bucket prouvé ; le mode ``observe``
journalise sans bloquer (canary).
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

MODE_OBSERVE = "observe"
MODE_CONVERT = "convert"
MODE_COMPAT = "compat"
MODES = frozenset({MODE_OBSERVE, MODE_CONVERT, MODE_COMPAT})

_PROVEN_STATES = frozenset({"proven", "serving"})


@dataclass(frozen=True)
class Admission:
    allowed: bool
    action: str  # route | refuse | observe
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "action": self.action, "reason": self.reason}


def _state_of(candidate: Any) -> str:
    return str(getattr(candidate, "state", "") or "")


def _fp_of(candidate: Any) -> str:
    return str(getattr(candidate, "wire_fp", "") or "")


def admit(
    wire_fp: str,
    candidate: Any,
    *,
    mode: str = MODE_CONVERT,
) -> Admission:
    """Décide si ``candidate`` peut recevoir ``wire_fp``."""
    if mode not in MODES:
        mode = MODE_CONVERT
    state = _state_of(candidate)
    bucket_fp = _fp_of(candidate)
    if mode == MODE_OBSERVE:
        if state in _PROVEN_STATES and bucket_fp and bucket_fp != wire_fp:
            return Admission(True, "observe", "wire_fp mismatch (observe mode)")
        return Admission(True, "observe", "")
    if mode == MODE_COMPAT and state in _PROVEN_STATES:
        return Admission(False, "refuse", "compat cannot target a proven bucket")
    if state in _PROVEN_STATES:
        if not wire_fp:
            return Admission(False, "refuse", "missing wire_fp")
        if bucket_fp != wire_fp:
            return Admission(False, "refuse", "wire_fp mismatch on a proven bucket")
        return Admission(True, "route", "wire_fp matches the proof")
    if state in ("unproven", ""):
        if mode == MODE_COMPAT:
            return Admission(True, "route", "compat targets an unproven bucket")
        return Admission(True, "route", "unproven bucket")
    if state in ("spent", "burned", "mismatch"):
        return Admission(False, "refuse", f"bucket is {state}")
    return Admission(False, "refuse", f"unknown state {state!r}")


def admit_ip(
    wire_fp: str,
    ip: str,
    *,
    mode: str = MODE_CONVERT,
    db_path: str | os.PathLike | None = None,
) -> Admission:
    """[023 T220] Décide si une IP d'egress peut recevoir ``wire_fp``.

    Lit ``registry.db`` via :mod:`free.ip_state` (aucun I/O réseau). Une IP
    sans ligne est « unproven » : le mode ``convert`` l'autorise, ``compat``
    l'autorise aussi (aucune preuve à contredire), ``observe`` ne bloque jamais.
    Une IP multi-provider est jugée sur sa ligne la plus favorable
    (``serving`` > ``proven`` > terminale > ``unproven``), pour ne pas refuser
    une IP prouvée par un provider au motif qu'un autre l'a marquée terminale.

    Fail-soft : tout échec de lecture retombe sur « unproven », donc ne bloque
    jamais un chemin client qui fonctionnait avant cette garde.
    """
    ip_s = str(ip or "").strip()
    if not ip_s:
        return Admission(True, "observe", "no egress ip")
    candidate: Any = None
    try:
        from free.ip_state import IPStateStore

        rows = [st for st in IPStateStore(db_path).list() if str(st.ip).strip() == ip_s]
        if rows:
            rank = {
                "serving": 0,
                "proven": 1,
                "spent": 2,
                "burned": 2,
                "mismatch": 2,
                "unproven": 3,
            }
            candidate = sorted(rows, key=lambda st: rank.get(str(st.state), 4))[0]
    except Exception:
        candidate = None
    if candidate is None:
        class _Unproven:
            state = "unproven"
            wire_fp = ""

        candidate = _Unproven()
    return admit(wire_fp, candidate, mode=mode)


def admit_many(
    wire_fp: str,
    candidates: Iterable[Any],
    *,
    mode: str = MODE_CONVERT,
) -> Any | None:
    for candidate in candidates:
        if admit(wire_fp, candidate, mode=mode).allowed:
            return candidate
    return None
