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


def _notifier(on_error, exc) -> None:
    """[FIX cause n°1] Signale une erreur de lecture amont à l'hôte.

    Ne lève JAMAIS : un rappel défaillant ne doit pas casser la pompe.
    """
    if on_error is None:
        return
    try:
        on_error(exc)
    except Exception:
        pass


async def sse_pump(
    stream,
    *,
    ping_interval: float = DEFAULT_PING_INTERVAL,
    coalesce_max: int = DEFAULT_COALESCE_MAX,
    on_error=None,
    error_event: bytes | None = None,
    idle_timeout: float | None = None,
):
    """[P4.5 perf] Pompe SSE fusionnée — remplace _sse_keepalive+_sse_coalesce séparés.

    Un seul `read_task` + timer idle réarmé par `asyncio.Event` (B2) + drain
    non-bloquant après le 1er chunk, ≤1 `sleep(0)`/groupe, plafond 64 KiB (B3).
    Priorité read > ping (ordre contractuel), retour propre sur Exception
    upstream, `finally` cancel pending/read. Ne touche PAS à
    `_CurlCffiStreamResponse.aiter_lines` [41] — uniquement la pompe d'émission.

    [FIX cause n°1] Une exception de lecture amont n'est PLUS avalée en silence
    (l'ancien `return` muet était indiscernable d'une fin normale : ni erreur
    protocolaire pour le client, ni trace dans les logs). Désormais
    ``on_error(exc)`` est appelé (journalisation/observabilité) puis
    ``error_event`` — un blob SSE déjà formaté par l'hôte pour le protocole du
    client (Anthropic ``event: error`` / OpenAI ``data: {"error": …}``) — est émis
    VERBATIM avant la fermeture. La pompe reste protocol-agnostique.

    [FIX gel silencieux] ``idle_timeout`` borne l'attente d'un octet amont. Sans lui,
    un amont qui ouvre la connexion puis se tait (tunnel mort, fournisseur figé) est
    attendu jusqu'au timeout de lecture TCP — **600 s** dans ``config.yaml`` — pendant
    que la pompe continue d'envoyer des ``: ping`` toutes les 15 s. Le client voit
    donc un flux vivant mais VIDE, sans erreur ni log : « ça s'arrête d'un coup ».
    Au-delà de ``idle_timeout`` sans le moindre octet amont, on notifie l'hôte et on
    émet ``error_event``. ``None`` (défaut) désactive la borne : le comportement des
    appelants existants est inchangé.

    `ping_interval` falsy → pas de pings, `coalesce_max` falsy → pas de coalesce.
    """
    # normalise en aiter
    try:
        aiter = stream.__aiter__()
    except AttributeError:
        aiter = stream
    read_task = None
    timer_task = None
    idle_task = None
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
            # [FIX gel silencieux] borne d'inactivité amont (indépendante du ping)
            if idle_timeout and idle_timeout > 0:
                if idle_task is None or idle_task.done():
                    idle_task = asyncio.ensure_future(asyncio.sleep(idle_timeout))
                wait_tasks.add(idle_task)
            done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
            if read_task in done:
                activity.set()
                if idle_task is not None and not idle_task.done():
                    # un octet amont vient d'arriver : la borne repart de zéro
                    idle_task.cancel()
                idle_task = None
                try:
                    first = read_task.result()
                except StopAsyncIteration:
                    return
                except Exception as exc:
                    # [FIX cause n°1] Ne JAMAIS avaler une erreur de lecture amont.
                    _notifier(on_error, exc)
                    if error_event:
                        yield error_event
                    return
                read_task = None
                # coalesce si activé et bytes
                if coalesce_max and coalesce_max > 0 and isinstance(first, (bytes, bytearray)):
                    groups = [first]
                    size = len(first)
                    exhausted = False
                    _abort = False
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
                        except Exception as exc:
                            # [FIX cause n°1] Erreur remontée, PAS avalée. L'émission de
                            # l'erreur protocolaire est DIFFÉRÉE après le flush du groupe
                            # déjà accumulé : l'émettre ici inverserait l'ordre (erreur
                            # avant le contenu) — inversion constatée par test.
                            _notifier(on_error, exc)
                            _abort = True
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
                    if _abort and error_event:
                        # émis APRÈS le contenu déjà accumulé (ordre protocolaire)
                        yield error_event
                    if exhausted:
                        return
                else:
                    yield first
            elif idle_task is not None and idle_task in done:
                # [FIX gel silencieux] Aucun octet amont depuis ``idle_timeout``. On ne
                # laisse PAS le client suspendu (pings toutes les 15 s) jusqu'aux 600 s
                # du timeout de lecture TCP : on émet une erreur explicite, traçable.
                _notifier(
                    on_error,
                    TimeoutError(f"aucun octet amont depuis {idle_timeout}s (flux gelé)"),
                )
                if error_event:
                    yield error_event
                return
            elif timer_task is not None and timer_task in done:
                timer_task = None
                yield b": ping\n\n"
    finally:
        if timer_task is not None and not timer_task.done():
            timer_task.cancel()
        if idle_task is not None and not idle_task.done():
            idle_task.cancel()
        if read_task is not None and not read_task.done():
            read_task.cancel()
        if pending is not None and not pending.done():
            pending.cancel()


# Alias historique (opencode._sse_pump).
_sse_pump = sse_pump

__all__ = ["DEFAULT_COALESCE_MAX", "DEFAULT_PING_INTERVAL", "sse_pump", "_sse_pump"]
