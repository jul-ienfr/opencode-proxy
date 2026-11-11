"""Command builder for the qualification daemon launched by the supervisor."""

from __future__ import annotations

import sys
from pathlib import Path


def daemon_once_command(
    root: str | Path,
    *,
    shadow: bool = True,
    config: str | Path | None = None,
    db: str | Path | None = None,
    watchdog: bool = True,
    watchdog_url: str = "http://127.0.0.1:4000",
    max_hosts: int = 0,
) -> list[str]:
    base = Path(root)
    command = [
        sys.executable,
        str(base / "scripts" / "qualification_daemon.py"),
        "--once",
        "--shadow" if shadow else "--live",
    ]
    if watchdog:
        command.extend(["--watchdog", "--watchdog-url", str(watchdog_url)])
    if config:
        command.extend(["--config", str(config)])
    if db:
        command.extend(["--db", str(db)])
    if int(max_hosts or 0) > 0:
        # [P0 2026-11-10] Un sweep borné garde le tick superviseur court :
        # --max 1 actualise les états de station en <1 s au lieu de bloquer
        # jusqu'à 180 s sur un sweep complet.
        command.extend(["--max", str(int(max_hosts))])
    return command


__all__ = ["daemon_once_command"]
