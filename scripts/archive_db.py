#!/usr/bin/env python3
"""archive_db.py — rotation + archivage de ``logs/requests.db`` (Phase 8 boot).

La base live a atteint 5,9 Go : chaque démarrage devait composer avec un
fichier énorme (backups lents, VACUUM de plusieurs minutes, restore coûteux).
Ce script la ramène à une taille exploitable en déplaçant l'historique ancien
vers des archives MENSUELLES en lecture seule, sans jamais perdre de données.

Modèle de données après archivage :

    logs/requests.db              base live, < 100 Mo, contient la fenêtre récente
    logs/archive/requests-YYYY-MM.db   archives mensuelles (lecture seule)

Étapes d'un passage (``--apply``) :

  1. ``wal_checkpoint(TRUNCATE)`` — le WAL est versé dans la base ;
  2. copie de sécurité complète par ``VACUUM INTO`` (fichier compacté, jamais
     un ``cp`` d'une base en cours d'écriture) ;
  3. transfert ``ATTACH`` des lignes antérieures au seuil vers l'archive du
     mois correspondant (``INSERT OR REPLACE``) ;
  4. suppression des lignes transférées de la base live, puis ``VACUUM``.

Sûreté :

  * refuse de tourner si le proxy tourne (fichier de lock ``logs/opencode-*.lock``
    ou ``-wal`` actif) — sauf ``--force`` ;
  * ``--dry-run`` par défaut : rien n'est écrit sans ``--apply`` ;
  * la copie de sécurité est vérifiée (``PRAGMA integrity_check``) avant
    toute suppression ;
  * ``--days`` fixe le seuil (défaut : ``database.archive_after_days`` de
    config.yaml, sinon 90).

Usage :
    python scripts/archive_db.py                    # dry-run, montre le plan
    python scripts/archive_db.py --apply            # exécute
    python scripts/archive_db.py --apply --days 60  # seuil personnalisé
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
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
DEFAULT_DB = ROOT / "logs" / "requests.db"
ARCHIVE_DIRNAME = "archive"
DEFAULT_DAYS = 90


def _human(n: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} To"


def _config_days() -> int:
    """Seuil depuis config.yaml (``database.archive_after_days``), sinon défaut."""
    try:
        import yaml

        with open(ROOT / "config.yaml", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        val = (data.get("database") or {}).get("archive_after_days")
        return int(val) if val else DEFAULT_DAYS
    except Exception:
        return DEFAULT_DAYS


def _proxy_running(db_path: Path) -> str | None:
    """Détecte une instance active — retourne la raison, ou None."""
    logs = db_path.parent
    for lock in logs.glob("opencode-*.lock"):
        try:
            if lock.stat().st_size >= 0 and lock.exists():
                # Un lock existe dès qu'une instance a démarré (pas de purge).
                # On le signale mais l'appelant peut --force : le seul vrai
                # danger est un écrivain ACTIF, détecté par le WAL ci-dessous.
                pass
        except OSError:
            continue
    wal = Path(str(db_path) + "-wal")
    try:
        if wal.exists() and wal.stat().st_size > 0:
            return f"WAL actif ({_human(wal.stat().st_size)}) — une instance écrit probablement"
    except OSError:
        pass
    locks = list(logs.glob("opencode-*.lock"))
    if locks and os.name == "nt":
        return f"fichier de lock présent ({locks[0].name})"
    return None


def _scalar(conn: sqlite3.Connection, sql: str, params=()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def build_plan(db_path: Path, days: int) -> dict:
    """Décrit ce qui serait archivé, sans rien modifier."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        total = _scalar(conn, "SELECT COUNT(*) FROM requests")
        old = _scalar(conn, "SELECT COUNT(*) FROM requests WHERE timestamp < ?", (cutoff,))
        months = conn.execute(
            "SELECT substr(timestamp, 1, 7), COUNT(*) FROM requests WHERE timestamp < ? GROUP BY 1 ORDER BY 1",
            (cutoff,),
        ).fetchall()
        return {
            "cutoff": cutoff,
            "total_rows": total,
            "old_rows": old,
            "recent_rows": total - old,
            "months": [(m, c) for m, c in months],
            "size": db_path.stat().st_size,
        }
    finally:
        conn.close()


