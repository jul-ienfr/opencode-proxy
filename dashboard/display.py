"""
Rich terminal display: token usage table, log panel, keyboard input.
"""

import collections
import logging
import os
import sys
import threading
import time

from rich import box
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

import config.settings as _cfg_settings

log_lines = collections.deque(maxlen=200)
LOG_VISIBLE = 35
_log_scroll = 0
_display_dirty = True  # Flag: set True when token usage changes, False after rebuild

# Debug file handle — set via set_debug_log_file() after LOG_DIR is known
_debug_file = None
_debug_file_path = None
_debug_write_counter = 0
_DEBUG_FLUSH_INTERVAL = 10  # Flush to disk every N writes (reduces syscall overhead)
_DEBUG_MAX_SIZE = 10 * 1024 * 1024  # Auto-rotate when file exceeds 10 MB
_extra_handlers = []  # FileHandlers attached to module loggers (vpn_manager, free_ip_pool)


def _rotate_debug_log():
    """Rotate debug.log → debug.log.1 when it exceeds _DEBUG_MAX_SIZE."""
    global _debug_file, _debug_file_path
    if _debug_file is None or _debug_file_path is None:
        return
    try:
        size = os.path.getsize(_debug_file_path)
        if size <= _DEBUG_MAX_SIZE:
            return
        _debug_file.close()
        # [36] close the module-logger FileHandlers BEFORE the rename —
        # on Windows an open handle makes os.rename fail with WinError 32.
        # FileHandler re-opens baseFilename lazily on the next emit.
        for _fh in _extra_handlers:
            try:
                _fh.close()
            except Exception:
                pass
        rotated = _debug_file_path + ".1"
        if os.path.exists(rotated):
            os.remove(rotated)
        os.rename(_debug_file_path, rotated)
        _debug_file = open(_debug_file_path, "a", encoding="utf-8")
        _debug_write_counter = 0
    except Exception:
        pass


def set_debug_log_file(path: str):
    """Open (or create) the debug log file. Called once at startup by opencode.py."""
    global _debug_file, _debug_file_path, _debug_write_counter
    _debug_file_path = path
    try:
        _debug_file = open(path, "a", encoding="utf-8")
        _debug_write_counter = 0
        _rotate_debug_log()  # Rotate if needed at startup
    except Exception:
        _debug_file = None


