#!/usr/bin/env python3
"""Lanceur supervis├® de l'app ÔÇö feature 019, t├óche T301.

Maintient ``opencode.py`` en vie sans d├®pendre de pythonw : red├®marrage en
moins de 30 s, stdout et stderr horodat├®s dans deux fichiers s├®par├®s, trace de
chaque cycle dans ``logs/app-supervisor.log``.

Arr├¬t propre : cr├®er ``logs/app.stop`` (ou Ctrl+C). Le superviseur termine
l'enfant puis sort sans le relancer.

Exemples :
    python scripts/app_supervisor.py
    python scripts/app_supervisor.py --once --health-timeout 20
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.environ.get("OPENCODE_APP_ENTRY") or os.path.join(ROOT, "opencode.py")
LOG_DIR = os.path.join(ROOT, "logs")
STDOUT_LOG = os.path.join(LOG_DIR, "app-stdout.log")
STDERR_LOG = os.path.join(LOG_DIR, "app-stderr.log")
SUPERVISOR_LOG = os.path.join(LOG_DIR, "app-supervisor.log")
PID_FILE = os.path.join(LOG_DIR, "app.pid")
STOP_FILE = os.path.join(LOG_DIR, "app.stop")
MAX_LOG_BYTES = 50 * 1024 * 1024
RESTART_DELAY_S = 2.0
MAX_RESTART_DELAY_S = 15.0
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 4000
HEALTH_PATH = "/health"
HEALTH_FAIL_LIMIT_S = 45.0


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _LineLog:
    """Fichier de log horodat├® ligne par ligne, avec rotation born├®e."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._fh = self._open()

    def _open(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            if os.path.getsize(self.path) > MAX_LOG_BYTES:
                os.replace(self.path, f"{self.path}.1")
        except OSError:
            pass
        return open(self.path, "a", encoding="utf-8", errors="replace")

    def write(self, line: str) -> None:
        with self._lock:
            try:
                self._fh.write(f"{_now()} {line.rstrip()}\n")
                self._fh.flush()
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except (OSError, ValueError):
                pass


def _pump(stream, log: _LineLog) -> None:
    try:
        for line in stream:
            log.write(line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _health_ok(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((HEALTH_HOST, HEALTH_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_health(limit_s: float) -> float | None:
    start = time.monotonic()
    while time.monotonic() - start < limit_s:
        if _health_ok():
            return time.monotonic() - start
        time.sleep(0.5)
    return None


def _terminate(proc: subprocess.Popen, grace_s: float = 10.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _spawn(out: _LineLog, err: _LineLog) -> subprocess.Popen:
    kwargs: dict = {
        "cwd": ROOT,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([sys.executable, "-u", APP], **kwargs)
    threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, err), daemon=True).start()
    return proc


def run(health_timeout: float = 25.0, once: bool = False, force: bool = False) -> int:
    os.makedirs(LOG_DIR, exist_ok=True)
    out = _LineLog(STDOUT_LOG)
    err = _LineLog(STDERR_LOG)
    sup = _LineLog(SUPERVISOR_LOG)
    stopping = threading.Event()

    def _handle_signal(signum, _frame):  # pragma: no cover - signal path
        sup.write(f"signal {signum}; arr├¬t demand├®")
        stopping.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handle_signal)
            except (OSError, ValueError):
                pass

    delay = RESTART_DELAY_S
    restarts = 0
    try:
        while not stopping.is_set():
            if os.path.exists(STOP_FILE):
                sup.write(f"fichier d'arr├¬t d├®tect├®: {STOP_FILE}")
                break
            if _health_ok() and not force:
                sup.write(
                    f"port {HEALTH_PORT} d├®j├á occup├® par une app non supervis├®e; "
                    "attente de sa lib├®ration"
                )
                time.sleep(5.0)
                continue
            started = time.monotonic()
            sup.write(f"d├®marrage pid=? (restart#{restarts})")
            proc = _spawn(out, err)
            try:
                with open(PID_FILE, "w", encoding="utf-8") as fh:
                    fh.write(str(proc.pid))
            except OSError:
                pass
            health = _wait_health(health_timeout)
            if health is None:
                sup.write(
                    f"pid={proc.pid} /health indisponible apr├¿s {health_timeout:.0f}s"
                )
            else:
                sup.write(f"pid={proc.pid} /health 200 apr├¿s {health:.1f}s")
                delay = RESTART_DELAY_S

            health_fail_since: float | None = None
            while proc.poll() is None and not stopping.is_set():
                if os.path.exists(STOP_FILE):
                    sup.write("fichier d'arr├¬t d├®tect├®; terminaison de l'enfant")
                    stopping.set()
                # [P0 2026-11-14] A dead listen socket (WinError 64) leaves
                # the process alive but /health unreachable. Waiting on
                # proc.poll() alone kept that zombie forever: kill it and
                # respawn after a sustained health failure.
                if _health_ok():
                    health_fail_since = None
                else:
                    if health_fail_since is None:
                        health_fail_since = time.monotonic()
                    elif (
                        time.monotonic() - health_fail_since
                        >= HEALTH_FAIL_LIMIT_S
                    ):
                        sup.write(
                            f"pid={proc.pid} /health indisponible depuis "
                            f"{time.monotonic() - health_fail_since:.0f}s; "
                            "arr├¬t du zombie et red├®marrage"
                        )
                        _terminate(proc)
                        break
                time.sleep(0.5)
            _terminate(proc)
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
            if stopping.is_set():
                break
            code = proc.returncode
            lived = time.monotonic() - started
            restarts += 1
            sup.write(
                f"pid={proc.pid} sorti code={code} apr├¿s {lived:.1f}s; "
                f"red├®marrage dans {delay:.0f}s"
            )
            if once:
                return int(code or 1)
            end = time.monotonic() + delay
            while time.monotonic() < end and not stopping.is_set():
                if os.path.exists(STOP_FILE):
                    stopping.set()
                    break
                time.sleep(0.25)
            delay = min(MAX_RESTART_DELAY_S, max(RESTART_DELAY_S, delay * 2))
    finally:
        out.close()
        err.close()
        sup.write("superviseur arr├¬t├®")
        sup.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=25.0,
        help="d├®lai d'attente de /health apr├¿s d├®marrage (d├®faut 25 s)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="un seul cycle (test) : sort avec le code de l'app",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignorer le pr├®-contr├┤le du port (tests uniquement)",
    )
    args = parser.parse_args(argv)
    return run(health_timeout=args.health_timeout, once=args.once, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())