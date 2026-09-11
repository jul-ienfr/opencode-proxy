#!/usr/bin/env python3
"""precompress.py — pré-compression des assets statiques AU BUILD (Phase 5 boot).

Génère ``<asset>.gz`` (gzip -9) et ``<asset>.br`` (brotli --best) à côté de
chaque ``.js`` / ``.css`` de ``static/``. ``dashboard/routes/static.py`` les
sert ensuite directement, ce qui :

* retire la compression zlib du chemin de démarrage (~17 ms mesurés pour
  250 Ko de JS) — le plan boot demandait « plus au runtime » ;
* divise la charge transférée (brotli ≈ −20 % vs gzip sur du JS) ;
* rend le service indépendant de la charge CPU de la machine au boot.

Idempotent : un fichier déjà pré-compressé et plus récent que sa source est
laissé intact (mtime préservé) — relancer le script n'invalide pas les caches
HTTP ni ne réécrit inutilement le disque.

Usage :
    python scripts/precompress.py                 # static/ du projet
    python scripts/precompress.py --check         # CI : échoue si périmé
    python scripts/precompress.py --dir autre/    # autre dossier

Sortie : code 1 en mode --check si un asset manque ou est périmé.
"""

from __future__ import annotations

import argparse
import os
import sys
import zlib

# Console Windows en cp1252 : les flèches/caractères typographiques font
# planter `print` (UnicodeEncodeError mesuré). Sortie volontairement ASCII.
for _stream in (sys.stdout, sys.stderr):
    _reconf = getattr(_stream, "reconfigure", None)
    if _reconf is not None:
        try:
            _reconf(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(ROOT, "static")
# Aligné sur dashboard/routes/static._COMPRESSIBLE_EXT : on ne génère que ce
# que le middleware consomme réellement (index.html est servi depuis la
# mémoire par la route « / », pas par /static/).
EXTENSIONS = (".js", ".css")

# Seuil : en dessous, la compression coûte plus (en-têtes, décompression) que
# ce qu'elle fait gagner. Aligné sur GZipMiddleware(minimum_size=500) du plan.
MIN_SIZE = 500


def _gzip_bytes(raw: bytes) -> bytes:
    co = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return co.compress(raw) + co.flush()


def _brotli_bytes(raw: bytes) -> bytes | None:
    try:
        # `brotli` (bindings Google) puis `brotlicffi` en repli.
        try:
            import brotli

            return brotli.compress(raw, quality=11)
        except ImportError:
            import brotlicffi

            return brotlicffi.compress(raw, quality=11)
    except Exception:
        return None


def _is_fresh(pre_path: str, src_path: str) -> bool:
    try:
        return os.path.getmtime(pre_path) >= os.path.getmtime(src_path)
    except OSError:
        return False


def precompress_dir(static_dir: str, *, check: bool = False, quiet: bool = False) -> int:
    """Pré-compresse les assets du dossier. Retourne le nombre de problèmes."""
    if not os.path.isdir(static_dir):
        print(f"[precompress] dossier introuvable: {static_dir}", file=sys.stderr)
        return 1

    problems = 0
    done = 0
    skipped = 0
    for name in sorted(os.listdir(static_dir)):
        if not name.endswith(EXTENSIONS):
            continue
        src_path = os.path.join(static_dir, name)
        if not os.path.isfile(src_path):
            continue
        try:
            with open(src_path, "rb") as f:
                raw = f.read()
        except OSError as e:
            print(f"[precompress] lecture impossible {name}: {e}", file=sys.stderr)
            problems += 1
            continue
        if len(raw) < MIN_SIZE:
            if not quiet:
                print(f"[precompress] {name}: {len(raw)} o < {MIN_SIZE} -> ignore")
            skipped += 1
            continue

        gz_path = src_path + ".gz"
        br_path = src_path + ".br"

        targets = [("gz", gz_path, _gzip_bytes(raw))]
        br = _brotli_bytes(raw)
        if br is not None:
            targets.append(("br", br_path, br))

        for label, path, payload in targets:
            if _is_fresh(path, src_path):
                skipped += 1
                continue
            if check:
                print(
                    f"[precompress] PERIME: {os.path.relpath(path, ROOT)} (relancer scripts/precompress.py)",
                    file=sys.stderr,
                )
                problems += 1
                continue
            tmp = path + ".tmp"
            try:
                with open(tmp, "wb") as f:
                    f.write(payload)
                os.replace(tmp, path)
            except OSError as e:
                print(f"[precompress] écriture impossible {path}: {e}", file=sys.stderr)
                problems += 1
                continue
            done += 1
            if not quiet:
                ratio = (1 - len(payload) / len(raw)) * 100 if raw else 0
                print(f"[precompress] {name}.{label}: {len(raw)} -> {len(payload)} o (-{ratio:.0f} %)")

    if not quiet:
        print(f"[precompress] {done} ecrit(s), {skipped} deja a jour/ignore(s)")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", default=DEFAULT_DIR, help=f"dossier assets (défaut: {DEFAULT_DIR})")
    parser.add_argument(
        "--check",
        action="store_true",
        help="ne rien écrire : échoue si un asset pré-compressé manque ou est périmé (CI)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="sortie minimale")
    args = parser.parse_args(argv)
    return precompress_dir(args.dir, check=args.check, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
