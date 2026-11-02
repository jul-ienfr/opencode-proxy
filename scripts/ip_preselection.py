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
import time
import urllib.request
from datetime import UTC, datetime, timedelta
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


def _get_json(url: str, timeout: int = 20, attempts: int = 3):
    """GET JSON avec backoff court : une API pays qui timeout ne doit pas
    produire un rapport « 0 candidat » indiscernable d'une absence réelle."""
    last: Exception | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "opencode-proxy/1.0"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(0.8 * (2**attempt))
    if last is not None:
        raise last
    raise RuntimeError(f"GET failed without exception: {url}")


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


def _quality(hours: int) -> dict[str, dict[str, dict]]:
    since = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not DB.exists():
        return {"hosts": {}, "ips": {}}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        has_host = "server_host" in {
            str(row["name"]) for row in con.execute("PRAGMA table_info(requests)")
        }
        base = """
            SELECT {key} AS key,
                   COUNT(*) AS n,
                   AVG(tokens_output * 1000.0 / NULLIF(duration_ms, 0)) AS tps,
                   AVG(ttfb_ms) AS ttfb,
                   MAX(timestamp) AS last
            FROM requests
            WHERE timestamp >= ?
              AND {key} IS NOT NULL
              AND {key} != ''
              AND success = 1
              AND tokens_output > 0
              AND duration_ms > 0
            GROUP BY {key}
        """

        def _rows(key: str) -> dict[str, dict]:
            sql = base.format(key=key)
            return {
                str(r["key"]): {
                    "n": int(r["n"]),
                    "tps": float(r["tps"] or 0.0),
                    "ttfb": float(r["ttfb"]) if r["ttfb"] is not None else None,
                    "last": str(r["last"] or ""),
                }
                for r in con.execute(sql, (since,))
            }

        hosts = _rows("server_host") if has_host else {}
        return {"hosts": hosts, "ips": _rows("free_model_ip")}
    finally:
        con.close()


