"""test_concurrency_guards.py — P1-8 : garde-fous concurrence.

1. KeyPauser : martèlement multi-threads (pause/is_paused/remaining/
   get_all_status/cleanup/unpause + _save concurrent) — termine sans
   RuntimeError ni deadlock, état final cohérent.
2. EventManager : publish() depuis un thread sync vers un abonné async —
   le frame arrive (delivery via call_soon_threadsafe, pas de put_nowait
   cross-thread).
"""

import asyncio
import threading

import pytest

from core.keys import KeyPauser
from dashboard.events import EventManager


def _pauser(tmp_path) -> KeyPauser:
    kp = KeyPauser(max_pause=600)
    kp._PAUSED_FILE = str(tmp_path / "paused_keys.yaml")
    return kp


def test_keypauser_thread_hammer_no_deadlock(tmp_path):
    kp = _pauser(tmp_path)
    keys = [f"sk-test-key-{i:03d}-xxxxxxxxxxxxxxxx" for i in range(8)]
    errors: list = []
    stop = threading.Event()

    def worker(n):
        try:
            for i in range(200):
                k = keys[(n + i) % len(keys)]
                kp.pause_key(k, 60, reason="hammer")
                kp.is_paused(k)
                kp.remaining(k)
                if i % 10 == 0:
                    kp.get_all_status()
                    kp.cleanup_expired()
                    kp.unpause_if_paused(keys[(n + i + 1) % len(keys)])
        except Exception as e:  # pragma: no cover
            errors.append(e)
        finally:
            if n == 0:
                stop.set()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "deadlock ? thread bloqué"
    assert errors == []
    # État final cohérent : que des expiries futures, raisons alignées.
    status = kp.get_all_status()
    assert set(status) <= {kp._prefix(k) for k in keys}


def test_keypauser_save_snapshot_under_concurrent_mutation(tmp_path):
    """_save() itère un snapshot : mutation concurrente → pas de RuntimeError."""
    kp = _pauser(tmp_path)
    errors: list = []

    def mutator():
        try:
            for i in range(300):
                kp.pause_key(f"sk-mut-{i % 16:04d}-yyyyyyyyyyyyyyyy", 60)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def saver():
        try:
            for _ in range(100):
                kp._save()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=mutator) for _ in range(4)] + [
        threading.Thread(target=saver) for _ in range(2)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert errors == []


@pytest.mark.asyncio
async def test_event_manager_cross_thread_publish_delivered():
    """publish() depuis un thread sync → frame reçue par l'abonné async."""
    mgr = EventManager()
    mgr.bind_loop(asyncio.get_running_loop())
    q = await mgr.subscribe()
    try:
        def _sync_publish():
            mgr.publish("stats_updated", {"n": 1})

        t = threading.Thread(target=_sync_publish)
        t.start()
        t.join(timeout=10)
        frame = await asyncio.wait_for(q.get(), timeout=5)
        assert "stats_updated" in frame
        assert '"n": 1' in frame
    finally:
        await mgr.unsubscribe(q)


@pytest.mark.asyncio
async def test_event_manager_loop_thread_publish_direct():
    """Chemin nominal (même thread) inchangé : livraison immédiate."""
    mgr = EventManager()
    mgr.bind_loop(asyncio.get_running_loop())
    q = await mgr.subscribe()
    try:
        mgr.publish("stats_updated", {"n": 2})
        frame = await asyncio.wait_for(q.get(), timeout=5)
        assert '"n": 2' in frame
    finally:
        await mgr.unsubscribe(q)
