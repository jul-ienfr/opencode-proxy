"""[docker-autostart 2026-10-07] relance Docker Desktop depuis le watchdog.

- classification daemon-down vs erreurs VPN (un AUTH_FAILED NordVPN ne
  doit JAMAIS déclencher une relance Docker) ;
- cooldown temporel : 1 relance max / cooldown (pas de latch one-shot) ;
- flag enabled + statut lecture seule pour l'API.
"""

import asyncio
import sys
import time

import pytest

from vpn import manager as vm

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="auto-launch Desktop = Windows only")


@pytest.fixture(autouse=True)
def _reset_state():
    saved = (
        vm._DOCKER_LAST_LAUNCH_AT,
        vm._DOCKER_LAUNCH_IN_PROGRESS,
        vm._DOCKER_DAEMON_REACHABLE,
        vm._DOCKER_DAEMON_CHECKED_AT,
        vm._DOCKER_AUTOSTART_ENABLED,
        vm._DOCKER_AUTOSTART_COOLDOWN_S,
    )
    vm._DOCKER_LAUNCH_IN_PROGRESS = False
    vm._DOCKER_LAST_LAUNCH_AT = 0.0
    vm._DOCKER_DAEMON_REACHABLE = None
    vm._DOCKER_DAEMON_CHECKED_AT = 0.0
    vm.configure_docker_autostart(enabled=True, cooldown_s=600.0)
    yield
    (
        vm._DOCKER_LAST_LAUNCH_AT,
        vm._DOCKER_LAUNCH_IN_PROGRESS,
        vm._DOCKER_DAEMON_REACHABLE,
        vm._DOCKER_DAEMON_CHECKED_AT,
        vm._DOCKER_AUTOSTART_ENABLED,
        vm._DOCKER_AUTOSTART_COOLDOWN_S,
    ) = saved


def test_classify_daemon_down():
    assert vm.is_daemon_unreachable_error(
        RuntimeError(
            "échec docker compose up : failed to connect to the docker API at "
            "npipe:////./pipe/dockerDesktopLinuxEngine; check if the path is "
            "correct and if the daemon is running: open //./pipe/dockerDesktopLinuxEngine: "
            "Le fichier spécifié est introuvable."
        )
    )
    assert vm.is_daemon_unreachable_error(
        RuntimeError(
            "Cannot connect to the Docker daemon at npipe:////./pipe/docker_engine. Is the docker daemon running?"
        )
    )
    assert vm.is_daemon_unreachable_error(
        RuntimeError(
            "error during connect: Get http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.49/containers/json: open //./pipe/dockerDesktopLinuxEngine"
        )
    )
    assert not vm.is_daemon_unreachable_error(None)
    assert not vm.is_daemon_unreachable_error(RuntimeError(""))


def test_classify_never_matches_vpn_errors():
    # AUTH_FAILED NordVPN / conflit compose / générique → pas de relance Docker.
    assert not vm.is_daemon_unreachable_error(RuntimeError("AUTH_FAILED: Your credentials might be wrong"))
    assert not vm.is_daemon_unreachable_error(
        RuntimeError("Conflict. The container name /opencode-vpn is already in use")
    )
    assert not vm.is_daemon_unreachable_error(RuntimeError("boom"))


class _RC:
    def __init__(self, rc=1):
        self.returncode = rc
        self.stdout = ""
        self.stderr = "error during connect"


def _patch_docker_dead(monkeypatch):
    """Simule un daemon mort : `docker ps` échoue, Popen réussit."""
    calls = {"probe": 0, "launch": 0}

    def fake_run(cmd, **kw):
        if list(cmd[:2]) == ["docker", "ps"]:
            calls["probe"] += 1
        elif list(cmd[:1]) in (["cmd"], ["docker"]):
            # Seules voies de lancement Desktop : `cmd /c start` (fallback)
            # ou un éventuel `docker ...` hors sonde. Le bruit de fond
            # (curl d'update-check au import dashboard, etc.) est ignoré.
            calls["launch"] += 1
        return _RC(1)

    def fake_popen(*a, **k):
        calls["launch"] += 1
        return object()

    monkeypatch.setattr(vm.subprocess, "run", fake_run)
    monkeypatch.setattr(vm.subprocess, "Popen", fake_popen)
    return calls


async def test_cooldown_limits_relaunch(monkeypatch):
    calls = _patch_docker_dead(monkeypatch)
    assert await vm.ensure_docker_running(timeout=4) is False
    assert calls["launch"] == 1
    # 2e appel dans le cooldown → attente passive, pas de nouveau lancement.
    assert await vm.ensure_docker_running(timeout=4) is False
    assert calls["launch"] == 1
    # Cooldown expiré → relance à nouveau possible (le bug d'origine :
    # latch one-shot = plus jamais de relance après un quit tardif).
    vm._DOCKER_LAST_LAUNCH_AT = time.monotonic() - 601.0
    assert await vm.ensure_docker_running(timeout=4) is False
    assert calls["launch"] == 2
    assert vm.docker_daemon_status()["reachable"] is False


async def test_request_cooldown_disabled_noop():
    vm._DOCKER_LAST_LAUNCH_AT = time.monotonic()
    assert vm.request_docker_autostart("test") == "cooldown"
    vm._DOCKER_LAST_LAUNCH_AT = 0.0
    vm.configure_docker_autostart(enabled=False)
    assert vm.request_docker_autostart("test") == "disabled"


async def test_request_started_and_worker_clears_flag(monkeypatch):
    _patch_docker_dead(monkeypatch)
    assert vm.request_docker_autostart("test", timeout=4) == "started"
    assert vm.request_docker_autostart("test", timeout=4) == "in_progress"
    await asyncio.sleep(8)  # worker (timeout=4) terminé
    assert vm._DOCKER_LAUNCH_IN_PROGRESS is False
    st = vm.docker_daemon_status()
    assert st["reachable"] is False
    assert st["last_launch_age_s"] is not None


def test_status_shape():
    st = vm.docker_daemon_status()
    assert set(st) == {
        "reachable",
        "checked_at_age_s",
        "autostart_enabled",
        "autostart_in_progress",
        "last_launch_age_s",
        "cooldown_s",
    }