def _candidates(countries: list[str]) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    try:
        raw_countries = _get_json(NORD_COUNTRIES)
    except Exception as exc:
        return [], [f"countries: {type(exc).__name__}: {exc}"]
    ids = {
        str(c.get("name")): int(c["id"])
        for c in raw_countries
        if c.get("name") and c.get("id")
    }
    out: list[dict] = []
    for country in countries:
        cid = ids.get(country)
        if not cid:
            errors.append(f"{country}: unknown country id")
            continue
        try:
            data = _get_json(NORD_RECO.format(cid=cid))
        except Exception as exc:
            errors.append(f"{country}: {type(exc).__name__}: {exc}")
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
    return out, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--fast-tps", type=float, default=50.0)
    ap.add_argument("--slow-tps", type=float, default=20.0)
    ap.add_argument("--max-load", type=int, default=80)
    ap.add_argument("--json", action="store_true", help="rapport machine (preuve reproductible)")
    ap.add_argument("--output", default="", help="écrit le rapport JSON dans ce fichier (UTF-8)")
    args = ap.parse_args()
    json_mode = bool(args.json or args.output)

    def out(*values, **kwargs) -> None:
        if not json_mode:
            print(*values, **kwargs)

    quality = _quality(args.hours)
    q_hosts = quality["hosts"]
    q_ips = quality["ips"]
    cands, api_errors = _candidates(_countries())
    for error in api_errors:
        out(f"  ! API source_unavailable: {error}")
    loads = [c["load"] for c in cands]
    load_stats = (
        {
            "min": min(loads),
            "median": statistics.median(loads),
            "max": max(loads),
        }
        if loads
        else None
    )
    if load_stats:
        out(
            f"API: {len(cands)} serveurs online, load min={load_stats['min']} "
            f"median={load_stats['median']:.0f} max={load_stats['max']}"
        )
    total_n = sum(q["n"] for q in q_ips.values())
    out(
        f"DB {args.hours}h: {len(q_ips)} IP mesurées, {len(q_hosts)} hostnames, "
        f"{total_n} requêtes"
    )
    if not q_hosts:
        out(
            "  ! aucune attribution hostname en DB : les lignes existantes n'ont pas "
            "server_host (colonne ajoutée par ce correctif). La qualité reste "
            "joignable par IP, mais l'IP d'entrée de l'API n'est pas l'egress."
        )
    if not q_ips:
        out("  ! aucune qualité mesurée : les candidats resteront en tier B (load seule)")

    matched = 0
    host_matched = 0
    ranked: list[dict] = []
    for c in cands:
        if c["load"] > args.max_load:
            continue
        q = q_hosts.get(c["host"])
        source = "host"
        if q is None:
            for ip in [c["ip"], *c["ips"]]:
                if ip in q_ips:
                    q = q_ips[ip]
                    source = "ip"
                    break
        if q is not None:
            matched += 1
            if source == "host":
                host_matched += 1
        if q and q["n"] >= 3 and q["tps"] >= args.fast_tps:
            tier, label = 0, "A"
        elif q and q["n"] >= 3 and q["tps"] < args.slow_tps:
            tier, label = 2, "C"
        else:
            tier, label = 1, "B"
        c.update({"tier": tier, "label": label, "q": q, "q_source": source})
        ranked.append(c)

    ranked.sort(
        key=lambda c: (
            c["tier"],
            -(c["q"]["tps"] if c["q"] and c["q"]["n"] >= 3 else 0.0),
            c["load"],
            c["host"],
        )
    )
    out(f"Appariement hostname->DB: {host_matched}/{len(cands)} serveurs")
    out(f"Appariement IP API->DB (legacy, non fiable): {matched - host_matched}/{len(cands)}")
    if matched == 0:
        out(
            "  ! L'IP 'station' de l'API est l'IP d'ENTREE du serveur, pas l'IP "
            "publique vue par l'upstream (DB). Joindre les deux est faux : il faut "
            "attribuer la qualite au hostname (server_host)."
        )
    top_measured = sorted(q_ips.items(), key=lambda kv: (-kv[1]["tps"], -kv[1]["n"]))[:10]
    if top_measured:
        out("\nTop IP publiques mesurees (DB, egress reel) :")
        for ip, q in top_measured:
            ttfb = f"{q['ttfb']:.0f}" if q.get("ttfb") is not None else "-"
            out(f"  {ip:16} n={q['n']:4} tok/s={q['tps']:6.1f} ttfb={ttfb:>7} last={q['last']}")
    out(f"\n{'tier':4} {'serveur':22} {'load':>4} {'ip':16} {'n':>4} {'tok/s':>7} {'ttfb':>7}  pays")
    for c in ranked[: args.top]:
        q = c["q"]
        tps = f"{q['tps']:.1f}" if q else "-"
        ttfb = f"{q['ttfb']:.0f}" if q and q.get("ttfb") is not None else "-"
        n = q["n"] if q else 0
        out(
            f"{c['label']:4} {c['host']:22} {c['load']:4} {c['ip']:16} "
            f"{n:4} {tps:>7} {ttfb:>7}  {c['country']}"
        )

    tiers = {k: sum(1 for c in ranked if c["label"] == k) for k in ("A", "B", "C")}
    out("\nRépartition :", tiers)

    if json_mode:
        notes: list[str] = []
        if api_errors:
            notes.append(
                "source_unavailable: "
                + "; ".join(api_errors)
                + " — les candidats absents ne signifient pas « aucun serveur »."
            )
        if not q_hosts:
            notes.append(
                "0 hostname dans l'historique : server_host n'existe que depuis ce "
                "correctif ; l'attribution démarrera au prochain redémarrage du proxy. "
                "Les anciennes lignes ne sont pas reconstructibles de façon fiable."
            )
        if matched == 0:
            notes.append(
                "L'IP 'station' de l'API est l'IP d'entrée, jamais l'egress : la "
                "jointure IP est un repli legacy explicitement non fiable."
            )
        notes.append("ip_registry.enabled=false : l'allocateur runtime n'est pas branché.")
        notes.append(
            "nordcache_packed est déclaré sans décodeur : la résolution rapporte "
            "no-adapter:nordcache_packed si ce fallback est nécessaire."
        )
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "hours": args.hours,
            "thresholds": {
                "fast_tps": args.fast_tps,
                "slow_tps": args.slow_tps,
                "max_load": args.max_load,
                "min_samples": 3,
            },
            "api": {"servers": len(cands), "load": load_stats},
            "api_errors": api_errors,
            "db": {
                "ips": len(q_ips),
                "hosts": len(q_hosts),
                "requests": total_n,
            },
            "matching": {
                "hostname": host_matched,
                "ip_legacy": matched - host_matched,
                "candidates": len(cands),
            },
            "tiers": tiers,
            "top_ips": [
                {
                    "ip": ip,
                    "n": item["n"],
                    "tps": round(item["tps"], 3),
                    "ttfb": item["ttfb"],
                    "last": item["last"],
                }
                for ip, item in top_measured
            ],
            "top_candidates": [
                {
                    "host": c["host"],
                    "country": c["country"],
                    "load": c["load"],
                    "entry_ip": c["ip"],
                    "tier": c["label"],
                    "quality_source": c["q_source"] if c["q"] else None,
                    "n": c["q"]["n"] if c["q"] else 0,
                    "tps": round(c["q"]["tps"], 3) if c["q"] else None,
                    "ttfb": c["q"]["ttfb"] if c["q"] else None,
                }
                for c in ranked[: args.top]
            ],
            "notes": notes,
        }
        payload = json.dumps(report, indent=2, ensure_ascii=False)
        if args.output:
            Path(args.output).write_text(payload + "\n", encoding="utf-8")
        else:
            print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
