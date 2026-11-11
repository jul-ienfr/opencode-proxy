"""Constitution XV/XVI — multiplateforme et aucune fenêtre intempestive.

Le service ne doit jamais ouvrir de console Windows, bloquer sur un TTY ou
coder en dur un chemin d'un seul OS. Ces tests rendent la règle exécutable :
l'audit doit être propre aujourd'hui et détecter une régression demain.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SPEC = importlib.util.spec_from_file_location(
    "platform_audit",
    ROOT / "scripts" / "platform_audit.py",
)
assert _SPEC and _SPEC.loader
audit = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = audit
_SPEC.loader.exec_module(audit)


def _write(tmp_path: pathlib.Path, relative: str, content: str) -> pathlib.Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_no_platform_violation_in_the_repository():
    result = audit.audit()
    assert result.findings == [], [f"{f.path}:{f.line} [{f.code}] {f.message}" for f in result.findings]
    assert result.files > 100


def test_process_without_silent_policy_is_reported(tmp_path):
    _write(
        tmp_path,
        "free/bad.py",
        "import subprocess\nsubprocess.run(['docker', 'ps'])\n",
    )
    codes = {finding.code for finding in audit.audit(tmp_path).findings}
    assert "process-visible" in codes


def test_shell_true_is_reported(tmp_path):
    _write(
        tmp_path,
        "free/bad.py",
        "import subprocess\nsubprocess.run('docker ps', shell=True, creationflags=0)\n",
    )
    codes = {finding.code for finding in audit.audit(tmp_path).findings}
    assert "shell-true" in codes


def test_os_system_is_reported(tmp_path):
    _write(tmp_path, "free/bad.py", "import os\nos.system('docker ps')\n")
    codes = {finding.code for finding in audit.audit(tmp_path).findings}
    assert "os-shell" in codes


def test_hardcoded_windows_path_is_reported(tmp_path):
    _write(tmp_path, "vpn/bad.py", 'PATH = r"C:\\Program Files\\Docker"\n')
    codes = {finding.code for finding in audit.audit(tmp_path).findings}
    assert "platform-hardcode" in codes


def test_justified_exception_is_visible_and_allowed(tmp_path):
    _write(
        tmp_path,
        "vpn/bad.py",
        'PATH = r"C:\\Program Files\\Docker"  # platform-audit: allow test\n',
    )
    assert audit.audit(tmp_path).findings == []


def test_docstrings_are_not_flagged(tmp_path):
    _write(
        tmp_path,
        "free/bad.py",
        'r"""Exemple : C:\\Program Files\\Docker."""\n',
    )
    assert audit.audit(tmp_path).findings == []


def test_hidden_kwargs_enforce_no_window_on_windows(monkeypatch):
    from core import process

    monkeypatch.setattr(process, "IS_WINDOWS", True)
    kwargs = process.hidden_kwargs()
    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["stdin"] is process.subprocess.DEVNULL


def test_hidden_kwargs_detach_on_posix(monkeypatch):
    from core import process

    monkeypatch.setattr(process, "IS_WINDOWS", False)
    kwargs = process.hidden_kwargs(detach=True)
    assert "creationflags" not in kwargs
    assert kwargs["start_new_session"] is True


def test_run_hidden_passes_an_argument_list(monkeypatch):
    from core import process

    calls = []

    def _fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return "ok"

    monkeypatch.setattr(process.subprocess, "run", _fake_run)
    assert process.run_hidden(["docker", "ps"], timeout=1) == "ok"
    assert calls[0][0] == ["docker", "ps"]
    assert calls[0][1]["stdin"] is process.subprocess.DEVNULL
    assert "shell" not in calls[0][1]


def test_python_executable_is_not_hardcoded():
    from core import process

    assert process.python_executable() == sys.executable
