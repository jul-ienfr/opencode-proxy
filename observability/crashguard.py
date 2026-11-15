"""Capture de crash ÔÇö feature 019, t├óche T301.

L'app tourne souvent via pythonw (aucune console) : sans hook, une exception
non rattrap├®e ou un segfault ne laisse aucune trace. Ce module installe
``faulthandler`` et des excepthooks qui ajoutent un traceback horodat├® ├á
``logs/app-crash.log``, puis d├®l├¿guent au hook d'origine.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback

__all__ = ["crash_log_path", "install"]

_LOG_NAME = "app-crash.log"


def crash_log_path(root: str | None = None) -> str:
    base = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "logs", _LOG_NAME)


def _write(kind: str, exc_type, exc, tb, path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                f"\n[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] "
                f"{kind}: {exc_type.__name__ if exc_type else '?'}: {exc}\n"
            )
            fh.write("".join(traceback.format_exception(exc_type, exc, tb)))
            fh.flush()
    except Exception:
        pass


def install(root: str | None = None) -> str:
    """Installe faulthandler + excepthooks. Retourne le chemin du journal."""
    path = crash_log_path(root)
    try:
        faulthandler.enable()
    except Exception:  # pragma: no cover ÔÇö pythonw sans stderr
        pass

    def _excepthook(exc_type, exc, tb):
        _write("uncaught", exc_type, exc, tb, path)
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            pass

    def _thread_excepthook(args):
        _write(
            f"thread:{getattr(args.thread, 'name', '?')}",
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            path,
        )

    sys.excepthook = _excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook
    return path