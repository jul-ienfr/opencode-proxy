#!/usr/bin/env python3
"""Statistiques d'echecs depuis logs/requests.db (lecture seule)."""
from __future__ import annotations

import os
import sqlite3
import sys

DB = os.path.join("logs", "requests.db")


def q(cur, sql, params=()):
    try:
        return cur.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        return [("ERR", str(exc))]


def main() -> int:
    if not os.path.exists(DB):
        print("db introuvable")
        return 1
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()

    print("=" * 78)
    print("REQUESTS : totaux")
    print("=" * 78)
    for row in q(cur, """
        SELECT COUNT(*) total,
               SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) ok,
               SUM(CASE WHEN success=0 OR success IS NULL THEN 1 ELSE 0 END) ko,
               MIN(timestamp), MAX(timestamp)
        FROM requests"""):
        print(row)

    print("\n--- echecs par modele (top 20) ---")
    for row in q(cur, """
        SELECT COALESCE(original_model, model) m, COUNT(*) n,
               ROUND(AVG(duration_ms)) avg_ms
        FROM requests WHERE success=0 OR success IS NULL
        GROUP BY m ORDER BY n DESC LIMIT 20"""):
        print(f"  {row[1]:>7}  {row[0]}   (avg {row[2]} ms)")

    print("\n--- echecs : messages d'erreur normalises (top 25) ---")
    for row in q(cur, """
        SELECT error, COUNT(*) n FROM requests
        WHERE (success=0 OR success IS NULL) AND error IS NOT NULL AND error <> ''
        GROUP BY error ORDER BY n DESC LIMIT 25"""):
        print(f"  {row[1]:>7}  {str(row[0])[:150]}")

    print("\n--- echecs par jour (14 derniers) ---")
    for row in q(cur, """
        SELECT substr(timestamp,1,10) d, COUNT(*) n,
               SUM(CASE WHEN success=0 OR success IS NULL THEN 1 ELSE 0 END) ko
        FROM requests GROUP BY d ORDER BY d DESC LIMIT 14"""):
        print(f"  {row[0]}  total={row[1]:>6}  echecs={row[2]:>6}")

    print("\n--- free_model_usage : statuts (top 20) ---")
    for row in q(cur, """
        SELECT status, COUNT(*) n FROM free_model_usage
        GROUP BY status ORDER BY n DESC LIMIT 20"""):
        print(f"  {row[1]:>7}  status={row[0]}")

    print("\n--- free_model_usage : statuts par jour (7 derniers) ---")
    for row in q(cur, """
        SELECT substr(timestamp,1,10) d, status, COUNT(*) n
        FROM free_model_usage GROUP BY d, status
        ORDER BY d DESC, n DESC LIMIT 25"""):
        print(f"  {row[0]}  status={row[1]:<10} n={row[2]}")

    print("\n--- geo / vpn (sur echantillon recent) ---")
    for row in q(cur, """
        SELECT geo_blocked, geo_via_vpn, COUNT(*) n FROM requests
        WHERE timestamp >= (SELECT MAX(timestamp) FROM requests)
        GROUP BY geo_blocked, geo_via_vpn"""):
        print(f"  geo_blocked={row[0]} via_vpn={row[1]} n={row[2]}")

    print("\n--- colonnes volumineuses (poids des bodies) ---")
    for row in q(cur, """
        SELECT COUNT(*) n,
               SUM(LENGTH(COALESCE(request_body,'')))/1048576.0 req_mb,
               SUM(LENGTH(COALESCE(response_body,'')))/1048576.0 resp_mb
        FROM requests"""):
        print(f"  n={row[0]} request_body={row[1]:.1f} Mo response_body={row[2]:.1f} Mo")

    print("\n--- thinking / effort (7 derniers jours) ---")
    for row in q(cur, """
        SELECT thinking, effort, COUNT(*) n,
               SUM(CASE WHEN tokens_output=0 OR tokens_output IS NULL THEN 1 ELSE 0 END) sortie_vide
        FROM requests WHERE timestamp >= date('now','-7 day')
        GROUP BY thinking, effort ORDER BY n DESC LIMIT 12"""):
        print(f"  thinking={row[0]} effort={row[1]} n={row[2]} sorties_vides={row[3]}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
