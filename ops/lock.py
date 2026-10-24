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


def _port_listening(host: str, port: int, timeout: float = 0.4) -> bool | None:
    """Le port répond-il ? ``None`` si la question n'a pas pu être posée."""
    try:
        import socket

        cible = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
        with socket.create_connection((cible, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def diagnose_holder(lock_path: str, *, host: str | None = None, port: int | None = None) -> list[str]:
    """Lignes de diagnostic quand le verrou est tenu. Jamais bloquant, jamais fatal.

    **Pourquoi ce diagnostic existe.** Un redémarrage qui sort `FATAL: another
    instance is already running` ne dit pas si l'autre instance *sert* quelque
    chose ou si c'est un zombie qui ne tient qu'un fichier. L'utilisateur se
    retrouve avec un proxy mort, un verrou tenu, et aucune action évidente.

    Deux cas, deux messages :

    * le port écoute → l'instance tourne, il suffit de l'utiliser (ou de la tuer) ;
    * le port n'écoute pas → verrou tenu par un processus qui ne sert plus rien :
      c'est un zombie, et le PID à arrêter est écrit dans le fichier.
    """
    lignes: list[str] = []
    pid = ""
    # Le fichier de verrou est tenu par le porteur : sous Windows, un nouveau
    # processus n'arrive pas toujours à le relire. On écrit donc le PID en
    # clair dans un fichier voisin, lisible par n'importe qui.
    try:
        with open(f"{lock_path}.pid", encoding="ascii", errors="replace") as handle:
            pid = (handle.read() or "").strip()
    except Exception:
        pid = ""
    if not pid:
        try:
            with open(lock_path, encoding="ascii", errors="replace") as handle:
                contenu = (handle.read() or "").strip()
            if contenu.startswith("pid="):
                pid = contenu[4:].strip()
        except Exception:
            pass
    if pid:
        lignes.append(f"       holder pid: {pid}")
    if host is None or port is None:
        return lignes
    ecoute = _port_listening(host, int(port))
    cible = f"{'127.0.0.1' if host in ('', '0.0.0.0', '::') else host}:{port}"
    if ecoute:
        lignes.append(f"       an instance IS serving on {cible} — use it, or restart it cleanly")
    else:
        lignes.append(
            f"       nothing is listening on {cible}: this lock holder is a zombie "
            "(it holds the file but serves nothing)"
        )
        if pid:
            lignes.append(f"       → kill {pid} (or delete {lock_path}) and retry")
        else:
            lignes.append(f"       → delete {lock_path} and retry")
    return lignes


def acquire_instance_lock(
    lock_path: str = _DEFAULT_LOCK_PATH,
    *,
    host: str | None = None,
    port: int | None = None,
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
        # [V8.6] Le message dit maintenant QUI tient le verrou et si cette
        # instance sert encore : un redémarrage ne doit pas laisser l'utilisateur
        # avec « already running » alors que le proxy ne répond plus.
        print(
            f"FATAL: another opencode-proxy instance is already running (lock held: {lock_path})",
            file=sys.stderr,
            flush=True,
        )
        for ligne in diagnose_holder(lock_path, host=host, port=port):
            print(ligne, file=sys.stderr, flush=True)
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
    # [V8.6] Copie lisible du PID : le fichier verrouillé n'est pas relisible
    # par un processus qui démarre et veut savoir qui le bloque.
    try:
        with open(f"{lock_path}.pid", "w", encoding="ascii") as _pid_file:
            _pid_file.write(str(os.getpid()))
    except OSError:
        pass
    _INSTANCE_LOCK_FDS.append(fd)
    _debug(f"  [lock] instance lock acquired: {lock_path}")


__all__ = ["_INSTANCE_LOCK_FDS", "acquire_instance_lock", "diagnose_holder"]
