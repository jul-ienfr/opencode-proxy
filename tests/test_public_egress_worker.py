"""Local isolated CONNECT worker for public proxy egress."""

from __future__ import annotations

import asyncio
import base64
import ssl
import sys
import tempfile
from pathlib import Path

import pytest

from free.public_egress_worker import (
    PublicEgressWorker,
    start_worker_process,
    worker_env,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tls_helpers import write_self_signed_cert  # noqa: E402


def _auth(upstream_id: str, token: str) -> str:
    raw = base64.b64encode(f"{upstream_id}:{token}".encode()).decode()
    return f"Basic {raw}"


async def _echo_tunnel(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        data = await reader.readuntil(b"\r\n\r\n")
        if not data.startswith(b"CONNECT opencode.ai:443"):
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def _connect_via_worker(port: int, auth: str, target: str = "opencode.ai:443"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: {auth}\r\n\r\n"
    writer.write(request.encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    return reader, writer, head


async def _socks5_tunnel(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Minimal SOCKS5 server (no auth) used to prove the worker's path."""

    try:
        head = await reader.readexactly(2)
        if head[0] != 5:
            return
        methods = await reader.readexactly(head[1])
        if 0 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return
        writer.write(b"\x05\x00")
        await writer.drain()
        req = await reader.readexactly(4)
        if req[0] != 5 or req[1] != 1:
            return
        atyp = req[3]
        if atyp == 1:
            await reader.readexactly(4)
        elif atyp == 3:
            length = (await reader.readexactly(1))[0]
            await reader.readexactly(length)
        elif atyp == 4:
            await reader.readexactly(16)
        else:
            return
        await reader.readexactly(2)
        # Echo tunnel: proves the worker's SOCKS5 handshake and relay without
        # depending on real external network access in the test suite.
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_worker_relays_through_socks5_upstream():
    upstream = await asyncio.start_server(_socks5_tunnel, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    worker = PublicEgressWorker(
        {"p1": f"socks5://127.0.0.1:{upstream_port}"},
        "secret-token",
    )
    await worker.start()
    try:
        reader, writer, head = await _connect_via_worker(worker.port, _auth("p1", "secret-token"))
        assert b" 200 " in head
        writer.write(b"hello")
        await writer.drain()
        assert await reader.readexactly(5) == b"hello"
        writer.close()
        await writer.wait_closed()
    finally:
        await worker.close()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_worker_relays_through_http_upstream():
    upstream = await asyncio.start_server(_echo_tunnel, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    worker = PublicEgressWorker(
        {"p1": f"http://127.0.0.1:{upstream_port}"},
        "secret-token",
    )
    await worker.start()
    try:
        reader, writer, head = await _connect_via_worker(worker.port, _auth("p1", "secret-token"))
        assert b" 200 " in head
        writer.write(b"hello")
        await writer.drain()
        assert await reader.readexactly(5) == b"hello"
        writer.close()
        await writer.wait_closed()
    finally:
        await worker.close()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_worker_relays_through_https_upstream():
    pytest.importorskip("cryptography")
    with tempfile.TemporaryDirectory() as raw_tmp:
        cert_path, key_path = write_self_signed_cert(Path(raw_tmp), "opencode.ai")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
        upstream = await asyncio.start_server(_echo_tunnel, "127.0.0.1", 0, ssl=context)
        upstream_port = upstream.sockets[0].getsockname()[1]
        worker = PublicEgressWorker(
            {"p1": f"https://127.0.0.1:{upstream_port}"},
            "secret-token",
        )
        await worker.start()
        try:
            reader, writer, head = await _connect_via_worker(worker.port, _auth("p1", "secret-token"))
            assert b" 200 " in head
            writer.write(b"hello")
            await writer.drain()
            assert await reader.readexactly(5) == b"hello"
            writer.close()
            await writer.wait_closed()
        finally:
            await worker.close()
            upstream.close()
            await upstream.wait_closed()


@pytest.mark.asyncio
async def test_worker_health_endpoint():
    worker = PublicEgressWorker({"p1": "http://127.0.0.1:9"}, "secret-token")
    await worker.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", worker.port)
        writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(512), timeout=2)
        assert b" 200 " in data
        assert b'"ok":true' in data
        writer.close()
        await writer.wait_closed()
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_worker_rejects_bad_token():
    worker = PublicEgressWorker({"p1": "http://127.0.0.1:9"}, "secret-token")
    await worker.start()
    try:
        _reader, writer, head = await _connect_via_worker(worker.port, _auth("p1", "wrong"))
        assert b" 407 " in head
        writer.close()
        await writer.wait_closed()
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_worker_rejects_non_allowed_target():
    worker = PublicEgressWorker({"p1": "http://127.0.0.1:9"}, "secret-token")
    await worker.start()
    try:
        _reader, writer, head = await _connect_via_worker(
            worker.port, _auth("p1", "secret-token"), target="evil.example:443"
        )
        assert b" 403 " in head
        writer.close()
        await writer.wait_closed()
    finally:
        await worker.close()


def test_worker_env_scrubs_secrets(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-secret")
    monkeypatch.setenv("OPENCODE_GO_AUTH_COOKIE", "cookie-secret")
    monkeypatch.setenv("DASHBOARD_TOKEN", "dash-secret")
    env = worker_env()
    assert "OPENCODE_API_KEY" not in env
    assert "OPENCODE_GO_AUTH_COOKIE" not in env
    assert "DASHBOARD_TOKEN" not in env


@pytest.mark.asyncio
async def test_worker_process_starts_and_relays():
    upstream = await asyncio.start_server(_echo_tunnel, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    handle = start_worker_process({"p1": f"http://127.0.0.1:{upstream_port}"})
    try:
        reader, writer, head = await _connect_via_worker(handle.port, _auth("p1", handle.token))
        assert b" 200 " in head
        writer.write(b"hello")
        await writer.drain()
        assert await reader.readexactly(5) == b"hello"
        writer.close()
        await writer.wait_closed()
    finally:
        handle.stop()
        upstream.close()
        await upstream.wait_closed()
