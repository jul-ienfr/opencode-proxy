"""scripts/check_models_catalog.py — job de diff catalogue (PLAN H1, lecture seule).

Compare la table statique ``models:`` de ``config.yaml`` au catalogue **live**
de l'éditeur et classe chaque entrée (`mort`, `hors-jambe`, `deprecated`,
`non-catalogue-interne`, `ok`). Écrit un rapport **horodaté** dans ``logs/`` et
affiche le compte rendu texte.

**Ce script n'écrit JAMAIS dans ``config.yaml``.** Il ne modifie rien au proxy :
c'est un diagnostic. La purge éventuelle est une décision manuelle, prise en
connaissance du rapport.

**Désactivé par défaut.** Le job ne part sur le réseau que si
``models_catalog.enabled: true`` est écrit dans ``config.yaml``. C'est le levier
de rollback : remettre ``false`` (ou retirer la section) suffit à le réduire au
silence, sans rien changer au code.

Usage :
    python scripts/check_models_catalog.py                 # refusé tant que non activé
    python scripts/check_models_catalog.py --force         # inspection ponctuelle, hors config
    python scripts/check_models_catalog.py --out rapport.json
    python scripts/check_models_catalog.py --json          # le rapport sur la sortie standard
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # exécutablevia `python -I` (sys.path sans cwd)
    sys.path.insert(0, str(ROOT))

from config.models_catalog import (  # noqa: E402  (import après le path)
    CATALOG_URL,
    audit,
    fetch_catalog,
    format_report,
)

EXIT_OK = 0
EXIT_ERREUR = 1
EXIT_DESACTIVE = 2


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _cfg_flag_enabled(default: bool = False) -> bool:
    """Lit ``models_catalog.enabled`` — sans jamais écrire quoi que ce soit."""
    from config import settings

    return bool(settings.yaml_get("models_catalog", "enabled", default))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="outrepasser models_catalog.enabled (manuel)")
    ap.add_argument(
        "--out", default=None, help="fichier de sortie (défaut : logs/models_catalog.diff.<horodatage>.json)"
    )
    ap.add_argument("--json", action="store_true", help="imprimer le rapport JSON au lieu du texte")
    args = ap.parse_args()

    if not args.force and not _cfg_flag_enabled():
        print(
            "job désactivé : écrire `models_catalog:\n  enabled: true` dans config.yaml "
            "(ou utiliser --force pour une inspection ponctuelle).",
            file=sys.stderr,
        )
        return EXIT_DESACTIVE

    try:
        catalog = fetch_catalog()
    except Exception as exc:  # réseau coupé, DNS, 403 : échec bruyant
        print(f"catalogue indisponible ({CATALOG_URL}) : {exc}", file=sys.stderr)
        return EXIT_ERREUR

    from config import settings  # noqa: PLC0415  (après le réseau, inutile sinon)

    report = audit(catalog, settings.MODELS)
    report["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    report["source"] = CATALOG_URL

    if args.out:
        out = Path(args.out)
    else:
        from config import settings  # noqa: PLC0415

        rel = settings.yaml_get("models_catalog", "output_dir", "logs") or "logs"
        out = ROOT / str(rel) / f"models_catalog.diff.{_stamp()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_report(report))
    print(f"\nrapport écrit : {out}  (config.yaml non modifié)", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
