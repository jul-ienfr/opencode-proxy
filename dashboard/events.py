"""
SSE event manager for real-time dashboard updates.

Thread-safe: publish() can be called from sync or async contexts.
"""

import json
import asyncio
import logging
import threading

logger = logging.getLogger(__name__)


class EventManager:
    """Simple pub/sub for SSE events. Thread-safe."""

    def __init__(self):
        self._subscribers = []
        self._lock = threading.Lock()

    async def subscribe(self):
        queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers.append(queue)
            logger.debug("[sse] subscriber added, count=%d", len(self._subscribers))
        return queue

    async def unsubscribe(self, queue):
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass
            logger.debug("[sse] subscriber removed, count=%d", len(self._subscribers))

    def publish(self, event: str, data: dict):
        """Thread-safe. Call from sync or async context.

        Uses snapshot pattern: copy subscribers under lock, then iterate
        without holding the lock to avoid blocking subscribe/unsubscribe.
        """
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # [31] Never drop the subscriber (that stalls the dashboard
                # permanently once it stops polling). Evict the oldest buffered
                # event instead — these are ephemeral state snapshots, so the
                # client just gets the freshest one.
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                    logger.debug("[sse] slow subscriber: oldest event evicted (event=%s)", event)
                except Exception:
                    pass  # closed/destroyed queue


_module_manager = EventManager()


def get_event_manager():
    return _module_manager
