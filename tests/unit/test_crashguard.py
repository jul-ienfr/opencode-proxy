"""[P3 2026-11-21 — T307] `observability/crashguard.py` : trace de crash."""

from __future__ import annotations

import threading

from observability import crashguard


def test_crash_log_path_is_under_logs():
    path = crashguard.crash_log_path("/tmp/project")
    assert path.replace("\\", "/").endswith("/logs/app-crash.log")


def test_write_appends_a_timestamped_traceback(tmp_path):
    target = tmp_path / "app-crash.log"
    try:
        raise ValueError("boum")
    except ValueError:
        exc_type, exc, tb = __import__("sys").exc_info()
        crashguard._write("uncaught", exc_type, exc, tb, str(target))
    text = target.read_text(encoding="utf-8")
    assert "uncaught: ValueError: boum" in text
    assert "Traceback (most recent call last)" in text


def test_install_sets_hooks_and_returns_the_log_path(tmp_path, monkeypatch):
    monkeypatch.setattr(crashguard.faulthandler, "enable", lambda: None)
    monkeypatch.setattr(crashguard.sys, "excepthook", lambda *a: None)
    monkeypatch.setattr(threading, "excepthook", lambda *a: None)
    path = crashguard.install(str(tmp_path))
    assert path.endswith("app-crash.log")
    assert crashguard.sys.excepthook is not None
    assert threading.excepthook is not None


def test_write_never_raises_on_an_unwritable_path(tmp_path):
    # Un chemin dont le parent est un fichier ne doit pas lever : le hook de
    # crash ne peut pas être la cause d'un second crash.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    crashguard._write("uncaught", None, None, None, str(blocker / "x.log"))
