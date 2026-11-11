"""Container isolation for the public egress worker (command surface only)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import free.public_egress_container as container


def _fake_run_factory(calls: list[list[str]]):
    def _run(command, **kwargs):
        calls.append(list(command))
        if "inspect" in command:
            return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        if "rm" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="cid-123\n", stderr="")

    return _run


def test_container_command_is_locked_down(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(container.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(container, "run_hidden", _fake_run_factory(calls))
    monkeypatch.setattr(container, "_wait_port", lambda port, timeout: True)

    handle = container.start_worker_container(
        {"p1": "https://37.203.35.42:443"},
        token="local-token",
        host_port=12345,
    )
    try:
        command = calls[0]
        joined = " ".join(command)
        assert command[0] == "/usr/bin/docker"
        assert "run" in command
        assert "--read-only" in command
        assert "--label" in command
        assert "oc.public-egress=1" in command
        assert "oc.managed=1" in command
        assert "--cap-drop=ALL" in command
        assert "--security-opt=no-new-privileges" in command
        assert "--user=65534:65534" in command
        assert "--pids-limit=128" in command
        assert "--memory=256m" in command
        assert "--cpus=0.5" in command
        assert "127.0.0.1:12345:8000" in command
        assert "python" in command
        assert "-I" in command
        assert "-B" in command
        assert not any(arg in ("-e", "--env") for arg in command)
        assert str(Path.cwd() / ".env") not in joined
        assert handle.port == 12345
        assert handle.local_proxy_url("p1") == "http://p1:local-token@127.0.0.1:12345"
    finally:
        handle.stop()

    assert any("rm" in command for command in calls)
    assert not handle.config_dir.exists()


def test_container_start_failure_is_clean(monkeypatch):
    monkeypatch.setattr(container.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        container,
        "run_hidden",
        lambda command, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(RuntimeError, match="docker run"):
        container.start_worker_container({"p1": "https://1.2.3.4:443"}, token="t")


def test_container_requires_docker(monkeypatch):
    monkeypatch.setattr(container.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="docker introuvable"):
        container.start_worker_container({"p1": "https://1.2.3.4:443"}, token="t")


def test_container_requires_upstreams():
    with pytest.raises(ValueError):
        container.start_worker_container({}, token="t")


def test_cleanup_removes_only_orphan_containers(monkeypatch):
    calls: list[list[str]] = []

    def _run(command, **kwargs):
        calls.append(list(command))
        if "ps" in command:
            return SimpleNamespace(returncode=0, stdout="cid-1\ncid-2\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(container.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(container, "run_hidden", _run)
    removed = container.cleanup_orphan_workers(keep={"cid-2"})
    assert removed == 1
    assert ["/usr/bin/docker", "rm", "-f", "cid-1"] in calls
    assert ["/usr/bin/docker", "rm", "-f", "cid-2"] not in calls
