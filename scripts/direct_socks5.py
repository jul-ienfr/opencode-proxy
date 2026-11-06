"""Minimal SOCKS5 CONNECT proxy for the direct egress.

Why: the free pool's ``socks5`` mode round-robins over a static list. The
WARP container is one entry; this bridge is the second, so the pool can
treat the host's direct IP as a normal station and fail over to it without
a special case in ``free/pool.py``.

It implements only what curl/curl_cffi need: version 5, no-auth handshake,
CONNECT to IPv4 / domain / IPv6, then a bidirectional byte pipe. No UDP
ASSOCIATE, no BIND, no authentication.

Run:
    pythonw scripts/direct_socks5.py --port 11084
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import struct
import sys
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "direct_socks5.log"


def _log() -> logging.Logger:
    logger = logging.getLogger("direct_socks5")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _read_destination(
    reader: asyncio.StreamReader, atyp: int
) -> tuple[str, int] | None:
    if atyp == 0x01:  # IPv4
        host = socket.inet_ntoa(await reader.readexactly(4))
    elif atyp == 0x03:  # domain
        length = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(length)).decode("idna", "strict")
    elif atyp == 0x04:  # IPv6
        host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
    else:
        return None
    port = struct.unpack("!H", await reader.readexactly(2))[0]
    return host, port


async def _handle(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    logger = _log()
    peer = writer.get_extra_info("peername")
    try:
        version, method_count = await reader.readexactly(2)
        methods = await reader.readexactly(method_count)
        if version != 5 or 0x00 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return
        writer.write(b"\x05\x00")
        await writer.drain()

        version, command, _reserved, atyp = await reader.readexactly(4)
        if version != 5 or command != 0x01:
            writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return

        destination = await _read_destination(reader, atyp)
        if destination is None:
            writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return
        host, port = destination

        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=15.0
            )
        except Exception as exc:
            logger.warning("connect %s:%s failed: %s", host, port, exc)
            writer.write(b"\x05\x05\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return

        writer.write(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
        await writer.drain()
        await asyncio.gather(
            _pipe(reader, remote_writer),
            _pipe(remote_reader, writer),
        )
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    except Exception as exc:  # never let one client kill the server
        logger.warning("client %s error: %s", peer, exc)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _serve(host: str, port: int) -> None:
    server = await asyncio.start_server(_handle, host, port)
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or ())
    _log().info("listening on %s", sockets)
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11084)
    args = parser.parse_args(argv)
    try:
        asyncio.run(_serve(args.host, args.port))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