def attach_module_logger(name: str, level: int = logging.INFO):
    """Attach the stdlib logger ``name`` to debug.log (rotation-aware).

    Used for critical subsystems (vpn_manager, free_ip_pool) whose failures
    must be visible in debug.log even when DEBUG is off — rotation failures
    happen exactly when the tunnel is down, the moments you need the trace
    most. The handler is registered in ``_extra_handlers`` so rotation
    closes it before the rename (Windows file lock) and it re-opens the
    new file lazily on the next emit.
    """
    global _debug_file_path
    if not _debug_file_path:
        return None
    logger = logging.getLogger(name)
    if logger.level == logging.NOTSET or logger.level > level:
        logger.setLevel(level)
    fh = logging.FileHandler(_debug_file_path, encoding="utf-8")
    # Same bracketed-timestamp style as debug()/log() writes to debug.log,
    # e.g. "[2026-08-17 18:54:50] [vpn_manager] [vpn-watchdog] ...". Keeping
    # one consistent format lets header-anchored greps find both paths.
    fh.setFormatter(
        logging.Formatter("[%(asctime)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    fh.setLevel(level)
    logger.addHandler(fh)
    _extra_handlers.append(fh)
    return fh


def attach_panel_logger(name: str, handler: logging.Handler):
    """Route a module logger to a panel/UI handler WITHOUT dropping existing ones.

    WebServerThread attaches RichLogHandler to vpn_manager/free_ip_pool so
    their lines appear in the dashboard panel. Replacing ``logger.handlers``
    here would clobber the debug.log FileHandler installed by
    attach_module_logger() — exactly what made [vpn]/[vpn-watchdog] lines
    invisible in logs/debug.log during AUTH_FAILED incidents. Append instead,
    and stop propagation so records don't double-emit through the root logger.
    """
    logger = logging.getLogger(name)
    if handler not in logger.handlers:
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def log(msg: str):
    global _log_scroll
    if "/api/" in msg or "Uvicorn running" in msg:
        return
    ts = time.strftime("%H:%M:%S")
    log_lines.append(f"[{ts}] {msg}")
    _log_scroll = max(0, len(log_lines) - LOG_VISIBLE)


def debug(msg: str):
    """Emit a debug message to terminal + debug.log file. No-op when DEBUG is off."""
    if not _cfg_settings.DEBUG:
        return
    # Write to terminal / web dashboard
    log(f"[DEBUG] {msg}")
    # Write to debug.log file (buffered: flush every N writes to reduce syscall overhead)
    if _debug_file is not None:
        try:
            global _debug_write_counter
            _rotate_debug_log()  # Auto-rotate if file is too large
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            _debug_file.write(f"[{ts}] {msg}\n")
            _debug_write_counter += 1
            if _debug_write_counter >= _DEBUG_FLUSH_INTERVAL:
                _debug_file.flush()
                _debug_write_counter = 0
        except Exception:
            pass


def _logger_writes_debug_file(logger_name: str) -> bool:
    """[plan 30/08 Lot B2] True si le logger émetteur possède DÉJÀ un
    FileHandler pointé sur debug.log (posé par ``attach_module_logger`` pour
    vpn_manager / free_ip_pool au démarrage) — le handler panel ne doit alors
    écrire QUE dans le panneau, sinon chaque ligne part en double dans
    debug.log (I/O ×2, rétention ÷2, hot-reload opencode.py:12733+)."""
    if not _debug_file_path:
        return False
    try:
        target = os.path.abspath(_debug_file_path)
        for h in logging.getLogger(logger_name).handlers:
            base = getattr(h, "baseFilename", None)
            if base and os.path.abspath(base) == target:
                return True
    except Exception:
        pass
    return False


class RichLogHandler(logging.Handler):
    def emit(self, record):
        msg = record.getMessage()
        if "/api/" in msg or "Uvicorn running" in msg:
            return
        # When DEBUG is on, forward all levels (including DEBUG) to the log panel
        if not _cfg_settings.DEBUG and record.levelno < logging.WARNING:
            return
        level = record.levelname
        ts = time.strftime("%H:%M:%S")
        log_lines.append(f"[{ts}] [{level}] {msg}")
        # Also write DEBUG-level messages to the debug.log file (buffered
        # flush) — sauf si le logger a déjà son FileHandler dessus (Lot B2).
        if (
            _cfg_settings.DEBUG
            and _debug_file is not None
            and record.levelno <= logging.DEBUG
            and not _logger_writes_debug_file(record.name)
        ):
            try:
                global _debug_write_counter
                _rotate_debug_log()  # Auto-rotate if file is too large
                fts = time.strftime("%Y-%m-%d %H:%M:%S")
                _debug_file.write(f"[{fts}] [{level}] {msg}\n")
                _debug_write_counter += 1
                if _debug_write_counter >= _DEBUG_FLUSH_INTERVAL:
                    _debug_file.flush()
                    _debug_write_counter = 0
            except Exception:
                pass


def build_display(routes, token_usage, token_lock):
    table = Table(
        box=box.SIMPLE, show_header=True, header_style="bold cyan", pad_edge=False, expand=False
    )
    table.add_column("Route", style="bold", width=8)
    table.add_column("Model", style="bold", min_width=14)
    table.add_column("Total", justify="right", min_width=10)
    table.add_column("Input", justify="right", min_width=10)
    table.add_column("Output", justify="right", min_width=10)
    table.add_column("Cache", justify="right", min_width=10)
    table.add_column("%", justify="right", min_width=6)

    with token_lock:
        usage_snapshot = {m: dict(d) for m, d in token_usage.items()}

    sum_total = 0
    for route_info in routes.values():
        d = usage_snapshot.get(route_info["model"], {"input": 0, "output": 0, "cache": 0})
        sum_total += d["input"] + d["output"] + d["cache"]

    sum_in = sum_out = sum_cache = 0
    shown = set()
    for route_name, route_info in routes.items():
        model = route_info["model"]
        shown.add(model)
        d = usage_snapshot.get(model, {"input": 0, "output": 0, "cache": 0})
        total = d["input"] + d["output"] + d["cache"]
        sum_in += d["input"]
        sum_out += d["output"]
        sum_cache += d["cache"]
        pct = f"{total / sum_total * 100:.1f}%" if sum_total else "0%"
        table.add_row(
            route_name,
            model,
            f"{total:,}",
            f"{d['input']:,}",
            f"{d['output']:,}",
            f"{d['cache']:,}",
            pct,
        )

    for model, d in usage_snapshot.items():
        if model in shown:
            continue
        total = d["input"] + d["output"] + d["cache"]
        if total == 0:
            continue
        sum_in += d["input"]
        sum_out += d["output"]
        sum_cache += d["cache"]
        pct = f"{total / sum_total * 100:.1f}%" if sum_total else "0%"
        table.add_row(
            "-",
            model,
            f"{total:,}",
            f"{d['input']:,}",
            f"{d['output']:,}",
            f"{d['cache']:,}",
            pct,
        )

    sum_total = sum_in + sum_out + sum_cache
    table.add_row(
        "[bold yellow]ALL[/]",
        "",
        f"[bold yellow]{sum_total:,}[/]",
        f"[bold yellow]{sum_in:,}[/]",
        f"[bold yellow]{sum_out:,}[/]",
        f"[bold yellow]{sum_cache:,}[/]",
        "[bold yellow]100%[/]",
    )

    start = max(0, min(_log_scroll, len(log_lines) - LOG_VISIBLE))
    visible = list(log_lines)[start : start + LOG_VISIBLE]
    log_text = "\n".join(visible) if visible else "[dim]waiting for requests...[/]"
    if len(log_lines) > LOG_VISIBLE:
        log_text += f"\n[dim]↑ {start + 1}/{len(log_lines)} logs (scroll with ↑↓ keys)[/]"

    return Group(
        Panel(table, title="[bold green]Token Usage[/]", border_style="green", padding=(0, 1)),
        Panel(log_text, title="[bold]Log[/]", border_style="dim", padding=(0, 1)),
    )


def start_input_thread():
    global _log_scroll
    _stop_event = threading.Event()

    def _input_thread():
        if sys.platform == "win32":
            import msvcrt

            while not _stop_event.is_set():
                if msvcrt.kbhit():
                    try:
                        ch = msvcrt.getch()
                        if ch in (b"\xe0", b"\x00"):
                            ch2 = msvcrt.getch()
                            if ch2 == b"H":
                                _log_scroll = max(0, _log_scroll - 1)
                            elif ch2 == b"P":
                                _log_scroll = min(
                                    max(0, len(log_lines) - LOG_VISIBLE), _log_scroll + 1
                                )
                            elif ch2 == b"I":
                                _log_scroll = max(0, _log_scroll - LOG_VISIBLE)
                            elif ch2 == b"Q":
                                _log_scroll = min(
                                    max(0, len(log_lines) - LOG_VISIBLE), _log_scroll + LOG_VISIBLE
                                )
                            elif ch2 == b"G":
                                _log_scroll = 0
                            elif ch2 == b"O":
                                _log_scroll = max(0, len(log_lines) - LOG_VISIBLE)
                            continue
                        ch = ch.decode("utf-8", errors="ignore")
                        if ch == "\x03":
                            _stop_event.set()
                        elif ch == "k":
                            _log_scroll = max(0, _log_scroll - 1)
                        elif ch == "j":
                            _log_scroll = min(max(0, len(log_lines) - LOG_VISIBLE), _log_scroll + 1)
                        elif ch == "G":
                            _log_scroll = max(0, len(log_lines) - LOG_VISIBLE)
                        elif ch == "g":
                            _log_scroll = 0
                    except Exception:
                        pass
                else:
                    time.sleep(0.05)
        else:
            import select
            import termios
            import tty

            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setraw(fd)
            try:
                while not _stop_event.is_set():
                    if select.select([sys.stdin], [], [], 0.05)[0]:
                        ch = sys.stdin.read(1)
                        if ch == "\x03":
                            _stop_event.set()
                        elif ch == "\x1b":
                            seq = (
                                sys.stdin.read(2)
                                if select.select([sys.stdin], [], [], 0.01)[0]
                                else ""
                            )
                            if seq == "[A":
                                _log_scroll = max(0, _log_scroll - 1)
                            elif seq == "[B":
                                _log_scroll = min(
                                    max(0, len(log_lines) - LOG_VISIBLE), _log_scroll + 1
                                )
                            elif seq == "[5":
                                if sys.stdin.read(1) == "~":
                                    _log_scroll = max(0, _log_scroll - LOG_VISIBLE)
                            elif seq == "[6":
                                if sys.stdin.read(1) == "~":
                                    _log_scroll = min(
                                        max(0, len(log_lines) - LOG_VISIBLE),
                                        _log_scroll + LOG_VISIBLE,
                                    )
                            elif seq == "[H":
                                _log_scroll = 0
                            elif seq == "[F":
                                _log_scroll = max(0, len(log_lines) - LOG_VISIBLE)
                        elif ch in ("k",):
                            _log_scroll = max(0, _log_scroll - 1)
                        elif ch in ("j",):
                            _log_scroll = min(max(0, len(log_lines) - LOG_VISIBLE), _log_scroll + 1)
                        elif ch == "G":
                            _log_scroll = max(0, len(log_lines) - LOG_VISIBLE)
                        elif ch == "g":
                            _log_scroll = 0
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    threading.Thread(target=_input_thread, daemon=True).start()
    return lambda: not _stop_event.is_set()


def run_terminal_loop(routes, token_usage, token_lock):
    global _display_dirty
    stop = start_input_thread()
    try:
        with Live(
            build_display(routes, token_usage, token_lock), refresh_per_second=1, screen=True
        ) as live:
            while stop():
                if _display_dirty:
                    _display_dirty = False
                    live.update(build_display(routes, token_usage, token_lock))
                time.sleep(0.5)
    except Exception as e:
        log(f"  [display] crashed: {type(e).__name__}: {e}")
        raise  # re-throw so __main__ handler can log it too
