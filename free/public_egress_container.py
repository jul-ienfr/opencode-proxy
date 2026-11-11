"""Isolation conteneur du worker d'egress public.

Le conteneur ne monte ni le dépôt ni ``.env`` : uniquement le script worker et
un fichier de configuration contenant les URLs des proxys publics. Il tourne
en lecture seule, sans capacités, sans privilèges supplémentaires, avec un
utilisateur non-root et des limites mémoire/PID/CPU.

Ce module ne fait aucun appel réseau direct : il construit et pilote la
commande Docker. Les tests peuvent le mocker sans démon Docker.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import socket
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from core.process import run_hidden

DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_USER = "65534:65534"
INTERNAL_PORT = 8000
WORKER_SCRIPT = Path(__file__).resolve().parent / "public_egress_worker.py"


def _free_host_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port(port: int, timeout_s: float) -> bool:
    """Attend une vraie réponse du worker, pas seulement le proxy de port Docker."""
    deadline = time.monotonic() + max(0.5, timeout_s)
    request = b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.75) as sock:
                sock.settimeout(0.75)
                sock.sendall(request)
                chunks: list[bytes] = []
                while sum(len(chunk) for chunk in chunks) < 512:
                    chunk = sock.recv(256)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\r\n\r\n" in b"".join(chunks):
                        break
                if b" 200 " in b"".join(chunks):
                    return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


@dataclass
class ContainerWorkerHandle:
    container_id: str
    name: str
    port: int
    token: str
    config_dir: Path
    docker: str

    def local_proxy_url(self, upstream_id: str) -> str:
        user = urllib.parse.quote(upstream_id, safe="")
        token = urllib.parse.quote(self.token, safe="")
        return f"http://{user}:{token}@127.0.0.1:{self.port}"

    def is_alive(self) -> bool:
        try:
            result = run_hidden(
                [self.docker, "inspect", "-f", "{{.State.Running}}", self.container_id],
                timeout=5,
                check=False,
            )
            return result.returncode == 0 and result.stdout.strip().lower() == "true"
        except Exception:
            return False

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            run_hidden(
                [self.docker, "rm", "-f", self.container_id],
                timeout=15,
                check=False,
            )
        shutil.rmtree(self.config_dir, ignore_errors=True)


def cleanup_orphan_workers(*, docker: str | None = None, keep: set[str] | None = None) -> int:
    """Remove leftover ``oc-public-egress-*`` containers from a crash/restart.

    A full process restart loses the in-memory worker handles; their Docker
    containers survive and accumulate. At boot no current worker exists, so
    every matching container is an orphan.
    """

    binary = docker or shutil.which("docker")
    if not binary:
        return 0
    keep = set(keep or ())
    try:
        listed = run_hidden(
            [binary, "ps", "-aq", "--filter", "name=oc-public-egress-"],
            timeout=10,
            check=False,
        )
        ids = [line.strip() for line in str(listed.stdout or "").splitlines() if line.strip()]
    except Exception:
        return 0
    removed = 0
    for container_id in ids:
        if container_id in keep:
            continue
        try:
            result = run_hidden(
                [binary, "rm", "-f", container_id],
                timeout=15,
                check=False,
            )
        except Exception:
            continue
        if int(getattr(result, "returncode", 1) or 0) == 0:
            removed += 1
    return removed


def start_worker_container(
    upstreams: dict[str, str],
    *,
    token: str | None = None,
    image: str = DEFAULT_IMAGE,
    host_port: int | None = None,
    internal_port: int = INTERNAL_PORT,
    timeout_s: float = 20.0,
) -> ContainerWorkerHandle:
    """Lance le worker dans un conteneur Docker isolé."""
    if not upstreams:
        raise ValueError("aucun upstream")
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("docker introuvable dans le PATH")
    token = token or secrets.token_urlsafe(24)
    config_dir = Path(tempfile.mkdtemp(prefix="oc-public-egress-container-"))
    config_path = config_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "listen_host": "0.0.0.0",
                "listen_port": int(internal_port),
                "token": token,
                "upstreams": upstreams,
            }
        ),
        encoding="utf-8",
    )
    # Le conteneur tourne en uid 65534 : le fichier doit être lisible.
    with contextlib.suppress(OSError):
        os.chmod(config_dir, 0o755)
        os.chmod(config_path, 0o644)
    host_port = int(host_port or _free_host_port())
    name = f"oc-public-egress-{secrets.token_hex(6)}"
    command = [
        docker,
        "run",
        "--rm",
        "-d",
        "--name",
        name,
        "--read-only",
        "--label", "oc.public-egress=1",
        "--label", "oc.managed=1",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--user={DEFAULT_USER}",
        "--pids-limit=128",
        "--memory=256m",
        "--cpus=0.5",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
        "-p",
        f"127.0.0.1:{host_port}:{int(internal_port)}",
        "-v",
        f"{config_dir}:/config:ro",
        "-v",
        f"{WORKER_SCRIPT}:/app/public_egress_worker.py:ro",
        str(image),
        "python",
        "-I",
        "-B",
        "/app/public_egress_worker.py",
        "--config",
        "/config/config.json",
    ]
    try:
        result = run_hidden(
            command,
            timeout=timeout_s,
            check=False,
        )
    except Exception:
        shutil.rmtree(config_dir, ignore_errors=True)
        raise
    if result.returncode != 0:
        shutil.rmtree(config_dir, ignore_errors=True)
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"docker run a échoué: {detail}")
    container_id = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
    if not container_id:
        shutil.rmtree(config_dir, ignore_errors=True)
        raise RuntimeError("docker run n'a pas retourné de container id")
    handle = ContainerWorkerHandle(
        container_id=container_id,
        name=name,
        port=host_port,
        token=token,
        config_dir=config_dir,
        docker=docker,
    )
    if not _wait_port(host_port, timeout_s):
        handle.stop()
        raise RuntimeError("worker conteneur non joignable sur 127.0.0.1")
    return handle


__all__ = [
    "DEFAULT_IMAGE",
    "ContainerWorkerHandle",
    "start_worker_container",
]
