"""test_vpn_logging.py — vpn_manager logger lines must actually reach debug.log.

Regression test for the AUTH_FAILED traceability bug of 2026-08-17: during a
gluetun "AUTH: Received control message: AUTH_FAILED" incident the operator saw
no [vpn] / [vpn-watchdog] trace in logs/debug.log and concluded the proxy
"ne détecte pas l'erreur". Root cause: WebServerThread.start() did
``lg.handlers = [h]`` + ``lg.propagate = False`` on vpn_manager /
free_ip_pool, REPLACING the debug.log FileHandler installed at startup by
attach_module_logger(). The watchdog's
"[vpn-watchdog] AUTH_FAILED detected — restarting ..." (a WARNING) went only
to the GUI panel, never to the log file.

The fix — dashboard.display.attach_panel_logger — APPENDS the panel handler
instead of replacing it, so the FileHandler survives and vpn lines land in
debug.log with the same bracketed-timestamp format debug()/log() use.

Covered here (offline, temp-file isolated):
  * a WARNING "[vpn-watchdog] AUTH_FAILED detected" from the real
    "vpn_manager" logger is written to the debug.log file in the format
    "[<ts>] [vpn_manager] <msg>"
  * an INFO "[vpn] rotated → IP ..." line also reaches the file
  * attach_panel_logger does NOT drop the FileHandler — both handlers remain,
    propagation is off
  * attach_panel_logger is idempotent (no duplicate handler add)
"""
import logging
import re

import pytest

from dashboard import display as _disp

_AUTH_LINE = "[vpn-watchdog] AUTH_FAILED detected — restarting vpn"
_ROT_LINE = "[vpn] rotated → IP 187.40.35.141 (switch #183)"


@pytest.fixture()
def vpn_log(tmp_path):
    """Isolated real-path setup: temp debug.log + a clean vpn_manager logger."""
    logfile = tmp_path / "debug.log"
    logger = logging.getLogger("vpn_manager")
    # Baseline: strip any handler a previous test / the app attached.
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    old_level = logger.level
    logger.setLevel(logging.NOTSET)
    _disp.set_debug_log_file(str(logfile))
    yield logfile, logger
    # Teardown: detach + close our handlers, reset module globals so no
    # FileHandle stays locked on the tmp file (Windows file lock).
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    logger.setLevel(old_level)
    if _disp._debug_file is not None:
        try:
            _disp._debug_file.close()
        except Exception:
            pass
    _disp._debug_file = None
    _disp._debug_file_path = None
    _disp._extra_handlers = []


def _file_lines(logfile):
    return logfile.read_text(encoding="utf-8").splitlines()


def _lines_containing(lines, needle):
    return [l for l in lines if needle in l]


def test_warning_auth_failed_reaches_debug_log(vpn_log):
    """The exact watchdog line from the incident must end up in debug.log."""
    logfile, logger = vpn_log
    _disp.attach_module_logger("vpn_manager")
    _disp.attach_panel_logger("vpn_manager", _disp.RichLogHandler())

    logger.warning(_AUTH_LINE)

    hits = _lines_containing(_file_lines(logfile), _AUTH_LINE)
    assert hits, "AUTH_FAILED watchdog line is missing from debug.log"
    # debug.log's bracketed-timestamp format, e.g.
    # "[2026-08-17 18:54:50] [vpn_manager] [vpn-watchdog] AUTH_FAILED ..."
    assert re.match(
        r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[vpn_manager\] ", hits[0]
    ), f"unexpected format: {hits[0]!r}"


def test_info_rotation_line_reaches_debug_log(vpn_log):
    """INFO-level rotation lines must reach debug.log too."""
    logfile, logger = vpn_log
    _disp.attach_module_logger("vpn_manager")
    _disp.attach_panel_logger("vpn_manager", _disp.RichLogHandler())

    logger.info(_ROT_LINE)

    hits = _lines_containing(_file_lines(logfile), _ROT_LINE)
    assert hits, "rotation INFO line is missing from debug.log"


def test_panel_attach_preserves_file_handler(vpn_log):
    """The fix: attach_panel_logger must NOT clobber the debug.log handler."""
    logfile, logger = vpn_log
    fh = _disp.attach_module_logger("vpn_manager")
    _disp.attach_panel_logger("vpn_manager", _disp.RichLogHandler())

    assert fh in logger.handlers, "FileHandler was dropped by panel attach"
    assert logger.propagate is False, "propagation must be off (no double-emit)"


def test_panel_attach_is_idempotent(vpn_log):
    logfile, logger = vpn_log
    _disp.attach_module_logger("vpn_manager")
    handler = _disp.RichLogHandler()
    _disp.attach_panel_logger("vpn_manager", handler)
    before = list(logger.handlers)
    _disp.attach_panel_logger("vpn_manager", handler)
    assert logger.handlers == before, "duplicate panel handler was added"


def test_formatter_matches_debug_log_style(vpn_log):
    """FileHandler format must equal debug.log's bracketed-timestamp style."""
    logfile, logger = vpn_log
    fh = _disp.attach_module_logger("vpn_manager")
    fmt = fh.formatter
    assert isinstance(fmt, logging.Formatter)
    assert fmt._fmt == "[%(asctime)s] [%(name)s] %(message)s"
    assert fmt.datefmt == "%Y-%m-%d %H:%M:%S"