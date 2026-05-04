"""
SSE event manager for real-time dashboard updates.

Thread-safe: publish() can be called from sync or async contexts.
"""

import json
import asyncio
import threading


class EventManager:
    """Simple pub/sub for SSE events. Thread-safe."""

    def __init__(self):
        self._subscribers = []
        self._lock = threading.Lock()

    async def subscribe(self):
        queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append(queue)
        return queue

    async def unsubscribe(self, queue):
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass

    def publish(self, event: str, data: dict):
        """Thread-safe. Call from sync or async context."""
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self._lock:
            alive = []
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                    alive.append(q)
                except Exception:
                    pass
            self._subscribers = alive


_module_manager = EventManager()


def get_event_manager():
    return _module_manager
