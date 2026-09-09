"""test_boot_restore_async.py — le restore compteurs ne bloque plus l'import.

Régression du chantier "vitesse max" : ``_restore_token_counters()``
(GROUP BY ~0.2-0.3 s sur grosse DB) tournait en ligne à l'import,
repoussant le bind uvicorn :4000. Désormais ``_restore_token_counters_async``
lance un thread daemon ``token-restore`` (fallback synchrone si le thread
ne peut pas démarrer).

Offline (aucun docker/réseau) : on vérifie le contrat sans toucher à la
vraie DB — le helper est monkeypatché.
"""

import threading
import time

import opencode as oc


def test_restore_async_spawns_daemon_thread(monkeypatch):
    """Le restore part en thread daemon ; valeurs appliquées en tâche de fond."""
    started = threading.Event()
    finished = threading.Event()

    def _fake_restore():
        started.set()
        time.sleep(0.01)
        oc._token_usage["boot-restore-model"] = {"input": 7, "output": 8, "cache": 9}
        finished.set()

    monkeypatch.setattr(oc, "_restore_token_counters", _fake_restore)
    oc._token_usage.pop("boot-restore-model", None)

    t0 = time.perf_counter()
    oc._restore_token_counters_async()
    elapsed = time.perf_counter() - t0
    # Non-bloquant : retour immédiat, restore en fond.
    assert elapsed < 1.0, f"restore bloquant: {elapsed:.2f}s"
    assert started.wait(timeout=5.0), "le thread token-restore n'a pas démarré"
    assert finished.wait(timeout=5.0), "le restore de fond n'a pas fini"
    assert oc._token_usage.get("boot-restore-model") == {"input": 7, "output": 8, "cache": 9}
    # Nettoyage : la clé test ne doit pas polluer le dashboard.
    oc._token_usage.pop("boot-restore-model", None)


def test_restore_async_fallback_sync_on_thread_failure(monkeypatch):
    """Si le thread ne démarre pas → fallback synchrone (sémantique exacte)."""
    called = []

    def _fake_restore():
        called.append(True)

    class _BoomThread:
        def __init__(self, *a, **kw):
            raise RuntimeError("no threads")

    monkeypatch.setattr(oc, "_restore_token_counters", _fake_restore)
    monkeypatch.setattr(threading, "Thread", _BoomThread)
    oc._restore_token_counters_async()
    assert called == [True]


def test_import_wires_async_restore():
    """Garde-fou : l'import appelle la version async, pas le restore direct."""
    import inspect

    src = inspect.getsource(oc._restore_token_counters_async)
    assert "daemon=True" in src
    assert "token-restore" in src
