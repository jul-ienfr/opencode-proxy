"""Worker d'egress isolé pour les proxys publics.

Le processus principal ne parle jamais directement au proxy public : il parle
à ce worker sur ``127.0.0.1``. Le worker relaie le CONNECT vers le proxy
upstream, sans terminer le TLS d'origine. Il tourne avec un environnement
scrubbé (aucune clé payante, aucun cookie) : un exploit du proxy public ne
peut lire que les URLs des proxys publics, pas les secrets du proxy.

Le worker est un proxy HTTP CONNECT local. L'upstream est choisi par
``Proxy-Authorization: Basic base64(<id>:<token>)``. Un seul port local sert
donc plusieurs proxys publics, ce qui permet de garder la rotation du pool
sans multiplier les processus.

Ce fichier n'importe aucun module du projet : il doit pouvoir être lancé avec
``python -I`` dans un environnement vidé.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hmac
import json
import logging
import os
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

# Sondes d'IP publiques : GET statique, aucun secret, utile au registre.
HEALTH_HOSTS = frozenset(
    {
        "api.ipify.org",
        "api64.ipify.org",
        "icanhazip.com",
        "ifconfig.me",
        "checkip.amazonaws.com",
    }
)
ALLOWED_HOSTS = ("opencode.ai",) + tuple(sorted(HEALTH_HOSTS))
ALLOWED_PORT = 443
MAX_HEADER = 16 * 1024
CONNECT_TIMEOUT = 12.0
RELAY_CHUNK = 64 * 1024
LOGGER = logging.getLogger("public-egress-worker")

KEEP_ENV = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
)


def worker_env() -> dict[str, str]:
    """Environnement minimal du worker : aucun secret du proxy principal."""
    env = {key: os.environ[key] for key in KEEP_ENV if key in os.environ}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _host_allowed(host: str) -> bool:
    value = str(host or "").strip().lower().rstrip(".")
    if value == "opencode.ai" or value.endswith(".opencode.ai"):
        return True
    return value in HEALTH_HOSTS


@dataclass(frozen=True)
class Upstream:
    id: str
    url: str
    scheme: str
    host: str
    port: int
    username: str | None
    password: str | None

    @classmethod
    def parse(cls, upstream_id: str, url: str) -> Upstream:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https", "socks5", "socks5h"):
            raise ValueError(f"schéma upstream non supporté: {scheme!r}")
        host = parsed.hostname or ""
        port = int(parsed.port or (443 if scheme == "https" else 80))
        if not host or not (1 <= port <= 65535):
            raise ValueError(f"upstream invalide: {url!r}")
        return cls(
            id=upstream_id,
            url=url,
            scheme="socks5" if scheme == "socks5h" else scheme,
            host=host,
            port=port,
            username=urllib.parse.unquote(parsed.username) if parsed.username else None,
            password=urllib.parse.unquote(parsed.password) if parsed.password else None,
        )

    @property
    def auth_header(self) -> str | None:
        if not (self.username or self.password):
            return None
        raw = f"{self.username or ''}:{self.password or ''}".encode()
        return "Basic " + base64.b64encode(raw).decode()


def _basic_auth(value: str | None) -> tuple[str, str] | None:
    text = str(value or "").strip()
    if not text.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(text[6:].strip(), validate=True).decode("utf-8")
    except Exception:
        return None
    if ":" not in raw:
        return None
    upstream_id, token = raw.split(":", 1)
    return upstream_id, token


async def _read_headers(reader: asyncio.StreamReader) -> bytes:
    data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=CONNECT_TIMEOUT)
    if len(data) > MAX_HEADER:
        raise ValueError("headers trop volumineux")
    return data


def _parse_connect(data: bytes) -> tuple[str, int, dict[str, str]]:
    text = data.decode("iso-8859-1", "replace")
    lines = text.split("\r\n")
    if not lines or not lines[0].startswith("CONNECT "):
        raise ValueError("méthode non supportée")
    parts = lines[0].split()
    if len(parts) < 2:
        raise ValueError("requête CONNECT invalide")
    target = parts[1]
    if target.startswith("["):
        raise ValueError("cible IPv6 non supportée")
    host, _, port_raw = target.rpartition(":")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError("port invalide") from exc
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return host, port, headers


async def _socks5_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    upstream: Upstream,
    host: str,
    port: int,
) -> None:
    need_auth = bool(upstream.username or upstream.password)
    methods = b"\x00\x02" if need_auth else b"\x00"
    writer.write(b"\x05" + bytes([len(methods)]) + methods)
    await writer.drain()
    head = await reader.readexactly(2)
    if head[0] != 5 or head[1] == 0xFF:
        raise ConnectionError("SOCKS5: aucune méthode acceptée")
    if head[1] == 2:
        user = (upstream.username or "").encode()
        pwd = (upstream.password or "").encode()
        if len(user) > 255 or len(pwd) > 255:
            raise ValueError("SOCKS5: credentials trop longs")
        writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
        await writer.drain()
        auth = await reader.readexactly(2)
        if auth[1] != 0:
            raise ConnectionError("SOCKS5: authentification refusée")
    host_bytes = host.encode("idna")
    writer.write(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + int(port).to_bytes(2, "big"))
    await writer.drain()
    reply = await reader.readexactly(4)
    if reply[1] != 0:
        raise ConnectionError(f"SOCKS5: CONNECT refusé ({reply[1]})")
    atyp = reply[3]
    if atyp == 1:
        await reader.readexactly(4)
    elif atyp == 3:
        length = (await reader.readexactly(1))[0]
        await reader.readexactly(length)
    elif atyp == 4:
        await reader.readexactly(16)
    else:
        raise ConnectionError("SOCKS5: réponse invalide")
    await reader.readexactly(2)


async def _http_tunnel(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    upstream: Upstream,
    host: str,
    port: int,
) -> None:
    target = f"{host}:{port}"
    lines = [
        f"CONNECT {target} HTTP/1.1",
        f"Host: {target}",
        "Proxy-Connection: keep-alive",
    ]
    auth = upstream.auth_header
    if auth:
        lines.append(f"Proxy-Authorization: {auth}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1"))
    await writer.drain()
    response = await _read_headers(reader)
    first = response.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
    parts = first.split()
    if len(parts) < 2 or parts[1] != "200":
        raise ConnectionError(f"upstream CONNECT refusé: {first[:120]}")


async def _open_upstream(upstream: Upstream, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(upstream.host, upstream.port),
        timeout=CONNECT_TIMEOUT,
    )
    try:
        if upstream.scheme == "https":
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            await writer.start_tls(context, server_hostname=upstream.host)
        if upstream.scheme in ("http", "https"):
            await _http_tunnel(reader, writer, upstream, host, port)
        else:
            await _socks5_connect(reader, writer, upstream, host, port)
    except BaseException:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        raise
    return reader, writer


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    total = 0
    try:
        while True:
            chunk = await reader.read(RELAY_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
    return total


class PublicEgressWorker:
    """Proxy CONNECT local qui relaie vers un pool de proxys publics."""

    def __init__(
        self,
        upstreams: dict[str, str],
        token: str,
        *,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
        max_connections: int = 128,
    ) -> None:
        if not token:
            raise ValueError("token requis")
        self.upstreams = {
            str(upstream_id): Upstream.parse(str(upstream_id), str(url)) for upstream_id, url in upstreams.items()
        }
        if not self.upstreams:
            raise ValueError("au moins un upstream est requis")
        self.token = token
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.max_connections = max(1, int(max_connections))
        self._server: asyncio.AbstractServer | None = None
        self._connections = 0

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            return 0
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> int:
        self._server = await asyncio.start_server(
            self._handle,
            host=self.listen_host,
            port=self.listen_port,
        )
        return self.port

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def local_proxy_url(self, upstream_id: str) -> str:
        if upstream_id not in self.upstreams:
            raise KeyError(upstream_id)
        user = urllib.parse.quote(upstream_id, safe="")
        token = urllib.parse.quote(self.token, safe="")
        return f"http://{user}:{token}@{self.listen_host}:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._connections >= self.max_connections:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            with contextlib.suppress(Exception):
                await writer.drain()
                writer.close()
            return
        self._connections += 1
        upstream_writer: asyncio.StreamWriter | None = None
        tunnel_open = False
        try:
            data = await _read_headers(reader)
            if data.startswith(b"GET /health "):
                body = b'{"ok":true}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 11\r\n"
                    b"Connection: close\r\n\r\n" + body
                )
                await writer.drain()
                return
            host, port, headers = _parse_connect(data)
            auth = _basic_auth(headers.get("proxy-authorization"))
            if auth is None or not hmac.compare_digest(auth[1], self.token):
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            upstream = self.upstreams.get(auth[0])
            if upstream is None:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            if port != ALLOWED_PORT or not _host_allowed(host):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            up_reader, upstream_writer = await _open_upstream(upstream, host, port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            tunnel_open = True
            sent, received = await asyncio.gather(
                _pipe(reader, upstream_writer),
                _pipe(up_reader, writer),
            )
            LOGGER.info(
                "relay upstream=%s target=%s:%s sent=%s received=%s",
                upstream.id,
                host,
                port,
                sent,
                received,
            )
        except Exception as exc:
            LOGGER.debug("connection rejected: %s", exc)
            if not tunnel_open:
                with contextlib.suppress(Exception):
                    if not writer.is_closing():
                        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                        await writer.drain()
        finally:
            self._connections -= 1
            if upstream_writer is not None:
                with contextlib.suppress(Exception):
                    upstream_writer.close()
            with contextlib.suppress(Exception):
                writer.close()


@dataclass
class WorkerHandle:
    process: subprocess.Popen[str]
    port: int
    token: str
    config_path: Path
    port_path: Path

    def local_proxy_url(self, upstream_id: str) -> str:
        user = urllib.parse.quote(upstream_id, safe="")
        token = urllib.parse.quote(self.token, safe="")
        return f"http://{user}:{token}@127.0.0.1:{self.port}"

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.process.terminate()
            self.process.wait(timeout=5)
        with contextlib.suppress(Exception):
            self.process.kill()
        for path in (self.config_path, self.port_path):
            with contextlib.suppress(OSError):
                path.unlink()
        with contextlib.suppress(OSError):
            self.config_path.parent.rmdir()


def start_worker_process(
    upstreams: dict[str, str],
    *,
    token: str | None = None,
    timeout_s: float = 8.0,
) -> WorkerHandle:
    """Lance le worker dans un sous-processus à l'environnement scrubbé."""
    if not upstreams:
        raise ValueError("aucun upstream")
    token = token or secrets.token_urlsafe(24)
    tmpdir = Path(tempfile.mkdtemp(prefix="oc-public-egress-"))
    config_path = tmpdir / "config.json"
    port_path = tmpdir / "port.json"
    config_path.write_text(
        json.dumps(
            {
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "token": token,
                "upstreams": upstreams,
                "port_file": str(port_path),
            }
        ),
        encoding="utf-8",
    )
    flags = 0x08000000 if os.name == "nt" else 0
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(Path(__file__).resolve()),
            "--config",
            str(config_path),
        ],
        env=worker_env(),
        cwd=str(tmpdir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        text=True,
    )
    deadline = time.monotonic() + max(1.0, timeout_s)
    port = 0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if port_path.exists():
            try:
                port = int(json.loads(port_path.read_text(encoding="utf-8")).get("port") or 0)
            except Exception:
                port = 0
            if port:
                break
        time.sleep(0.05)
    if not port:
        with contextlib.suppress(Exception):
            process.terminate()
            process.wait(timeout=2)
        for path in (config_path, port_path):
            with contextlib.suppress(OSError):
                path.unlink()
        with contextlib.suppress(OSError):
            tmpdir.rmdir()
        raise RuntimeError("le worker public-egress n'a pas démarré")
    with contextlib.suppress(OSError):
        config_path.unlink()
    return WorkerHandle(process=process, port=port, token=token, config_path=config_path, port_path=port_path)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def run() -> None:
        worker = PublicEgressWorker(
            config.get("upstreams") or {},
            str(config.get("token") or ""),
            listen_host=str(config.get("listen_host") or "127.0.0.1"),
            listen_port=int(config.get("listen_port") or 0),
        )
        port = await worker.start()
        port_file = str(config.get("port_file") or "").strip()
        if port_file:
            Path(port_file).write_text(json.dumps({"port": port}), encoding="utf-8")
        server = worker._server
        assert server is not None
        LOGGER.info("listening on %s upstreams=%s", port, len(worker.upstreams))
        async with server:
            await server.serve_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
