"""Contrôleur du Fresh IP Hunter (process séparé, activable depuis le GUI).

Le hunter est un script autonome : il rotationne une station, teste un vrai
POST free, et s'arrête au premier 200. Ce module gère son cycle de vie
(démarrage/arrêt/statut/log) et persiste la préférence dans
``config.yaml`` → ``free.fresh_ip_hunter.enabled``.

Aucun import de ``opencode`` ici : le dashboard et le proxy peuvent l'importer
sans cycle.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "fresh_ip_hunter.py"
LOG_PATH = ROOT / "logs" / "fresh_ip_hunter.log"
PID_PATH = ROOT / "logs" / "fresh_ip_hunter.pid"

_LOCK = threading.RLock()
_PROC: subprocess.Popen | None = None
_STARTED_AT: float = 0.0


def _cfg() -> dict:
    try:
        from config import settings

        raw = settings.yaml_get("free", "fresh_ip_hunter", {})
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _persist(enabled: bool) -> None:
    try:
        from config import settings

        cfg = _cfg()
        cfg["enabled"] = bool(enabled)
        cfg.setdefault("interval_s", 600)
        cfg.setdefault("max_attempts", 36)
        settings.yaml_set("free", "fresh_ip_hunter", cfg)
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _pid_from_file() -> int:
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _tail(lines: int = 24) -> list[str]:
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            data = f.readlines()
        return [ln.rstrip("\n") for ln in data[-max(1, int(lines)) :]]
    except Exception:
        return []


def status() -> dict:
    with _LOCK:
        proc = _PROC
        pid = proc.pid if proc is not None and proc.poll() is None else _pid_from_file()
        running = bool(pid and _pid_alive(pid))
        return {
            "enabled": bool(_cfg().get("enabled", False)),
            "running": running,
            "pid": int(pid) if running else None,
            "started_at": _STARTED_AT if running else None,
            "log_path": str(LOG_PATH),
            "last_log": _tail(24),
        }


def start() -> dict:
    with _LOCK:
        if status()["running"]:
            _persist(True)
            return status()
        if not SCRIPT.exists():
            return {**status(), "error": "scripts/fresh_ip_hunter.py introuvable"}
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            cfg = _cfg()
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["OPENCODE_HUNTER_INTERVAL_S"] = str(cfg.get("interval_s", 600))
            env["OPENCODE_HUNTER_MAX_ATTEMPTS"] = str(cfg.get("max_attempts", 36))
            flags = 0
            if os.name == "nt":
                flags = 0x08000000  # CREATE_NO_WINDOW
            global _PROC, _STARTED_AT
            _PROC = subprocess.Popen(
                [sys.executable, str(SCRIPT)],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            _STARTED_AT = time.time()
            try:
                PID_PATH.write_text(str(_PROC.pid), encoding="utf-8")
            except Exception:
                pass
            _persist(True)
            time.sleep(0.3)
            return status()
        except Exception as exc:
            return {**status(), "error": f"{type(exc).__name__}: {exc}"}


def stop() -> dict:
    with _LOCK:
        global _PROC, _STARTED_AT
        proc = _PROC
        pid = proc.pid if proc is not None and proc.poll() is None else _pid_from_file()
        if pid and _pid_alive(pid):
            try:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                elif os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                        capture_output=True,
                        check=False,
                    )
                else:
                    os.kill(int(pid), 15)
            except Exception:
                pass
        _PROC = None
        _STARTED_AT = 0.0
        try:
            PID_PATH.unlink()
        except OSError:
            pass
        _persist(False)
        return status()


def set_enabled(enabled: bool) -> dict:
    return start() if enabled else stop()


def ensure_started() -> None:
    try:
        if bool(_cfg().get("enabled", False)):
            start()
    except Exception:
        pass


def tail(lines: int = 80) -> list[str]:
    return _tail(lines)


__all__ = ["ensure_started", "set_enabled", "start", "status", "stop", "tail"]
