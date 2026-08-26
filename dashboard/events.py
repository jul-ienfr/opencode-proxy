"""
SSE event manager for real-time dashboard updates.

Thread-safe: publish() can be called from sync or async contexts.

``vpn_event`` is coalesced per subscriber: docker container bursts
(die/start/health_status/stop cascades) collapse to at most one
delivered event per subscriber per 500 ms — the dashboard only needs the
freshest snapshot, and the SSE stream must not be flooded.
"""

import asyncio
import json
import logging
import threading
import time

logger = logging.getLogger(__name__)


class EventManager:
    """Simple pub/sub for SSE events. Thread-safe."""

    # Burst window for vpn_event coalescing (≤ 1 delivered per subscriber).
    _COALESCE_SECONDS = 0.5
    # Flusher cadence — well under the coalesce window so pending events
    # land promptly after their window elapses.
    _FLUSH_INTERVAL = 0.1
    # [P4.4 perf/sécurité] plafond subscribers — /api/events ouvert LAN sans token
    MAX_SUBSCRIBERS = 100

    def __init__(self):
        self._subscribers = []
        self._lock = threading.Lock()
        # Per-subscriber vpn_event coalescing state:
        # _vpn_last[q]    — monotonic time the last vpn_event was DELIVERED.
        # _vpn_pending[q] — (arrival_time, frame) of the latest undelivered
        #                   vpn_event (replaced on each new burst frame).
        self._vpn_last: dict = {}
        self._vpn_pending: dict = {}
        self._flush_task: asyncio.Task | None = None
        self._bound_loop: asyncio.AbstractEventLoop | None = None

    async def subscribe(self):
        queue = asyncio.Queue(maxsize=256)
        with self._lock:
            if len(self._subscribers) >= self.MAX_SUBSCRIBERS:
                raise RuntimeError(f"SSE subscriber limit reached ({self.MAX_SUBSCRIBERS})")
            self._subscribers.append(queue)
            logger.debug("[sse] subscriber added, count=%d", len(self._subscribers))
        return queue

    async def unsubscribe(self, queue):
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass
            self._vpn_last.pop(queue, None)
            self._vpn_pending.pop(queue, None)
            logger.debug("[sse] subscriber removed, count=%d", len(self._subscribers))

    def _deliver(self, queue, payload: str, event: str):
        """Queue one frame for a subscriber (caller holds ``_lock``).

        Never blocks and never drops the subscriber: a full queue ejects
        its oldest buffered event ([31]) — these are ephemeral snapshots,
        so the client just gets the freshest one.
        """
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(payload)
                logger.debug("[sse] slow subscriber: oldest event evicted (event=%s)", event)
            except Exception:
                pass  # closed/destroyed queue

    def publish(self, event: str, data: dict):
        """Thread-safe. Call from sync or async context.

        Uses snapshot pattern: copy subscribers under lock, then iterate
        without holding the lock to avoid blocking subscribe/unsubscribe.
        """
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self._lock:
            subscribers = list(self._subscribers)
            if event == "vpn_event":
                # Coalesce bursts: deliver now only if the window has
                # elapsed for this subscriber, else hold the latest frame
                # pending (a fresh frame replaces the stale one).
                now = time.monotonic()
                for q in subscribers:
                    last = self._vpn_last.get(q)
                    if last is None or now - last >= self._COALESCE_SECONDS:
                        self._deliver(q, payload, event)
                        self._vpn_last[q] = now
                        self._vpn_pending.pop(q, None)
                    else:
                        self._vpn_pending[q] = (now, payload)
                if subscribers:
                    self._ensure_flusher_locked()
                return
            for q in subscribers:
                self._deliver(q, payload, event)

    def _ensure_flusher_locked(self):
        """Start the pending-vpn_event flusher (caller holds ``_lock``).

        Only when a loop is running — otherwise inline delivery on the
        next publish covers it (no loop = no async subscribers either).
        [v10 §14.3.20] appelé depuis un thread sync : create_task échoue →
        on schedule sur la boucle capturée via run_coroutine_threadsafe."""
        if self._flush_task is None or self._flush_task.done():
            try:
                self._flush_task = asyncio.create_task(self._flush_loop())
            except RuntimeError:
                loop = getattr(self, "_bound_loop", None)
                if loop is not None and loop.is_running():
                    asyncio.run_coroutine_threadsafe(self._bind_flusher(), loop)
                self._flush_task = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture la boucle principale au boot — publish() thread-safe."""
        self._bound_loop = loop

    async def _bind_flusher(self):
        with self._lock:
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self):
        """Deliver pending (coalesced) vpn_events once their coalesce
        window elapses — a kill/restart cascade collapses to ≤1 event per
        subscriber per 500 ms, delivered at most 100 ms late."""
        while True:
            await asyncio.sleep(self._FLUSH_INTERVAL)
            now = time.monotonic()
            with self._lock:
                if not self._vpn_pending:
                    continue
                for q, (arrived, frame) in list(self._vpn_pending.items()):
                    if now - arrived >= self._COALESCE_SECONDS:
                        last = self._vpn_last.get(q, 0.0)
                        if now - last >= self._COALESCE_SECONDS:
                            self._deliver(q, frame, "vpn_event")
                            self._vpn_last[q] = now
                        self._vpn_pending.pop(q, None)


_module_manager = EventManager()


def get_event_manager():
    return _module_manager