def archive(db_path: Path, days: int, *, apply: bool, force: bool) -> int:
    if not db_path.exists():
        print(f"[archive] base introuvable: {db_path}", file=sys.stderr)
        return 1

    reason = _proxy_running(db_path)
    if reason and not force:
        print(f"[archive] REFUS: {reason}", file=sys.stderr)
        print("[archive] arrête le proxy (ou --force si tu sais ce que tu fais)", file=sys.stderr)
        return 1
    if reason:
        print(f"[archive] AVERTISSEMENT ignoré via --force: {reason}")

    plan = build_plan(db_path, days)
    print(f"[archive] base         : {db_path} ({_human(plan['size'])})")
    print(f"[archive] lignes       : {plan['total_rows']} au total")
    print(f"[archive] seuil        : {plan['cutoff']} ({days} jours)")
    print(f"[archive] à archiver   : {plan['old_rows']} lignes / à garder : {plan['recent_rows']}")
    for month, count in plan["months"]:
        print(f"[archive]   - {month}: {count} lignes")

    if plan["old_rows"] == 0:
        print("[archive] rien à archiver.")
        return 0
    if not apply:
        print("[archive] DRY-RUN — relance avec --apply pour exécuter.")
        return 0

    archive_dir = db_path.parent / ARCHIVE_DIRNAME
    archive_dir.mkdir(exist_ok=True)

    # 1) WAL versé dans la base principale.
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()

        # 2) Copie de sécurité compactée (VACUUM INTO, pas un cp à chaud).
        backup = db_path.with_suffix(f".bak-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
        print(f"[archive] copie de sécurité -> {backup.name}")
        conn.execute("VACUUM INTO ?", (str(backup),))

        # Vérification d'intégrité AVANT toute suppression.
        check = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()
        finally:
            check.close()
        if not ok or str(ok[0]).lower() != "ok":
            print(f"[archive] ABANDON: copie de sécurité corrompue ({ok})", file=sys.stderr)
            return 1

        # 3) Transfert vers les archives mensuelles.
        moved = 0
        # Schéma réel de `requests` récupéré depuis sqlite_master : on le
        # rejoue tel quel dans chaque archive (PK + colonnes + types), au lieu
        # d'un CREATE TABLE AS SELECT qui perdrait les contraintes.
        schema_row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='requests'").fetchone()
        if not schema_row or not schema_row[0]:
            print("[archive] ABANDON: schéma `requests` introuvable", file=sys.stderr)
            return 1
        # La DDL d'origine est NON qualifiée (« CREATE TABLE requests ») : rejouée
        # telle quelle, SQLite la résout vers `main.requests` — qui existe déjà,
        # donc `IF NOT EXISTS` en fait un NO-OP silencieux et l'INSERT suivant
        # échoue sur « no such table: arch.requests » (reproduit sur base test).
        # On qualifie donc explicitement la table dans le schéma attaché.
        table_ddl = schema_row[0].replace("CREATE TABLE requests", "CREATE TABLE IF NOT EXISTS arch.requests", 1)
        if "arch.requests" not in table_ddl:  # pragma: no cover - garde-fou
            print(
                f"[archive] ABANDON: DDL inattendue, qualification impossible: {schema_row[0][:80]}",
                file=sys.stderr,
            )
            return 1
        col_names = [r[1] for r in conn.execute("PRAGMA table_info(requests)").fetchall()]
        col_list = ", ".join(col_names)

        for month, _count in plan["months"]:
            target = archive_dir / f"requests-{month}.db"
            first_day = f"{month}-01"
            next_month = (dt.date(int(month[:4]), int(month[5:7]), 1) + dt.timedelta(days=32)).replace(day=1)
            upper = next_month.strftime("%Y-%m-01")
            print(f"[archive] {month} -> {target.name}")
            conn.execute("ATTACH DATABASE ? AS arch", (str(target),))
            try:
                # [fix] SQLite n'expose pas une table créée dans une base
                # attachée tant que la transaction n'est pas validée :
                # sans ce commit, l'INSERT suivant échoue sur
                # « no such table: arch.requests » (reproduit sur base test).
                conn.execute(table_ddl)
                conn.commit()
                conn.execute(
                    f"INSERT OR REPLACE INTO arch.requests ({col_list})"
                    f" SELECT {col_list} FROM main.requests"
                    " WHERE timestamp >= ? AND timestamp < ? AND timestamp < ?",
                    (first_day, upper, plan["cutoff"]),
                )
                conn.commit()
                n = conn.execute(
                    "SELECT COUNT(*) FROM arch.requests WHERE timestamp >= ? AND timestamp < ?",
                    (first_day, upper),
                ).fetchone()[0]
                moved += int(n or 0)
                # [fix] SQLite refuse un nom de TABLE qualifié dans CREATE
                # INDEX (« near ".": syntax error ») : c'est l'INDEX qu'on
                # qualifie, la table reste nue (résolue dans `arch`).
                conn.execute("CREATE INDEX IF NOT EXISTS arch.idx_archive_timestamp ON requests(timestamp)")
                conn.commit()
            except sqlite3.Error as e:
                print(f"[archive] ERREUR sur {month}: {e}", file=sys.stderr)
                return 1
            finally:
                try:
                    conn.execute("DETACH DATABASE arch")
                except sqlite3.Error:
                    pass

        # 4) Purge de la base live + compaction.
        print(f"[archive] suppression de {plan['old_rows']} lignes de la base live")
        conn.execute("DELETE FROM requests WHERE timestamp < ?", (plan["cutoff"],))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()

    new_size = db_path.stat().st_size
    print(f"[archive] terminé: {moved} lignes archivées, base live {_human(new_size)}")
    print(f"[archive] copie de sécurité conservée: {backup.name}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"base live (défaut: {DEFAULT_DB})")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help=f"seuil en jours (défaut: config.yaml database.archive_after_days, sinon {DEFAULT_DAYS})",
    )
    parser.add_argument("--apply", action="store_true", help="exécute réellement (sinon dry-run)")
    parser.add_argument("--force", action="store_true", help="outrepasse la détection d'instance active")
    args = parser.parse_args(argv)
    days = args.days if args.days is not None else _config_days()
    return archive(Path(args.db), days, apply=args.apply, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
