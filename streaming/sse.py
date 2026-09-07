"""streaming.sse — pompe SSE ping + coalesce (Phase 7 refonte).

Déplacement PUR depuis ``opencode.py`` (``_sse_pump``). asyncio seul :
AUCUN import du projet, ``ping_interval`` / ``coalesce_max`` en paramètres
(l'hôte lit ``streaming.sse_keepalive_interval`` depuis ``config.yaml`` et
les passe — y compris via ses wrappers ``_sse_keepalive`` /
``_sse_coalesce`` conservés pour les tests).
"""

from __future__ import annotations

import asyncio

DEFAULT_PING_INTERVAL = 15.0  # s, même défaut que config.yaml
DEFAULT_COALESCE_MAX = 64 * 1024  # plafond micro-batch (B3)


async def sse_pump(stream, *, ping_interval: float = DEFAULT_PING_INTERVAL, coalesce_max: int = DEFAULT_COALESCE_MAX):
    """[P4.5 perf] Pompe SSE fusionnée — remplace _sse_keepalive+_sse_coalesce séparés.

    Un seul `read_task` + timer idle réarmé par `asyncio.Event` (B2) + drain
    non-bloquant après le 1er chunk, ≤1 `sleep(0)`/groupe, plafond 64 KiB (B3).
    Priorité read > ping (ordre contractuel), retour propre sur Exception
    upstream, `finally` cancel pending/read. Ne touche PAS à
    `_CurlCffiStreamResponse.aiter_lines` [41] — uniquement la pompe d'émission.

    `ping_interval` falsy → pas de pings, `coalesce_max` falsy → pas de coalesce.
    """
    # normalise en aiter
    try:
        aiter = stream.__aiter__()
    except AttributeError:
        aiter = stream
    read_task = None
    timer_task = None
    pending = None
    activity = asyncio.Event()

    async def _idle_timer():
        while True:
            activity.clear()
            try:
                await asyncio.wait_for(activity.wait(), timeout=ping_interval)
            except TimeoutError:
                return

    try:
        while True:
            if pending is not None:
                read_task = pending
                pending = None
            elif read_task is None or read_task.done():
                read_task = asyncio.ensure_future(anext(aiter))
            # timer seulement si ping activé
            if ping_interval and ping_interval > 0:
                if timer_task is None or timer_task.done():
                    timer_task = asyncio.ensure_future(_idle_timer())
                wait_tasks = {read_task, timer_task}
            else:
                wait_tasks = {read_task}
                timer_task = None
            done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
            if read_task in done:
                activity.set()
                try:
                    first = read_task.result()
                except StopAsyncIteration:
                    return
                except Exception:
                    return
                read_task = None
                # coalesce si activé et bytes
                if coalesce_max and coalesce_max > 0 and isinstance(first, (bytes, bytearray)):
                    groups = [first]
                    size = len(first)
                    exhausted = False
                    while size < coalesce_max:
                        nxt = asyncio.ensure_future(anext(aiter))
                        await asyncio.sleep(0)
                        if not nxt.done():
                            pending = nxt
                            break
                        try:
                            chunk = nxt.result()
                        except StopAsyncIteration:
                            exhausted = True
                            break
                        except Exception:
                            exhausted = True
                            break
                        if not isinstance(chunk, (bytes, bytearray)):
                            # flush bytes group puis yield non-bytes isolé
                            if groups:
                                yield b"".join(groups)
                                groups = []
                            yield chunk
                            break
                        groups.append(chunk)
                        size += len(chunk)
                    if groups:
                        yield b"".join(groups)
                    if exhausted:
                        return
                else:
                    yield first
            elif timer_task is not None and timer_task in done:
                timer_task = None
                yield b": ping\n\n"
    finally:
        if timer_task is not None and not timer_task.done():
            timer_task.cancel()
        if read_task is not None and not read_task.done():
            read_task.cancel()
        if pending is not None and not pending.done():
            pending.cancel()


# Alias historique (opencode._sse_pump).
_sse_pump = sse_pump

__all__ = ["DEFAULT_COALESCE_MAX", "DEFAULT_PING_INTERVAL", "sse_pump", "_sse_pump"]
