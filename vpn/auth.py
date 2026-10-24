"""vpn.auth — throttle AUTH NordVPN (extrait de vpn.manager).

[audit-2026-10-24 Phase4] Première découpe du god object (8559 lignes) :
la sérialisation des connexions OpenVPN (rate-limit NordVPN) vit ici,
testable isolément, sans dépendance docker/réseau.

Contrat :
- 2 connexions simultanées max (slot global) ;
- 12s min entre 2 AUTH de stations différentes, 30s même station ;
- cooldown global 120s après 3 AUTH_FAILED / 300s ;
- bypass total sous PYTEST_CURRENT_TEST (tests sans attente).

``vpn.manager`` ré-exporte les noms historiques (``_auth_gate``,
``_auth_record_failure``, ...) pour compatibilité (tests + shims).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_AUTH_THROTTLE_LOCK = threading.Lock()
_AUTH_FAIL_TIMES: list[float] = []
_AUTH_GLOBAL_COOLDOWN_UNTIL = 0.0
_AUTH_LAST_CONNECT_AT = 0.0
_AUTH_IN_FLIGHT = 0
_AUTH_MIN_INTERVAL_GLOBAL_S = 12.0
_AUTH_MIN_INTERVAL_SAME_STATION_S = 30.0
_AUTH_LAST_BY_STATION: dict[str, float] = {}


def _auth_record_failure() -> None:
    global _AUTH_GLOBAL_COOLDOWN_UNTIL
    now = time.monotonic()
    armed = False
    with _AUTH_THROTTLE_LOCK:
        _AUTH_FAIL_TIMES.append(now)
        _AUTH_FAIL_TIMES[:] = [t for t in _AUTH_FAIL_TIMES if now - t <= 300.0]
        if len(_AUTH_FAIL_TIMES) >= 3:
            _AUTH_GLOBAL_COOLDOWN_UNTIL = now + 120.0
            _AUTH_FAIL_TIMES.clear()
            armed = True
    if armed:
        logger.warning("[vpn] AUTH_FAILED x3 — cooldown global 120s (plan 05/09 §1.1)")


def _auth_cooldown_remaining() -> float:
    now = time.monotonic()
    with _AUTH_THROTTLE_LOCK:
        return max(0.0, _AUTH_GLOBAL_COOLDOWN_UNTIL - now)


def _auth_connect_done() -> None:
    global _AUTH_IN_FLIGHT
    with _AUTH_THROTTLE_LOCK:
        _AUTH_IN_FLIGHT = max(0, _AUTH_IN_FLIGHT - 1)


async def _auth_gate(station: str | None = None) -> None:
    """Throttle AUTH NordVPN : 2 connexions simultanées max + cooldowns."""
    global _AUTH_LAST_CONNECT_AT, _AUTH_IN_FLIGHT
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    while True:
        wait = _auth_cooldown_remaining()
        if wait > 0:
            await asyncio.sleep(min(wait, 5.0))
            continue
        sleep_for = 0.0
        with _AUTH_THROTTLE_LOCK:
            if _AUTH_IN_FLIGHT >= 2:
                sleep_for = 2.0
            else:
                now = time.monotonic()
                delta_global = now - _AUTH_LAST_CONNECT_AT
                if delta_global < _AUTH_MIN_INTERVAL_GLOBAL_S:
                    sleep_for = _AUTH_MIN_INTERVAL_GLOBAL_S - delta_global
                elif station is not None:
                    last_same = _AUTH_LAST_BY_STATION.get(str(station), 0.0)
                    if now - last_same < _AUTH_MIN_INTERVAL_SAME_STATION_S:
                        sleep_for = _AUTH_MIN_INTERVAL_SAME_STATION_S - (now - last_same)
                    else:
                        _AUTH_LAST_CONNECT_AT = now
                        _AUTH_LAST_BY_STATION[str(station)] = now
                        _AUTH_IN_FLIGHT += 1
                        return
                else:
                    _AUTH_LAST_CONNECT_AT = now
                    _AUTH_IN_FLIGHT += 1
                    return
        await asyncio.sleep(sleep_for)


def _auth_reset_for_tests() -> None:
    """Remet le throttle à zéro (tests unitaires auth uniquement)."""
    global _AUTH_GLOBAL_COOLDOWN_UNTIL, _AUTH_LAST_CONNECT_AT, _AUTH_IN_FLIGHT
    with _AUTH_THROTTLE_LOCK:
        _AUTH_FAIL_TIMES.clear()
        _AUTH_GLOBAL_COOLDOWN_UNTIL = 0.0
        _AUTH_LAST_CONNECT_AT = 0.0
        _AUTH_IN_FLIGHT = 0
        _AUTH_LAST_BY_STATION.clear()


__all__ = [
    "_AUTH_THROTTLE_LOCK",
    "_auth_gate",
    "_auth_record_failure",
    "_auth_cooldown_remaining",
    "_auth_connect_done",
    "_auth_reset_for_tests",
]
