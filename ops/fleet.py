"""Contrôleur de flotte : opérations idempotentes par station.

Ce module ne crée pas de manager et ne connaît pas FastAPI : il reçoit la
registry `shared_state.vpn_managers` et applique des opérations. Les endpoints
dashboard et les scripts peuvent l'utiliser sans dupliquer la logique
start/stop/connect/disconnect.

Les opérations restent séquentielles par station (les managers ont leurs
propres verrous) et parallèles entre stations avec un sémaphore borné.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class FleetController:
    def __init__(self, managers: list[Any], pool: Any = None) -> None:
        self._managers = [m for m in (managers or []) if m is not None]
        self._pool = pool

    def _by_sid(self, sid: int) -> Any | None:
        try:
            wanted = int(sid)
        except (TypeError, ValueError):
            return None
        for m in self._managers:
            if int(getattr(m, "_station", -1)) == wanted:
                return m
        return None

    def _sync_pool_primary(self) -> None:
        try:
            if self._pool is not None and self._managers:
                self._pool._vpn = self._managers[0]
        except Exception:
            pass

    async def set_enabled_all(self, enabled: bool) -> dict:
        """Active/désactive la flotte entière, pas seulement la station 1."""
        enabled = bool(enabled)
        for m in self._managers:
            try:
                # [audit 2026-10-30] Never resurrect a station disabled by
                # config/stop_station through the global toggle or heal.
                if enabled and getattr(m, "_disabled", False):
                    continue
                m.enabled = enabled
                m._desired_connected = enabled
            except Exception:
                continue
        if enabled:
            await self.connect_all()
        else:
            await self.disconnect_all(stop_containers=False)
        self._sync_pool_primary()
        return {"ok": True, "enabled": enabled, "stations": len(self._managers)}

    async def connect_all(self, timeout: float | None = None) -> dict:
        sem = asyncio.Semaphore(2)
        errors: list[str] = []

        async def _one(m):
            async with sem:
                try:
                    if getattr(m, "_disabled", False):
                        return
                    m._desired_connected = True
                    if timeout:
                        await asyncio.wait_for(m.connect(), timeout=timeout)
                    else:
                        await m.connect()
                except Exception as e:
                    errors.append(f"s{getattr(m, '_station', '?')}: {e}")

        await asyncio.gather(*(_one(m) for m in self._managers), return_exceptions=True)
        self._sync_pool_primary()
        return {
            "ok": not errors,
            "connected": sum(1 for m in self._managers if str(getattr(m, "status", "")) in ("connected", "degraded")),
            "total": len(self._managers),
            "errors": errors,
        }

    async def disconnect_all(self, stop_containers: bool = False) -> dict:
        errors: list[str] = []

        async def _one(m):
            try:
                m._desired_connected = False
                await m.disconnect()
                if stop_containers and hasattr(m, "stop_container"):
                    await m.stop_container()
            except Exception as e:
                errors.append(f"s{getattr(m, '_station', '?')}: {e}")

        await asyncio.gather(*(_one(m) for m in self._managers), return_exceptions=True)
        return {"ok": not errors, "disconnected": len(self._managers) - len(errors), "errors": errors}

    async def heal(self) -> dict:
        """Reconnecte les stations down, pas seulement les managers manquants."""
        sem = asyncio.Semaphore(2)
        healed: list[int] = []
        errors: list[str] = []

        async def _one(m):
            async with sem:
                if getattr(m, "_disabled", False):
                    return
                status = str(getattr(m, "status", "disconnected") or "disconnected")
                if status in ("connected", "degraded"):
                    return
                try:
                    m._desired_connected = True
                    await m.connect()
                    healed.append(int(getattr(m, "_station", -1)))
                except Exception as e:
                    errors.append(f"s{getattr(m, '_station', '?')}: {e}")

        await asyncio.gather(*(_one(m) for m in self._managers), return_exceptions=True)
        return {"ok": not errors, "healed": healed, "errors": errors}

    async def start_station(self, sid: int) -> dict:
        m = self._by_sid(sid)
        if m is None:
            return {"ok": False, "error": f"station {sid} introuvable"}
        try:
            # Explicit start/connect is the operator's override: clear the
            # disabled flag so the pool can use the station again.
            m._disabled = False
            m.enabled = True
            m._desired_connected = True
            await m.start()
            await m.connect()
            return {"ok": True, "station": int(sid)}
        except Exception as e:
            return {"ok": False, "station": int(sid), "error": str(e)[:200]}

    async def stop_station(self, sid: int) -> dict:
        m = self._by_sid(sid)
        if m is None:
            return {"ok": False, "error": f"station {sid} introuvable"}
        try:
            # [audit 2026-10-30] Exclude the station BEFORE removing its
            # container. The old stop_station left status="connected" and an
            # old IP, and the pool kept picking a container that no longer
            # existed (the manager could not converge because
            # _desired_connected was False).
            m._disabled = True
            m._desired_connected = False
            await m.stop()
            if hasattr(m, "stop_container"):
                await m.stop_container()
            try:
                m._status = "disconnected"
                m._current_ip = None
                m._error = None
            except Exception:
                pass
            return {"ok": True, "station": int(sid)}
        except Exception as e:
            return {"ok": False, "station": int(sid), "error": str(e)[:200]}

    async def restart_station(self, sid: int) -> dict:
        m = self._by_sid(sid)
        if m is None:
            return {"ok": False, "error": f"station {sid} introuvable"}
        try:
            await m.restart()
            return {"ok": True, "station": int(sid)}
        except Exception as e:
            return {"ok": False, "station": int(sid), "error": str(e)[:200]}

    async def connect_station(self, sid: int) -> dict:
        m = self._by_sid(sid)
        if m is None:
            return {"ok": False, "error": f"station {sid} introuvable"}
        try:
            m._disabled = False
            m.enabled = True
            m._desired_connected = True
            await m.connect()
            return {"ok": True, "station": int(sid), "ip": getattr(m, "current_ip", None)}
        except Exception as e:
            return {"ok": False, "station": int(sid), "error": str(e)[:200]}

    async def disconnect_station(self, sid: int) -> dict:
        m = self._by_sid(sid)
        if m is None:
            return {"ok": False, "error": f"station {sid} introuvable"}
        try:
            m._disabled = True
            m._desired_connected = False
            await m.disconnect()
            return {"ok": True, "station": int(sid)}
        except Exception as e:
            return {"ok": False, "station": int(sid), "error": str(e)[:200]}

    def status(self) -> dict:
        return {
            "total": len(self._managers),
            "enabled": sum(1 for m in self._managers if getattr(m, "enabled", False)),
            "desired_connected": sum(1 for m in self._managers if getattr(m, "_desired_connected", True)),
            "stations": [
                {
                    "station": getattr(m, "_station", None),
                    "enabled": bool(getattr(m, "enabled", False)),
                    "desired_connected": bool(getattr(m, "_desired_connected", True)),
                    "status": str(getattr(m, "status", "disconnected")),
                    "ip": getattr(m, "current_ip", None),
                }
                for m in self._managers
            ],
        }


def get_fleet(managers: list[Any], pool: Any = None) -> FleetController:
    return FleetController(managers, pool)


__all__ = ["FleetController", "get_fleet"]
