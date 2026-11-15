"""Escalade immédiate quand le socket d'écoute meurt sur Windows.

Le superviseur d'app reste le propriétaire du cycle de vie. Ce guard réduit
la fenêtre d'indisponibilité : dès que la boucle asyncio perd son socket
d'écoute (`WinError 64`, `Accept failed on a socket`), le processus sort
volontairement au lieu de rester vivant mais muet. Le superviseur le relance
en ~2 s.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

logger = logging.getLogger("socket_guard")

_INSTALLED = False
_PATCHED = False
_RESTART_CALLBACK = None
_RESTART_LOCK = threading.Lock()
_RESTART_TIMES: list[float] = []
_MAX_RESTARTS_PER_MIN = 3


def set_restart_callback(callback) -> None:
    """Callback in-process qui recrée le serveur (ex. ServerManager.restart)."""

    global _RESTART_CALLBACK
    _RESTART_CALLBACK = callback


def _restart() -> None:
    global _RESTART_CALLBACK
    now = time.time()
    with _RESTART_LOCK:
        _RESTART_TIMES[:] = [t for t in _RESTART_TIMES if now - t < 60.0]
        if len(_RESTART_TIMES) >= _MAX_RESTARTS_PER_MIN:
            logger.error(
                "socket guard: %d restarts en 60 s — sortie pour le superviseur",
                len(_RESTART_TIMES),
            )
            os._exit(1)
        _RESTART_TIMES.append(now)
    callback = _RESTART_CALLBACK
    if callback is None:
        logger.error("socket guard: aucun restart in-process — sortie")
        os._exit(1)
    try:
        callback()
    except Exception:
        logger.exception("socket guard: restart in-process échoué — sortie")
        os._exit(1)


def _is_listen_failure(context: dict) -> bool:
    exc = context.get("exception")
    message = str(context.get("message") or "")
    if "Accept failed" in message:
        return True
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) == 64:
            return True
        # 10038 = socket not a socket, 10054 = connection reset by peer on accept
        if getattr(exc, "errno", None) in (64, 10038, 10054):
            return True
    return False


def _handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    if _is_listen_failure(context):
        logger.error(
            "socket d'écoute perdu (WinError 64) — restart in-process"
        )
        threading.Thread(
            target=_restart, name="socket-guard-restart", daemon=True
        ).start()
        return
    loop.default_exception_handler(context)


def install(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Installe le handler sur la boucle asyncio donnée (idempotent)."""

    global _INSTALLED
    if _INSTALLED:
        return
    target = loop or asyncio.get_event_loop()
    target.set_exception_handler(_handler)
    _INSTALLED = True


def install_server_patch() -> None:
    """Installe le guard dans la boucle interne d'uvicorn.

    ``uvicorn.Server.serve`` crée sa propre boucle via ``asyncio.run`` : un
    handler posé sur la boucle principale ne verrait pas l'erreur ``accept``.
    On enveloppe ``serve`` pour poser le handler dans la bonne boucle.
    """

    global _PATCHED
    if _PATCHED:
        return
    try:
        from uvicorn import Server
    except Exception:
        return
    original = Server.serve

    async def serve(self, sockets=None):  # type: ignore[no-untyped-def]
        install(asyncio.get_running_loop())
        return await original(self, sockets)

    Server.serve = serve  # type: ignore[method-assign]
    _PATCHED = True
