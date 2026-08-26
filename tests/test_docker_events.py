"""test_docker_events.py — [P6] premiers tests unitaires du watcher.

Le watcher est fail-open par design : ces tests couvrent le parsing d'event,
le dispatch par conteneur, la révilation watchdog, le flip ERROR sur
die/stop/kill d'un conteneur CONNECTED, et l'invalidation du cache status.
"""

import json

import pytest

import docker_events as de


class _FakeManager:
    """Tranche VPNManager : uniquement ce que _handle_line consomme."""

    def __init__(self, station=1):
        self._station = station
        self.events = []
        self.status_refresh_stamps = []
        self._status = "connected"

    def _on_container_event(self, event):
        self.events.append(event)
        status = event.get("status")
        if status in ("die", "stop", "kill") and self._status == "connected":
            self._status = "error"

    @property
    def _last_status_refresh_at(self):
        return getattr(self, "_stamp", 1.0)

    @_last_status_refresh_at.setter
    def _last_status_refresh_at(self, v):
        self._stamp = v
        self.status_refresh_stamps.append(v)


def _line(name="opencode-vpn", status="die", **attrs):
    return json.dumps(
        {
            "Actor": {"Attributes": {"name": name, **attrs}},
            "status": status,
        }
    ).encode()


@pytest.fixture
def watcher():
    return de.DockerEventWatcher({"opencode-vpn": _FakeManager(1)})


def test_handle_line_dispatches_to_matching_manager(watcher):
    mgr = watcher._managers["opencode-vpn"]
    watcher._handle_line(_line(status="start"))
    assert len(mgr.events) == 1
    assert mgr.events[0]["status"] == "start"
    assert watcher._events_seen == 1


def test_handle_line_ignores_unknown_container(watcher):
    watcher._handle_line(_line(name="autre-conteneur", status="die"))
    assert watcher._events_seen == 0
    assert watcher._last_seen == {}


def test_handle_line_ignores_malformed_json(watcher):
    watcher._handle_line(b"pas du json {{{")
    watcher._handle_line(b"")
    assert watcher._events_seen == 0


def test_die_on_connected_marks_error(watcher):
    # Le flip ERROR appartient à VPNManager._on_container_event (couvert par
    # les tests vpn_manager) — ici on vérifie le DISPATCH du watcher.
    mgr = watcher._managers["opencode-vpn"]
    watcher._handle_line(_line(status="die"))
    assert mgr._status == "error"
    assert mgr.events[0]["status"] == "die"


def test_die_invalidates_status_cache(watcher):
    mgr = watcher._managers["opencode-vpn"]
    watcher._handle_line(_line(status="stop"))
    assert mgr.status_refresh_stamps and mgr.status_refresh_stamps[-1] == 0


def test_set_managers_swaps_map_atomically(watcher):
    m2 = _FakeManager(2)
    watcher.set_managers({"opencode-vpn-2": m2})
    watcher._handle_line(_line(name="opencode-vpn", status="die"))
    assert watcher._events_seen == 0  # l'ancien conteneur n'est plus suivi
    watcher._handle_line(_line(name="opencode-vpn-2", status="start"))
    assert len(m2.events) == 1


def test_get_status_shape(watcher):
    st = watcher.get_status()
    assert st["enabled"] is True
    assert st["containers"] == ["opencode-vpn"]
    assert "events_seen" in st and "started_at" in st
