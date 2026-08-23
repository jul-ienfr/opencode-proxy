"""
Docker event watcher for real-time VPN container status.

Streams `docker events` for container lifecycle events, maps them to the
dual-station gluetun containers, wakes the per-station watchdog (so a
die/stop/health change triggers millisecond-level recovery instead of
the next interval tick) and publishes `vpn_event` on the SSE stream so
the dashboard panel updates in real time.

Fail-open by design: a missing docker CLI, a broken docker socket or a
malformed event line must NEVER break a proxy request — the watcher logs
and degrades (watchdog falls back to its interval pacing, the dashboard
to its 10 s poll).
"""

import asyncio
import json
import logging
import subprocess
import time

logger = logging.getLogger(__name__)


class DockerEventWatcher:
    """Stream docker container events → VPNManager callbacks + SSE vpn_event.

    ``managers`` maps container names (e.g. ``opencode-vpn``) to the
    VPNManager instances owning them — only gluetun stations are watched.
    """

    def __init__(self, managers: dict, *, enabled: bool = True):
        self._managers = dict(managers or {})
        self._enabled = bool(enabled)
        self._stopped = False
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._events_seen = 0
        self._started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Last observed status per watched container ({name: {"status", "time"}})
        self._last_seen: dict[str, dict] = {}

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Start the watcher task. Idempotent; no-op when disabled."""
        if not self._enabled or self._task is not None:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the watcher: cancel the stream task and kill docker events."""
        if self._task is None:
            return
        self._stopped = True
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_managers(self, managers: dict) -> None:
        """[plan 18/08 §4] Atomically swap the watched container map (hot
        reload: upscale/downscale). ``_handle_line`` reads the map per
        event — no reference is retained, so a plain attribute swap is
        safe for the running stream."""
        self._managers = dict(managers or {})

    # ── Watcher loop ───────────────────────────────────────────

    async def _run(self) -> None:
        """Outer loop: spawn ``docker events``, read JSON lines, self-heal
        (re-spawn after a short pause) when the stream ends — a docker
        daemon restart tears the stream down and docker events comes back
        with the daemon."""
        while not self._stopped:
            try:
                proc = await self._spawn()
            except FileNotFoundError:
                logger.warning(
                    "[docker-events] docker CLI not found — "
                    "real-time VPN events disabled (watchdog "
                    "falls back to interval pacing)"
                )
                return
            except Exception as e:
                logger.warning("[docker-events] failed to start watcher: %s", e)
                return
            self._proc = proc
            logger.info("[docker-events] watcher started (docker events)")
            try:
                while not self._stopped:
                    line = await proc.stdout.readline()
                    if not line:
                        break  # stream ended (daemon restart, CLI killed)
                    self._handle_line(line)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[docker-events] read loop ended: %s", e)
            finally:
                self._proc = None
                try:
                    proc.kill()
                except Exception:
                    pass
            # Stream ended — retry after a pause (docker daemon restart etc).
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise

    async def _spawn(self) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            "docker",
            "events",
            "--type",
            "container",
            "--format",
            "{{json .}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            ),
        )

    # ── Event handling ─────────────────────────────────────────

    def _handle_line(self, line: bytes) -> None:
        """Parse one docker events JSON line and dispatch to the station
        it affects (loop context — sync, never blocks)."""
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return  # not a JSON event line — ignore
        actor = event.get("Actor")
        attributes = actor.get("Attributes", {}) if isinstance(actor, dict) else {}
        name = attributes.get("name")
        if not name:
            return
        mgr = self._managers.get(name)
        if mgr is None:
            return  # not a watched gluetun container
        status = event.get("status") or ""
        self._events_seen += 1
        self._last_seen[name] = {
            "status": status,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        # Per-station callback: wakes the watchdog, flags die/stop/kill of
        # a connected container as ERROR immediately (guarded internally).
        try:
            mgr._on_container_event(event)
        except Exception as e:
            logger.debug("[docker-events] station callback failed: %s", e)
        # Real-time dashboard push (coalesced on the SSE side).
        payload = {
            "container": name,
            "station": getattr(mgr, "_station", 1),
            "status": status,
            "time": self._last_seen[name]["time"],
        }
        if status == "health_status" and attributes.get("status"):
            payload["health"] = attributes["status"]
        try:
            from dashboard.events import get_event_manager

            get_event_manager().publish("vpn_event", payload)
        except Exception as e:
            logger.debug("[docker-events] vpn_event publish failed: %s", e)

    def get_status(self) -> dict:
        """Watcher state for the dashboard (enabled, stream alive, cache)."""
        return {
            "enabled": self._enabled,
            "running": self.running,
            "events_seen": self._events_seen,
            "started_at": self._started_at,
            "containers": list(self._managers.keys()),
            "last_seen": dict(self._last_seen),
        }
