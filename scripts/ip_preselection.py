"""Rapport de présélection IP/serveur pour la rotation des stations.

Recherche uniquement : ne modifie ni la config ni le pool. Il joint
- les serveurs NordVPN recommandés (load, hostname, IP d'entrée),
- la qualité réellement mesurée dans logs/requests.db (tok/s, TTFB),
pour classer les candidats en tiers A (prouvés rapides), B (inconnus à
faible charge) et C (lents connus).

Usage :
    python scripts/ip_preselection.py
    python scripts/ip_preselection.py --hours 72 --top 25 --fast-tps 50
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
DB = ROOT / "logs" / "requests.db"
NORD_COUNTRIES = "https://api.nordvpn.com/v1/servers/countries"
NORD_RECO = (
    "https://api.nordvpn.com/v1/servers/recommendations"
    "?filters%5Bcountry_id%5D={cid}&limit=50"
    "&filters%5Bservers_technologies%5D%5Bidentifier%5D=openvpn_udp"
)


def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "opencode-proxy/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _countries() -> list[str]:
    try:
        cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
        raw = str((cfg.get("ip_rotation") or {}).get("server_countries") or "")
        out = [c.strip() for c in raw.split(",") if c.strip()]
        if out:
            return out
    except Exception:
        pass
    return ["Germany", "Netherlands", "France", "Sweden", "Switzerland"]


def _quality(hours: int) -> dict[str, dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not DB.exists():
        return {}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT free_model_ip AS ip,
               COUNT(*) AS n,
               AVG(tokens_output * 1000.0 / NULLIF(duration_ms, 0)) AS tps,
               AVG(ttfb_ms) AS ttfb,
               MAX(timestamp) AS last
        FROM requests
        WHERE timestamp >= ?
          AND free_model_ip IS NOT NULL
          AND free_model_ip != ''
          AND success = 1
          AND tokens_output > 0
          AND duration_ms > 0
        GROUP BY free_model_ip
        """,
        (since,),
    ).fetchall()
    con.close()
    return {
        str(r["ip"]): {
            "n": int(r["n"]),
            "tps": float(r["tps"] or 0.0),
            "ttfb": float(r["ttfb"]) if r["ttfb"] is not None else None,
            "last": str(r["last"] or ""),
        }
        for r in rows
    }


def _candidates(countries: list[str]) -> list[dict]:
    ids = {
        str(c.get("name")): int(c["id"])
        for c in _get_json(NORD_COUNTRIES)
        if c.get("name") and c.get("id")
    }
    out: list[dict] = []
    for country in countries:
        cid = ids.get(country)
        if not cid:
            print(f"  ! pays inconnu côté API : {country}")
            continue
        try:
            data = _get_json(NORD_RECO.format(cid=cid))
        except Exception as exc:
            print(f"  ! {country}: {type(exc).__name__}: {exc}")
            continue
        for s in data or []:
            if s.get("status") != "online" or not s.get("hostname"):
                continue
            ips = [
                str((entry.get("ip") or {}).get("ip") or "")
                for entry in (s.get("ips") or [])
                if isinstance(entry, dict)
            ]
            ips = [ip for ip in ips if ip]
            entry_ip = str(s.get("station") or (ips[0] if ips else ""))
            out.append(
                {
                    "country": country,
                    "host": str(s["hostname"]),
                    "load": int(s.get("load") or 0),
                    "ip": entry_ip,
                    "ips": ips,
                }
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--fast-tps", type=float, default=50.0)
    ap.add_argument("--slow-tps", type=float, default=20.0)
    ap.add_argument("--max-load", type=int, default=80)
    args = ap.parse_args()

    quality = _quality(args.hours)
    cands = _candidates(_countries())
    loads = [c["load"] for c in cands]
    if loads:
        print(
            f"API: {len(cands)} serveurs online, load min={min(loads)} "
            f"median={statistics.median(loads):.0f} max={max(loads)}"
        )
    print(f"DB {args.hours}h: {len(quality)} IP mesurées, {sum(q['n'] for q in quality.values())} requêtes")
    if not quality:
        print("  ! aucune qualité mesurée : les candidats resteront en tier B (load seule)")

    matched = 0
    ranked: list[dict] = []
    for c in cands:
        if c["load"] > args.max_load:
            continue
        q = None
        for ip in [c["ip"], *c["ips"]]:
            if ip in quality:
                q = quality[ip]
                matched += 1
                break
        if q and q["n"] >= 3 and q["tps"] >= args.fast_tps:
            tier, label = 0, "A"
        elif q and q["n"] >= 3 and q["tps"] < args.slow_tps:
            tier, label = 2, "C"
        else:
            tier, label = 1, "B"
        c.update({"tier": tier, "label": label, "q": q})
        ranked.append(c)

    ranked.sort(
        key=lambda c: (
            c["tier"],
            -(c["q"]["tps"] if c["q"] and c["q"]["n"] >= 3 else 0.0),
            c["load"],
            c["host"],
        )
    )
    print(f"Appariement IP API->DB: {matched}/{len(cands)} serveurs")
    if matched == 0:
        print(
            "  ! L'IP 'station' de l'API est l'IP d'ENTREE du serveur, pas l'IP "
            "publique vue par l'upstream (DB). Joindre les deux est faux : il faut "
            "attribuer la qualite au hostname (logs gluetun), pas a l'IP API."
        )
    top_measured = sorted(quality.items(), key=lambda kv: (-kv[1]["tps"], -kv[1]["n"]))[:10]
    if top_measured:
        print("\nTop IP publiques mesurees (DB, egress reel) :")
        for ip, q in top_measured:
            ttfb = f"{q['ttfb']:.0f}" if q.get("ttfb") is not None else "-"
            print(f"  {ip:16} n={q['n']:4} tok/s={q['tps']:6.1f} ttfb={ttfb:>7} last={q['last']}")
    print(f"\n{'tier':4} {'serveur':22} {'load':>4} {'ip':16} {'n':>4} {'tok/s':>7} {'ttfb':>7}  pays")
    for c in ranked[: args.top]:
        q = c["q"]
        tps = f"{q['tps']:.1f}" if q else "-"
        ttfb = f"{q['ttfb']:.0f}" if q and q.get("ttfb") is not None else "-"
        n = q["n"] if q else 0
        print(
            f"{c['label']:4} {c['host']:22} {c['load']:4} {c['ip']:16} "
            f"{n:4} {tps:>7} {ttfb:>7}  {c['country']}"
        )

    print("\nRépartition :", {k: sum(1 for c in ranked if c["label"] == k) for k in ("A", "B", "C")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
