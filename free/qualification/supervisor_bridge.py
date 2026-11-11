"""Supervisor bridge for the qualification daemon tick."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from config.settings import yaml_get

from .daemon_control import daemon_once_command


def _qualified_shadow_mode() -> bool:
    """Shadow policy for the supervisor tick.

    ``daemon_shadow`` is explicit and defaults to ``shadow_mode``.  When at
    least one dedicated qualifier is active, the tick is forced to shadow:
    production stations must never be re-bound by the automatic daemon while
    the live sweeps run on stations 7-10.
    """

    try:
        qualifiers = yaml_get(
            "egress_catalog", "qualification.qualifiers", {}
        ) or {}
        if bool(qualifiers.get("enabled", False)) and int(
            qualifiers.get("count", 0) or 0
        ) > 0:
            return True
    except (TypeError, ValueError, AttributeError):
        pass
    try:
        explicit = yaml_get(
            "egress_catalog", "qualification.daemon_shadow", None
        )
        if explicit is not None:
            return bool(explicit)
    except Exception:
        pass
    return bool(
        yaml_get("egress_catalog", "qualification.shadow_mode", True)
    )


def qualification_tick(
    root: str | Path,
    *,
    last_at: float,
    no_window: int = 0,
    log=print,
    now: float | None = None,
) -> float:
    """Run one qualification cycle when enabled and due; return the new timestamp."""

    enabled = bool(
        yaml_get("egress_catalog", "qualification.daemon_enabled", False)
    )
    try:
        interval = max(
            60.0,
            float(
                yaml_get(
                    "egress_catalog",
                    "qualification.daemon_interval_s",
                    900,
                )
                or 900
            ),
        )
    except (TypeError, ValueError):
        interval = 900.0
    try:
        max_hosts = max(
            0,
            int(
                yaml_get(
                    "egress_catalog",
                    "qualification.daemon_max_hosts",
                    0,
                )
                or 0
            ),
        )
    except (TypeError, ValueError):
        max_hosts = 0
    ts = float(time.time() if now is None else now)
    if not enabled or ts - float(last_at) < interval:
        return float(last_at)
    shadow = _qualified_shadow_mode()
    try:
        proc = subprocess.run(
            daemon_once_command(root, shadow=shadow, max_hosts=max_hosts),
            cwd=str(Path(root)),
            capture_output=True,
            text=True,
            timeout=180,
            creationflags=int(no_window),
        )
        if proc.returncode == 0:
            log(
                "qualification daemon once ok "
                f"({'shadow' if shadow else 'live'})"
            )
        else:
            log(
                "qualification daemon failed: "
                f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
            )
    except Exception as exc:
        log(f"qualification daemon error: {type(exc).__name__}: {exc}")
    return ts


__all__ = ["qualification_tick"]
