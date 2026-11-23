"""[T027] Écrivain unique pour config.yaml / .env.

Toutes les écritures runtime passent par ce module : verrou thread
(RLock) + verrou fichier cross-process (portalocker), écriture atomique
tmp+fsync+replace avec retry Windows, suppression des no-op (contenu
identique => pas de réécriture), journalisation de chaque écriture réelle
avec sa raison.

Avant T027, quatre écrivains coexistaient (settings.save_yaml_config,
dashboard._persist_vpn_config avec fallback non atomique,
_persist_free_model_map sans verrou, settings.save_env sans atomicité)
et réécrivaient le fichier toutes les 1-2 min même sans changement.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger("config.store")


class ConfigLockError(RuntimeError):
    """Le verrou cross-process est indisponible — écriture refusée (fail-closed).

    Exception dédiée pour que les appelants qui ont un contrat d'erreur
    (ex. ``dashboard._persist_vpn_config`` retourne une STRING) puissent la
    traduire, au lieu de laisser fuiter une ``OSError`` brute.
    """


_thread_lock = threading.RLock()
_local = threading.local()
_LOCK_TIMEOUT_S = 10.0
_REPLACE_ATTEMPTS = 5


def _held(path: str) -> bool:
    held = getattr(_local, "held", None)
    return held is not None and path in held


@contextmanager
def transaction(path: str):
    """Verrou cross-process + thread pour un cycle read-modify-write.

    Réentrant par thread : les appels imbriqués (yaml_set ->
    save_yaml_config -> write_yaml) réutilisent l'acquisition au lieu de
    deadlocker sur un second handle vers le même .lock (Windows : verrous
    par handle). Le fichier .lock n'est jamais supprimé (sa suppression
    casserait le verrou pour les autres processus en attente).
    """
    path = os.path.abspath(path)
    with _thread_lock:
        if _held(path):
            yield
            return
        lock = None
        try:
            import portalocker

            # [T027-fix] portalocker.lock(file, flags) n'accepte PAS de
            # timeout (3.2.0) : le kwarg levait TypeError, avalé ci-dessous,
            # et toute écriture passait sans verrou cross-process (constaté
            # en live le 2026-11-16 21:39Z). La classe Lock gère le timeout,
            # le retry (check_interval) et son propre handle.
            lock = portalocker.Lock(
                path + ".lock",
                mode="a",
                timeout=_LOCK_TIMEOUT_S,
                check_interval=0.05,
                fail_when_locked=False,
            )
            lock.acquire()
        except ImportError:
            lock = None
        except Exception as exc:
            # [fix 2026-11-21] Fail-CLOSED. Avant, toute erreur d'acquisition
            # (timeout 10 s, OSError, LockException) etait avalee et le cycle
            # read-modify-write continuait avec le seul verrou thread : deux
            # processus pouvaient s'entrelacer et perdre des mises a jour —
            # exactement la regression que T027 devait empecher. Refuser
            # l'ecriture est preferable a une ecriture non protegee.
            logger.warning(
                "[store] verrou %s indisponible (%s) — écriture refusée "
                "(fail-closed, pas de verrou cross-process)",
                os.path.basename(path),
                exc,
            )
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass
            raise ConfigLockError(
                f"verrou {os.path.basename(path)} indisponible: {exc}"
            ) from exc
        held = getattr(_local, "held", None)
        if held is None:
            held = set()
            _local.held = held
        held.add(path)
        try:
            yield
        finally:
            held.discard(path)
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass


def atomic_write(path: str, text: str, *, reason: str) -> bool:
    """Écrit text atomiquement ; False si contenu identique (pas d'écriture)."""
    path = os.path.abspath(path)
    with transaction(path):
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == text:
                    logger.debug(
                        "[store] %s inchangé (%s) — écriture supprimée",
                        os.path.basename(path),
                        reason,
                    )
                    return False
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("[store] lecture %s: %s", os.path.basename(path), exc)
        dir_name = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=dir_name, prefix=f".{os.path.basename(path)}.tmp.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            last: OSError | None = None
            for attempt in range(_REPLACE_ATTEMPTS):
                try:
                    os.replace(tmp, path)
                    last = None
                    break
                except OSError as exc:
                    last = exc
                    time.sleep(0.1 * (attempt + 1))
            if last is not None:
                raise last
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        logger.info(
            "[store] %s écrit (%s, %d octets)",
            os.path.basename(path),
            reason,
            len(text.encode("utf-8")),
        )
        return True


def write_yaml(path: str, data: dict, *, reason: str) -> bool:
    import yaml

    buf = io.StringIO()
    yaml.dump(data, buf, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return atomic_write(path, buf.getvalue(), reason=reason)


def read_yaml(path: str) -> dict:
    import yaml

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def upsert_env(path: str, updates: dict, *, reason: str) -> bool:
    """Upsert ligne par ligne : préserve commentaires, ordre et lignes vides.

    [T027-fix] La LECTURE est dans la même transaction que l'écriture.
    Lire hors verrou puis écrire sous verrou (version initiale) perdait les
    clés des écrivains concurrents : 20 threads upsertant chacun une clé
    produisaient 20 fichiers de 12-14 octets ne contenant que BASE + une
    seule clé (test_concurrent_updates_do_not_lose_keys, 2026-11-16 22:1xZ).
    Le verrou doit couvrir tout le cycle read-modify-write, pas seulement
    le replace. transaction() est réentrant par thread : l'atomic_write
    imbriqué réutilise l'acquisition au lieu de deadlocker.
    """
    remaining = {}
    for k, v in updates.items():
        key, value = str(k), str(v)
        # [fix 2026-11-21] La relecture utilise str.splitlines() et la
        # reecriture "\n".join(). splitlines() coupe AUSSI sur \v \f
        # \x1c-\x1e \x85 U+2028 U+2029 : un caractere de ce jeu passait la
        # garde \n/\r, puis devenait une VRAIE ligne KEY=value au prochain
        # upsert (injection/corruption du .env). La cle est validee aussi.
        if value and value.splitlines() != [value]:
            raise ValueError(f"valeur multi-ligne interdite pour {key}")
        if not key.isascii() or not key.isidentifier():
            raise ValueError(f"nom de variable invalide: {key!r}")
        remaining[key] = value
    path = os.path.abspath(path)
    with transaction(path):
        lines: list[str] = []
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            pass
        out: list[str] = []
        seen: set[str] = set()
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in remaining:
                    out.append(f"{key}={remaining[key]}")
                    seen.add(key)
                    continue
            out.append(line)
        for key, value in remaining.items():
            if key not in seen:
                out.append(f"{key}={value}")
        text = "\n".join(out) + "\n"
        return atomic_write(path, text, reason=reason)
