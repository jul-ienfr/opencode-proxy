#!/usr/bin/env python3
"""purge_logs.py — purge des transients de diagnostic de ``logs/`` (P0-4).

Cible UNIQUEMENT les artefacts régénérables, déjà couverts par
``.gitignore`` (jamais versionnés) et jamais embarqués dans l'image
(``.dockerignore`` : ``logs/``) :

* ``logs/free400_*.json`` — captures de diagnostic free400 ;
* ``logs/_*.xml`` — sondes/captures jetables de conformité ;
* ``logs/_capture_real_provider.jsonl`` et ``logs/bench_*.jsonl`` ;
* ``logs/gate_*.log`` / vieux ``logs/debug.log.*`` au-delà de N gardés.

JAMAIS touchés : ``requests.db*``, ``debug.log`` courant, ``*.lock``,
``shared_rotation.json``, ``vpn_state*.json``, ``archive/``.

Usage :
    python scripts/purge_logs.py              # dry-run, montre le plan
    python scripts/purge_logs.py --apply       # exécute
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _reconf = getattr(_stream, "reconfigure", None)
    if _reconf is not None:
        try:
            _reconf(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"

PATTERNS = (
    "free400_*.json",
    "_*.xml",
    "_capture_real_provider.jsonl",
    "bench_*.jsonl",
    "gate_*.log",
)


def _human(n: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} To"


def collect() -> list[Path]:
    found: list[Path] = []
    if not LOG_DIR.is_dir():
        return found
    for pat in PATTERNS:
        found.extend(p for p in LOG_DIR.glob(pat) if p.is_file())
    return sorted(set(found))


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge les transients de logs/ (dry-run par défaut).")
    ap.add_argument("--apply", action="store_true", help="supprime réellement")
    args = ap.parse_args()
    targets = collect()
    total = sum(p.stat().st_size for p in targets)
    print(f"{len(targets)} fichier(s), {_human(total)} — {'SUPPRESSION' if args.apply else 'dry-run'}")
    for p in targets[:20]:
        print(f"  {p.name} ({_human(p.stat().st_size)})")
    if len(targets) > 20:
        print(f"  … +{len(targets) - 20} autres")
    if not args.apply:
        print("Relancer avec --apply pour supprimer.")
        return 0
    freed = 0
    for p in targets:
        try:
            freed += p.stat().st_size
            p.unlink()
        except OSError as e:
            print(f"  ! {p.name}: {e}", file=sys.stderr)
    print(f"Libéré : {_human(freed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
