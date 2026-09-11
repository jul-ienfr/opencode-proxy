#!/usr/bin/env python3
"""Audit des logs du proxy : agrège warnings / erreurs / familles de pannes.

Usage:
    python scripts/log_audit.py [--file logs/debug.log] [--db logs/requests.db] [--top 25]

Le fichier debug.log peut etre verrouille par le process en cours : on l'ouvre
en lecture partagee. Les lignes de payload (SSE, bodies convertis, schemas) sont
exclues du regroupement car elles noient les vrais messages.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Any

# Lignes = dumps de payload : elles contiennent du texte utilisateur et ne
# doivent jamais etre comptees comme des erreurs.
PAYLOAD_MARKERS = (
    "[stream-oai] no choices, trying responses_sse convert",
    "[messages] converted to openai",
    "[responses-sse] event type=",
    "trying responses_sse convert",
    "[schema] strip pattern",
    "[stream-oai] summary:",
)

RE_LEVEL = re.compile(r"\[(WARNING|ERROR|CRITICAL|FATAL)\]|\b(WARNING|ERROR|CRITICAL|FATAL)\b")
RE_TS = re.compile(r"\d{4}-\d{2}-\d{2}[ T][\d:.]+Z?")
RE_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
RE_LONGSTR = re.compile(r"'[^']{25,}'|\"[^\"]{25,}\"")
RE_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")

FAMILIES = {
    "vpn_auth_failed": re.compile(r"AUTH_FAILED"),
    "vpn_degraded": re.compile(r"degraded pin-loop|degraded, recheck"),
    "vpn_reconnect": re.compile(r"(?i)\breconnect|resets voie secondaire|restart"),
    "thinking_missing": re.compile(r"upstream returned no reasoning_content"),
    "rate_limit_429": re.compile(r"\b429\b|rate.?limit", re.I),
    "quota": re.compile(r"(?i)\bquota\b"),
    "paused_key": re.compile(r"(?i)\bpaused?\b"),
    "circuit_breaker": re.compile(r"(?i)circuit"),
    "timeout": re.compile(r"(?i)timed? ?out|timeout"),
    "conn_error": re.compile(r"(?i)connection (?:reset|refused|aborted|error)"),
    "ssl_error": re.compile(r"(?i)\bssl\b|certificate"),
    "truncation": re.compile(r"(?i)truncat"),
    "auth_missing": re.compile(r"(?i)(missing|no|invalid|unauthoriz\w*)\s*(api[\s_-]?key|token|auth)"),
    "db_error": re.compile(r"(?i)database is locked|sqlite3\.Operational|disk i/o error"),
    "datapolicy": re.compile(r"DataPolicyError|datapolicy-guard"),
    "free_400": re.compile(r"free400"),
    "fallback": re.compile(r"(?i)fallback"),
    "exception": re.compile(r"(?i)traceback|exception|\berror\b"),
}


def normalize(line: str) -> str:
    s = RE_TS.sub("<TS>", line)
    s = re.sub(r"^\[[^\]]*\]\s*", "", s)
    s = RE_UUID.sub("<UUID>", s)
    s = RE_LONGSTR.sub("<STR>", s)
    s = RE_NUM.sub("<N>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200]


def open_shared(path: str):
    """Ouvre un fichier eventuellement verrouille par un autre process."""
    if os.name == "nt":
        import msvcrt  # noqa: F401

        handle = open(path, encoding="utf-8", errors="replace")
        return handle
    return open(path, encoding="utf-8", errors="replace")


def scan_log(path: str, top: int) -> dict:
    levels: Counter = Counter()
    families: Counter = Counter()
    warn_msgs: Counter = Counter()
    crit_msgs: Counter = Counter()
    err_msgs: Counter = Counter()
    family_samples: dict[str, Counter] = defaultdict(Counter)
    total = 0
    payload_lines = 0
    first_ts = last_ts = None

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            total += 1
            stripped = line.rstrip("\n")
            if first_ts is None:
                m = RE_TS.search(stripped)
                if m:
                    first_ts = m.group(0)
            m = RE_TS.search(stripped)
            if m:
                last_ts = m.group(0)

            is_payload = any(mk in stripped for mk in PAYLOAD_MARKERS)
            if is_payload:
                payload_lines += 1
                continue

            lvl = None
            m = RE_LEVEL.search(stripped)
            if m:
                lvl = m.group(1) or m.group(2)
            if lvl:
                levels[lvl] += 1
                norm = normalize(stripped)
                if lvl == "WARNING":
                    warn_msgs[norm] += 1
                elif lvl in ("CRITICAL", "FATAL"):
                    crit_msgs[norm] += 1
                else:
                    err_msgs[norm] += 1

            for name, rx in FAMILIES.items():
                if rx.search(stripped):
                    families[name] += 1
                    if len(family_samples[name]) < 400:
                        family_samples[name][normalize(stripped)] += 1

    return {
        "file": path,
        "lines": total,
        "payload_lines_excluded": payload_lines,
        "window": (first_ts, last_ts),
        "levels": levels,
        "families": families,
        "warn_msgs": warn_msgs,
        "crit_msgs": crit_msgs,
        "err_msgs": err_msgs,
        "family_samples": family_samples,
        "top": top,
    }


def scan_db(path: str) -> dict:
    if not os.path.exists(path):
        return {"error": f"{path} introuvable"}
    out: dict = {"path": path, "size_bytes": os.path.getsize(path)}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"error": f"ouverture impossible: {exc}"}
    cur = con.cursor()
    try:
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        out["tables"] = tables
        for t in tables:
            try:
                out.setdefault("rowcounts", {})[t] = cur.execute(
                    f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                pass
        cols = {}
        for t in tables:
            try:
                cols[t] = [r[1] for r in cur.execute(f'PRAGMA table_info("{t}")')]
            except sqlite3.Error:
                pass
        out["columns"] = cols
        out["page_count"] = cur.execute("PRAGMA page_count").fetchone()[0]
        out["page_size"] = cur.execute("PRAGMA page_size").fetchone()[0]
        out["freelist"] = cur.execute("PRAGMA freelist_count").fetchone()[0]
    except sqlite3.Error as exc:
        out["error"] = str(exc)
    finally:
        con.close()
    return out


def fmt(counter: Counter, n: int) -> list[str]:
    return [f"  {v:>7}  {k}" for k, v in counter.most_common(n)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=os.path.join("logs", "debug.log"))
    ap.add_argument("--db", default=os.path.join("logs", "requests.db"))
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # Annoté `dict[str, Any]` : sans cela, le ternaire ci-dessous fait inférer
    # `dict[str, str]`, et chaque accès (`families`, `levels`, `window`…) devient
    # une erreur de type — le contenu réel mêle entiers, listes et Counters.
    log: dict[str, Any] = (
        scan_log(args.file, args.top) if os.path.exists(args.file) else {"error": "log introuvable"}
    )
    db = scan_db(args.db)

    if args.json:
        print(json.dumps({
            "log": {k: (dict(v) if isinstance(v, Counter) else
                        {kk: dict(vv) for kk, vv in v.items()} if isinstance(v, dict) and v and isinstance(next(iter(v.values())), Counter) else v)
                    for k, v in log.items() if k != "family_samples"},
            "db": db,
        }, indent=2, default=str))
        return 0

    print("=" * 78)
    print(f"LOG AUDIT  {log.get('file')}")
    print("=" * 78)
    if "error" in log:
        print(log["error"])
    else:
        print(f"lignes totales     : {log['lines']}")
        print(f"payloads exclus    : {log['payload_lines_excluded']}")
        print(f"fenetre            : {log['window'][0]}  ->  {log['window'][1]}")
        print(f"niveaux            : {dict(log['levels']) or '{} (aucun tag de niveau)'}")

        print("\n--- FAMILLES DE PROBLEMES (lignes hors payload) ---")
        for k, v in log["families"].most_common():
            print(f"  {v:>7}  {k}")

        print("\n--- WARNINGS (normalises) ---")
        print("\n".join(fmt(log["warn_msgs"], args.top)) or "  (aucun)")

        print("\n--- ERREURS / CRITIQUES (normalises) ---")
        print("\n".join(fmt(log["err_msgs"] + log["crit_msgs"], args.top)) or "  (aucun)")

        print("\n--- ECHANTILLONS PAR FAMILLE ---")
        for name, _ in log["families"].most_common(8):
            print(f"\n  [{name}]")
            print("\n".join(fmt(log["family_samples"][name], 4)))

    print("\n" + "=" * 78)
    print(f"DB AUDIT  {db.get('path')}")
    print("=" * 78)
    if "error" in db:
        print(db["error"])
    else:
        size = db.get("size_bytes", 0)
        print(f"taille            : {size/1e9:.2f} Go ({size/1e6:.0f} Mo)")
        pages = db.get("page_count") or 0
        psize = db.get("page_size") or 0
        print(f"page_count*size   : {pages} x {psize} = {pages*psize/1e9:.2f} Go")
        print(f"freelist (vides)  : {db.get('freelist')} pages "
              f"({(db.get('freelist') or 0)*psize/1e6:.0f} Mo)")
        print(f"tables            : {db.get('tables')}")
        print(f"rowcounts         : {db.get('rowcounts')}")
        for t, c in (db.get("columns") or {}).items():
            print(f"  {t}: {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
