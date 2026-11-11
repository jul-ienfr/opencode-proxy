"""Portable, silent process launching.

Every non-interactive process started by the service (dashboard, supervisor,
scheduler, worker, probe, rotation, migration) must go through this module:

- Windows: ``CREATE_NO_WINDOW`` so no ``cmd.exe``/console window flashes or
  steals focus;
- POSIX: ``start_new_session=True`` for detached children, ``stdin=DEVNULL``
  so a child can never block on an interactive prompt;
- arguments are always a sequence (no ``shell=True``).

Interactive operator CLIs are allowed to use plain ``subprocess`` directly;
they are listed in ``scripts/platform_audit.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any

IS_WINDOWS = sys.platform == "win32"

# ``subprocess.CREATE_NO_WINDOW`` exists on Windows only; keep the numeric
# fallback so type-checkers and non-Windows imports stay happy.
CREATE_NO_WINDOW = (
    int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)) if IS_WINDOWS else 0
)


def hidden_kwargs(
    *,
    detach: bool = False,
    stdin: Any = subprocess.DEVNULL,
) -> dict[str, Any]:
    """Return the platform kwargs that keep a child process invisible.

    ``detach`` requests a new session on POSIX; on Windows the process is
    already detached from the parent console by ``CREATE_NO_WINDOW``.
    """

    kwargs: dict[str, Any] = {"stdin": stdin}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    elif detach:
        kwargs["start_new_session"] = True
    return kwargs


def run_hidden(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    capture_output: bool = True,
    text: bool = True,
    check: bool = False,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    detach: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a non-interactive command without opening a window or a TTY."""

    options = hidden_kwargs(detach=detach)
    options.update(
        {
            "timeout": timeout,
            "capture_output": capture_output,
            "text": text,
            "check": check,
        }
    )
    if cwd is not None:
        options["cwd"] = cwd
    if env is not None:
        options["env"] = dict(env)
    options.update(kwargs)
    return subprocess.run(list(args), **options)


def popen_hidden(
    args: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    detach: bool = False,
    **kwargs: Any,
) -> subprocess.Popen[str]:
    """Start a background process with the same visibility policy."""

    options = hidden_kwargs(detach=detach)
    if cwd is not None:
        options["cwd"] = cwd
    if env is not None:
        options["env"] = dict(env)
    options.update(kwargs)
    return subprocess.Popen(list(args), **options)


def python_executable() -> str:
    """The interpreter running the service, never a hardcoded ``python.exe``."""

    return sys.executable


def which(name: str) -> str | None:
    """Resolve an executable portably (``None`` when absent)."""

    return shutil.which(name)


__all__ = [
    "CREATE_NO_WINDOW",
    "IS_WINDOWS",
    "hidden_kwargs",
    "popen_hidden",
    "python_executable",
    "run_hidden",
    "which",
]
