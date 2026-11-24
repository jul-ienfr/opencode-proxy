"""Écoute dual-stack : le proxy doit répondre en IPv4 **et** en IPv6."""

from __future__ import annotations

import socket

import pytest

from opencode import ServerManager


def test_dualstack_socket_accepts_ipv4_and_ipv6():
    sock = ServerManager._dualstack_socket("0.0.0.0", 0)
    if sock is None:
        pytest.skip("IPv6 non disponible sur cette plateforme")
    port = int(sock.getsockname()[1])
    try:
        for host in ("127.0.0.1", "::1"):
            try:
                conn = socket.create_connection((host, port), timeout=2.0)
            except OSError as exc:  # pragma: no cover - plateforme sans v6
                pytest.skip(f"{host} indisponible: {exc}")
            conn.close()
    finally:
        sock.close()


def test_dualstack_skips_explicit_host():
    assert ServerManager._dualstack_socket("127.0.0.1", 0) is None
    assert ServerManager._dualstack_socket("192.0.2.1", 0) is None
