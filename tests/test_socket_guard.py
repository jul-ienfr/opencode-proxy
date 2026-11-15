"""Tests du guard socket (021 T004/T005)."""

from __future__ import annotations

import errno
import os

from observability import socket_guard


def test_detects_winerror_64() -> None:
    exc = OSError(64, "Le nom réseau spécifié n'est plus disponible")
    exc.winerror = 64  # type: ignore[attr-defined]
    assert socket_guard._is_listen_failure({"exception": exc}) is True


def test_detects_accept_failed_message() -> None:
    assert (
        socket_guard._is_listen_failure({"message": "Accept failed on a socket"})
        is True
    )


def test_detects_errno_10038() -> None:
    exc = OSError(errno.EBADF, "not a socket")
    exc.errno = 10038  # type: ignore[attr-defined]
    assert socket_guard._is_listen_failure({"exception": exc}) is True


def test_ignores_regular_exception() -> None:
    assert socket_guard._is_listen_failure({"exception": ValueError("boom")}) is False


def test_install_is_idempotent() -> None:
    class _Loop:
        def __init__(self) -> None:
            self.handlers: list = []

        def set_exception_handler(self, handler) -> None:  # noqa: ANN001
            self.handlers.append(handler)

    loop = _Loop()
    previous = socket_guard._INSTALLED
    socket_guard._INSTALLED = False
    try:
        socket_guard.install(loop)  # type: ignore[arg-type]
        socket_guard.install(loop)  # type: ignore[arg-type]
        assert len(loop.handlers) == 1
    finally:
        socket_guard._INSTALLED = previous
