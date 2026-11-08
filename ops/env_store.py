"""Écriture atomique et sérialisée du fichier ``.env`` (feature 011).

Trois processus écrivaient ``.env`` en parallèle (manager, superviseur,
chaînes). Ce module fournit un point d'entrée unique :

- verrou inter-processus par fichier ``.env.lock`` (création exclusive) ;
- lecture/modification/écriture sous verrou ;
- fichier temporaire unique + ``os.replace`` atomique ;
- purge d'un verrou stale après 30 s.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_SUFFIX = ".lock"
STALE_LOCK_S = 30.0


@contextmanager
def _env_lock(path: Path) -> Iterator[None]:
    lock = path.with_name(path.name + LOCK_SUFFIX)
    deadline = time.monotonic() + 10.0
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                if age > STALE_LOCK_S:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"env lock timeout: {lock}") from None
            time.sleep(0.02)
        except PermissionError:
            # Windows : entre le close() et l'unlink() du détenteur, le fichier
            # de verrou est en état « delete pending » ; O_CREAT|O_EXCL répond
            # alors PermissionError au lieu de FileExistsError. Ce n'est pas
            # une erreur de droits : on réessaie jusqu'à la deadline.
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def read_env(path: str | Path) -> dict[str, str]:
    target = Path(path)
    out: dict[str, str] = {}
    try:
        for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return out


def update_env(path: str | Path, updates: dict[str, str]) -> None:
    """Met à jour ``updates`` dans ``.env`` de façon atomique et sérialisée."""
    target = Path(path)
    if not updates:
        return
    with _env_lock(target):
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        seen: set[str] = set()
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            key = stripped.split("=", 1)[0] if "=" in stripped else ""
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                out.append(line)
        for key, value in updates.items():
            if key not in seen:
                out.append(f"{key}={value}")
        tmp = target.with_name(
            f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8", newline="")
        # Windows : un antivirus / indexeur peut ouvrir la cible brièvement,
        # ce qui fait échouer os.replace avec PermissionError. Sans retry, la
        # mise à jour entière est perdue (clé absente du .env final).
        last_exc: OSError | None = None
        for attempt in range(5):
            try:
                os.replace(tmp, target)
                last_exc = None
                break
            except OSError as exc:
                last_exc = exc
                time.sleep(0.05 * (attempt + 1))
        if last_exc is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise last_exc


def replace_env_line(path: str | Path, key: str, value: str) -> None:
    update_env(path, {key: value})
