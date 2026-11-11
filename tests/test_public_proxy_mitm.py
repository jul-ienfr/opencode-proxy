"""A public proxy that tries to terminate TLS must be rejected by the client."""

from __future__ import annotations

import socket
import ssl
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tls_helpers import write_self_signed_cert  # noqa: E402


def test_proxy_mitm_self_signed_is_rejected():
    pytest.importorskip("cryptography")
    pytest.importorskip("curl_cffi")
    from curl_cffi import requests

    with tempfile.TemporaryDirectory() as raw_tmp:
        cert_path, key_path = write_self_signed_cert(Path(raw_tmp), "opencode.ai")
        server = socket.socket()
        server.settimeout(5)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def serve() -> None:
            try:
                conn, _ = server.accept()
                conn.recv(4096)
                # Le proxy malveillant accepte le CONNECT puis tente de se faire
                # passer pour opencode.ai avec son propre certificat.
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(str(cert_path), str(key_path))
                tls = ctx.wrap_socket(conn, server_side=True)
                tls.recv(4096)
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                tls.close()
            except Exception:
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        proxy = f"http://127.0.0.1:{port}"
        try:
            with pytest.raises(Exception) as excinfo:
                with requests.Session(trust_env=False) as session:
                    session.get(
                        "https://opencode.ai/",
                        proxies={"http": proxy, "https": proxy},
                        timeout=5,
                    )
            message = str(excinfo.value).lower()
            assert "certificate" in message or "ssl" in message, message
        finally:
            server.close()
