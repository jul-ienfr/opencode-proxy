"""Qualification operator launcher tests."""

from __future__ import annotations

from dashboard import qualification_launcher


def test_launch_shadow_builds_detached_command(monkeypatch, tmp_path):
    calls = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            self.pid = 4321
            calls.append((command, kwargs))

    monkeypatch.setattr(qualification_launcher.subprocess, "Popen", FakePopen)
    payload = qualification_launcher.launch_qualification_epoch(
        tmp_path,
        shadow=True,
        station=3,
        max_hosts=50,
        db_path=tmp_path / "registry.db",
        log_path=tmp_path / "logs" / "operator.log",
    )
    command, kwargs = calls[0]
    assert payload["pid"] == 4321
    assert payload["mode"] == "shadow"
    assert payload["station"] == 3
    assert payload["max_hosts"] == 50
    assert payload["process"].pid == 4321
    assert "--once" in command
    assert "--shadow" in command
    assert "--station" in command
    assert command[command.index("--station") + 1] == "3"
    assert command[command.index("--max") + 1] == "50"
    assert command[command.index("--db") + 1] == str(tmp_path / "registry.db")
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdin"] is qualification_launcher.subprocess.DEVNULL
    assert (tmp_path / "logs" / "operator.log").exists()


def test_launch_live_uses_live_flag(monkeypatch, tmp_path):
    calls = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            self.pid = 1
            calls.append(command)

    monkeypatch.setattr(qualification_launcher.subprocess, "Popen", FakePopen)
    payload = qualification_launcher.launch_qualification_epoch(
        tmp_path,
        shadow=False,
        station=0,
        max_hosts=-5,
    )
    assert payload["mode"] == "live"
    assert payload["station"] == 1
    assert payload["max_hosts"] == 0
    assert "--live" in calls[0]
    assert "--shadow" not in calls[0]
