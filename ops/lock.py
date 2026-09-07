"""ops.lock — verrou mono-instance (Phase 8 refonte).

Déplacement PUR depuis ``opencode.py`` (§ « Mono-instance lock [CRITIC(7)] »).
AUCUN import du projet : ``log_fn`` / ``debug_fn`` injectés (l'hôte passe
``dashboard.display.log/debug`` — le message FATAL stderr reste
inconditionnel : le panneau Rich n'existe jamais quand on quitte ici).

La liste ``_INSTANCE_LOCK_FDS`` vit ici (les fds doivent rester référencés :
le GC les fermerait, relâchant le verrou).

Compat : l'hôte fait ``from ops.lock import acquire_instance_lock as
_acquire_instance_lock`` (IMPORT, pas wrapper — ``inspect.getsource``
doit voir le message FATAL figé, contrat ``test_phase0_contracts.py``).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

_DEFAULT_LOCK_PATH = os.path.join("logs", "opencode.lock")

_INSTANCE_LOCK_FDS: list[int] = []  # keep fds referenced: GC closing them would release the lock


def _noop(*args, **kwargs) -> None:
    return None


def acquire_instance_lock(
    lock_path: str = _DEFAULT_LOCK_PATH,
    *,
    log_fn: Callable[..., None] | None = None,
    debug_fn: Callable[..., None] | None = None,
) -> None:
    """Take a non-blocking exclusive file lock; exit if another instance holds it.

    [CRITIC(7)] Two proxy instances would fight over port 4000, the rotation
    machinery and the SQLite DB. The lock file is advisory — one lock per
    host — so a second `python opencode.py` exits immediately with a clear
    message instead of corrupting state.
    """
    _log = log_fn or _noop
    _debug = debug_fn or _noop
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        _log(f"WARNING: cannot create lock file {lock_path}: {e} — continuing without mono-instance guard")
        return
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # posix only — branche morte sous Windows, mypy ok

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
    except OSError:
        # stderr print: the Rich log panel is never built when we exit here,
        # so this is the only place the user sees why the instance refused to start.
        print(
            f"FATAL: another opencode-proxy instance is already running (lock held: {lock_path})",
            file=sys.stderr,
            flush=True,
        )
        _log(f"FATAL: another opencode-proxy instance is already running (lock held: {lock_path})")
        try:
            os.close(fd)
        except OSError:
            pass
        sys.exit(1)
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
    except OSError:
        pass
    _INSTANCE_LOCK_FDS.append(fd)
    _debug(f"  [lock] instance lock acquired: {lock_path}")


__all__ = ["_INSTANCE_LOCK_FDS", "acquire_instance_lock"]
