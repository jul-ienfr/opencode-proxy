"""Proxy control route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers import proxy_routes


class FakeManager:
    def __init__(self, running=False, port=4000):
        self.is_running = running
        self.port = port
        self.calls = []

    def start(self):
        self.calls.append("start")
        self.is_running = True

    def stop(self):
        self.calls.append("stop")
        self.is_running = False

    def restart(self):
        self.calls.append("restart")

    def full_restart(self):
        self.calls.append("full_restart")


def _app(manager=None):
    app = FastAPI()
    proxy_routes.register_proxy_routes(
        app,
        server_manager_getter=lambda: manager,
        default_port=4000,
    )
    return app


def test_status_and_start_stop():
    manager = FakeManager(running=False, port=4100)
    client = TestClient(_app(manager))
    assert client.get("/api/proxy/status").json() == {
        "running": False,
        "port": 4100,
    }
    assert client.post("/api/proxy/start").json()["message"] == "Proxy démarré"
    assert manager.calls == ["start"]
    assert client.post("/api/proxy/start").json()["message"] == "Déjà en cours"
    assert client.post("/api/proxy/stop").json()["message"] == "Proxy arrêté"
    assert manager.calls == ["start", "stop"]
    assert client.post("/api/proxy/stop").json()["message"] == "Déjà arrêté"


def test_status_without_manager_is_ok():
    assert TestClient(_app(None)).get("/api/proxy/status").json() == {
        "running": True,
        "port": 4000,
    }
    assert "Aucun" in TestClient(_app(None)).post("/api/proxy/start").json()["message"]


def test_restart_spawns_thread(monkeypatch):
    manager = FakeManager()
    started = []

    class FakeThread:
        def __init__(self, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            started.append((self.name, self.daemon))
            self.target()

    monkeypatch.setattr(proxy_routes.threading, "Thread", FakeThread)
    client = TestClient(_app(manager))
    assert "déclenché" in client.post("/api/proxy/restart").json()["message"]
    assert manager.calls == ["restart"]
    assert started == [("proxy-restart", False)]
    assert "complet" in client.post(
        "/api/proxy/restart",
        params={"full": True},
    ).json()["message"]
    assert manager.calls == ["restart", "full_restart"]


class HealingManager(FakeManager):
    """Flag périmé à False alors que le serveur écoute (boot > délai start)."""

    def __init__(self):
        super().__init__(running=False, port=4000)
        self.actually_listening = True

    def refresh_state(self):
        if self.actually_listening:
            self.is_running = True
        return self.is_running


def test_status_refreshes_stale_running_flag():
    """[fix 10/11] /api/proxy/status ne doit pas relayer un flag périmé."""
    manager = HealingManager()
    client = TestClient(_app(manager))
    assert client.get("/api/proxy/status").json() == {
        "running": True,
        "port": 4000,
    }
    # Un start sur un état périmé ne doit PAS empiler un second serveur.
    assert client.post("/api/proxy/start").json()["message"] == "Déjà en cours"
    assert manager.calls == []


def test_stop_refreshes_stale_running_flag():
    """Un stop doit s'appliquer même si is_running était resté à False."""
    manager = HealingManager()
    client = TestClient(_app(manager))
    assert client.post("/api/proxy/stop").json()["message"] == "Proxy arrêté"
    assert manager.calls == ["stop"]
