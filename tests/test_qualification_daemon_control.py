"""Command contract for the supervisor-launched qualification daemon."""

from __future__ import annotations

from types import SimpleNamespace

from free.qualification import supervisor_bridge
from free.qualification.daemon_control import daemon_once_command


def test_qualification_tick_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(supervisor_bridge, "yaml_get", lambda *args, **kwargs: False)
    called = []
    monkeypatch.setattr(
        supervisor_bridge.subprocess,
        "run",
        lambda *args, **kwargs: called.append(args),
    )
    assert supervisor_bridge.qualification_tick(
        ".",
        last_at=0.0,
        now=1000.0,
        log=lambda message: None,
    ) == 0.0
    assert called == []


def test_qualification_tick_runs_when_due(monkeypatch):
    values = {
        "daemon_enabled": True,
        "daemon_interval_s": 60,
        "daemon_max_hosts": 1,
    }
    monkeypatch.setattr(
        supervisor_bridge,
        "yaml_get",
        lambda section, key, default=None: values.get(key.rsplit(".", 1)[-1], default),
    )
    called = []

    def fake_run(*args, **kwargs):
        called.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(supervisor_bridge.subprocess, "run", fake_run)
    assert supervisor_bridge.qualification_tick(
        ".",
        last_at=0.0,
        now=1000.0,
        log=lambda message: None,
    ) == 1000.0
    assert len(called) == 1
    command = called[0][0][0]
    assert "--max" in command and command[command.index("--max") + 1] == "1"


def test_daemon_once_command_shape(tmp_path):
    command = daemon_once_command(tmp_path)
    assert command[1].endswith("qualification_daemon.py")
    assert "--once" in command
    assert "--shadow" in command
    assert "--watchdog" in command
    assert "http://127.0.0.1:4000" in command


def test_daemon_once_command_can_bound_the_sweep(tmp_path):
    command = daemon_once_command(tmp_path, max_hosts=1)
    assert "--max" in command
    assert command[command.index("--max") + 1] == "1"
    # 0 = pas de borne (sweep complet historique)
    assert "--max" not in daemon_once_command(tmp_path, max_hosts=0)


def test_daemon_live_command_carries_paths(tmp_path):
    command = daemon_once_command(
        tmp_path,
        shadow=False,
        config=tmp_path / "config.yaml",
        db=tmp_path / "registry.db",
    )
    assert "--live" in command
    assert str(tmp_path / "config.yaml") in command
    assert str(tmp_path / "registry.db") in command

def test_active_qualifiers_force_the_supervisor_tick_to_shadow(monkeypatch):
    values = {
        "qualification.qualifiers": {"enabled": True, "count": 2},
        "qualification.daemon_shadow": False,
        "qualification.shadow_mode": False,
    }
    monkeypatch.setattr(
        supervisor_bridge,
        "yaml_get",
        lambda section, key, default=None: values.get(key, default),
    )
    assert supervisor_bridge._qualified_shadow_mode() is True


def test_daemon_shadow_wins_when_qualifiers_are_idle(monkeypatch):
    values = {
        "qualification.qualifiers": {"enabled": False, "count": 2},
        "qualification.daemon_shadow": True,
        "qualification.shadow_mode": False,
    }
    monkeypatch.setattr(
        supervisor_bridge,
        "yaml_get",
        lambda section, key, default=None: values.get(key, default),
    )
    assert supervisor_bridge._qualified_shadow_mode() is True


def test_shadow_mode_is_the_fallback_when_daemon_shadow_is_absent(monkeypatch):
    values = {
        "qualification.qualifiers": {"enabled": False, "count": 0},
        "qualification.daemon_shadow": None,
        "qualification.shadow_mode": False,
    }
    monkeypatch.setattr(
        supervisor_bridge,
        "yaml_get",
        lambda section, key, default=None: values.get(key, default),
    )
    assert supervisor_bridge._qualified_shadow_mode() is False
