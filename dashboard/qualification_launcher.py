"""Operator-triggered qualification epoch launcher.

The dashboard never runs a sweep in its own event loop: it starts the existing
``qualification_daemon.py --once`` process and returns immediately.  Shadow mode
is the default; live mode is selected only when the operator config disables
``shadow_mode``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def launch_qualification_epoch(
    root: str | Path,
    *,
    shadow: bool = True,
    station: int = 1,
    max_hosts: int = 0,
    db_path: str | Path | None = None,
    log_path: str | Path | None = None,
) -> dict[str, Any]:
    base = Path(root)
    logs = Path(log_path) if log_path else base / "logs" / "qualification-operator.log"
    logs.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(base / "scripts" / "qualification_daemon.py"),
        "--once",
        "--shadow" if shadow else "--live",
        "--station",
        str(max(1, int(station))),
        "--max",
        str(max(0, int(max_hosts))),
    ]
    if db_path:
        command.extend(["--db", str(db_path)])
    creationflags = 0
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with open(logs, "ab") as output:
        process = subprocess.Popen(
            command,
            cwd=str(base),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    return {
        "pid": int(process.pid),
        "mode": "shadow" if shadow else "live",
        "station": max(1, int(station)),
        "max_hosts": max(0, int(max_hosts)),
        "started_at": time.time(),
        "log_path": str(logs),
        "process": process,
    }


__all__ = ["launch_qualification_epoch"]
