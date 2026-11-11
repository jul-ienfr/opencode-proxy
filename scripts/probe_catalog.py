#!/usr/bin/env python3
"""Campagne de découverte sur la 7e station (rôle probe), documentée en SQLite.

La station 7 n'est pas une station servante : elle rebinde des hostnames du
catalogue, teste la capture 2.2.5 avec un `ses_` neuf, et écrit chaque résultat
dans `logs/ip_registry.db` (`scan_runs`, `scan_attempts`, `observations`,
`ip_state`, `verified_spares`).

Le SOCKS5 de la 7e station est utilisé en DNS LOCAL (`socks5://`) : son
healthcheck DNS interne peut être cassé, mais le tunnel WireGuard passe les
IPs. On résout donc `opencode.ai` / `api.ipify.org` côté hôte.

Exemples :
    python scripts/probe_catalog.py --dry-run --max 12
    python scripts/probe_catalog.py --max 8
    python scripts/probe_catalog.py --hosts au1140.nordvpn.com,ca1704.nordvpn.com
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from test_free_egress import CAPTURE, load_capture, post_once  # noqa: E402
from test_selection_cycle import recreate  # noqa: E402

CATALOG = ROOT / "logs" / "nordvpn_classified.json"
DB = ROOT / "logs" / "ip_registry.db"
TRANSPORT = "wireguard"


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_catalog() -> dict[str, dict]:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for server in data.get("servers") or []:
        host = str(server.get("hostname") or "").lower()
        if host:
            out[host] = server
    return out


def load_tested(conn: sqlite3.Connection) -> set[str]:
    tested: set[str] = set()
    for table in ("observations", "scan_attempts", "verified_spares"):
        try:
            for (host,) in conn.execute(f"select distinct hostname from {table} where hostname is not null"):
                tested.add(str(host).lower())
        except sqlite3.Error:
            pass
    return tested


def select_hosts(catalog: dict[str, dict], tested: set[str], max_count: int, types: list[str], exclude: set[str]) -> list[dict]:
    by_type: dict[str, dict[str, list[dict]]] = {t: {} for t in types}
    for host, server in catalog.items():
        if server.get("status") != "online":
            continue
        if host in tested or host.startswith("socks-") or host.startswith("proxy-"):
            continue
        country = str(server.get("country") or "")
        if country.lower() in exclude:
            continue
        server_types = [t for t in (server.get("types") or []) if t in by_type]
        if not server_types:
            continue
        for typ in server_types:
            by_type[typ].setdefault(country, []).append(server)
    for typ, countries in by_type.items():
        for country, servers in countries.items():
            servers.sort(key=lambda s: float(s.get("load") or 100))

    picked: list[dict] = []
    used: set[str] = set()
    while len(picked) < max_count:
        progressed = False
        for typ in types:
            countries = by_type[typ]
            if not countries:
                continue
            for country in sorted(countries):
                servers = countries[country]
                while servers and str(servers[0].get("hostname") or "").lower() in used:
                    servers.pop(0)
                if not servers:
                    continue
                server = servers.pop(0)
                host = str(server.get("hostname") or "").lower()
                used.add(host)
                picked.append({"hostname": host, "country": country, "types": server.get("types") or [], "load": server.get("load")})
                progressed = True
                break
            if len(picked) >= max_count:
                break
        if not progressed:
            break
    return picked


def wait_egress_local(station: int, timeout_s: float) -> str:
    port = 1079 + station
    deadline = now() + timeout_s
    while now() < deadline:
        try:
            result = subprocess.run(
                ["curl.exe", "-s", "-m", "6", "--socks5", f"127.0.0.1:{port}", "https://api.ipify.org"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ip = (result.stdout or "").strip()
            if ip.count(".") == 3:
                return ip
        except Exception:
            pass
        time.sleep(3)
    return ""


def db_record(
    conn: sqlite3.Connection,
    run_id: str,
    station: int,
    candidate: dict,
    egress: str,
    result: dict | None,
    error: str,
    started: float,
    finished: float,
) -> None:
    status = int(result.get("status", 0)) if result else 0
    retry_after = float(result.get("retry_after") or 0.0) if result else 0.0
    latency = float(result.get("latency_ms") or 0.0) if result else 0.0
    session = str(result.get("session") or "") if result else ""
    attempt_id = uuid.uuid4().hex
    status_text = "200" if status == 200 else ("429" if status == 429 else ("no-egress" if not egress else "error"))
    reset_at = finished + retry_after if retry_after else None
    conn.execute(
        """
        insert into scan_attempts
        (attempt_id, run_id, station, hostname, country, transport, started_at, finished_at,
         status, egress_ip, valid, validation, fingerprint, error)
        values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            attempt_id, run_id, station, candidate["hostname"], candidate.get("country", ""), TRANSPORT,
            started, finished, status_text, egress or None, 1 if status == 200 else 0,
            session[:64] if session else None, None, error[:500],
        ),
    )
    conn.execute(
        """
        insert into observations
        (hostname, egress_ip, station, tested_at, status, retry_after_s, reset_at, latency_ms, error, attempt_id)
        values (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            candidate["hostname"], egress or None, station, finished,
            status if status in (200, 429) else 0, retry_after, reset_at, latency, error[:500], attempt_id,
        ),
    )
    if egress:
        state = "verified" if status == 200 else "candidate"
        score = 1.0 if status == 200 else 0.0
        cur = conn.execute(
            """
            update ip_state set hostname=?, country=?, state=?, score=?, last_status=?, last_tested_at=?,
                last_ok_at=coalesce(?, last_ok_at), reset_at=?, latency_ms=?,
                tests=tests+1, successes=successes+?, failures=failures+?, updated_at=?
            where egress_ip=?
            """,
            (
                candidate["hostname"], candidate.get("country", ""), state, score, status, finished,
                finished if status == 200 else None, reset_at, latency,
                1 if status == 200 else 0, 1 if status != 200 else 0, finished, egress,
            ),
        )
        if cur.rowcount == 0:
            conn.execute(
                """
                insert into ip_state
                (egress_ip, hostname, country, state, score, last_status, last_tested_at, last_ok_at,
                 reset_at, latency_ms, tests, successes, failures, updated_at)
                values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    egress, candidate["hostname"], candidate.get("country", ""), state, score, status,
                    finished, finished if status == 200 else None, reset_at, latency,
                    1, 1 if status == 200 else 0, 1 if status != 200 else 0, finished,
                ),
            )
        if status == 200:
            conn.execute(
                """
                insert into verified_spares
                (hostname, country, egress_ip, model, transport, station, last_200_at, expires_at)
                values (?,?,?,?,?,?,?,?)
                """,
                (
                    candidate["hostname"], candidate.get("country", ""), egress,
                    "deepseek-v4.1-flash-free", TRANSPORT, station, finished, finished + 6 * 3600,
                ),
            )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", type=int, default=7)
    parser.add_argument("--max", type=int, default=8)
    parser.add_argument("--hosts", help="liste explicite, séparée par des virgules")
    parser.add_argument("--types", default="standard,p2p,dedicated,double,obfuscated,onion")
    parser.add_argument("--exclude-countries", default="Albania")
    parser.add_argument("--egress-timeout", type=float, default=240.0)
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--model", default="deepseek-v4.1-flash-free")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    catalog = load_catalog()
    conn = sqlite3.connect(DB)
    tested = load_tested(conn)

    if args.hosts:
        hosts = []
        for raw in args.hosts.split(","):
            host = raw.strip().lower()
            if not host:
                continue
            server = catalog.get(host, {})
            hosts.append({"hostname": host, "country": server.get("country", ""), "types": server.get("types") or [], "load": server.get("load")})
    else:
        types = [t.strip() for t in args.types.split(",") if t.strip()]
        exclude = {c.strip().lower() for c in args.exclude_countries.split(",") if c.strip()}
        hosts = select_hosts(catalog, tested, args.max, types, exclude)
    hosts = hosts[: max(1, args.max)]

    if not hosts:
        print("aucun candidat", file=sys.stderr)
        return 2
    print(f"run candidates={len(hosts)} station={args.station} transport={TRANSPORT}", flush=True)
    for candidate in hosts:
        print(f"  - {candidate['hostname']} | {candidate.get('country')} | {candidate.get('types')} | load={candidate.get('load')}", flush=True)
    if args.dry_run:
        return 0

    capture = load_capture(CAPTURE)
    if capture is None:
        raise SystemExit(f"capture absente: {CAPTURE}")

    run_id = f"probe7-{int(now())}-{uuid.uuid4().hex[:6]}"
    started_run = now()
    conn.execute(
        "insert into scan_runs (run_id, station, transport, reason, candidates, started_at) values (?,?,?,?,?,?)",
        (run_id, args.station, TRANSPORT, "discovery-all-types-countries", len(hosts), started_run),
    )
    conn.commit()
    print(f"run_id={run_id}", flush=True)

    winners: list[dict] = []
    for candidate in hosts:
        host = candidate["hostname"]
        country = candidate.get("country") or ""
        started = now()
        print(f"bind station {args.station} -> {host} ({country})", flush=True)
        try:
            recreate(args.station, host, country)
            egress = wait_egress_local(args.station, args.egress_timeout)
            if not egress:
                db_record(conn, run_id, args.station, candidate, "", None, "no-egress", started, now())
                print("  no-egress", flush=True)
                continue
            result = post_once(
                f"socks5://127.0.0.1:{1079 + args.station}",
                args.model,
                args.timeout,
                capture=capture,
                reuse_session=False,
            )
            error = str(result.get("error") or "")
            db_record(conn, run_id, args.station, candidate, egress, result, error, started, now())
            print(
                f"  egress={egress} status={result.get('status')} ra={result.get('retry_after')} "
                f"session={result.get('session')}",
                flush=True,
            )
            if result.get("status") == 200:
                winners.append({"hostname": host, "country": country, "egress": egress, "session": result.get("session")})
                print(f"  WINNER {host} {egress}", flush=True)
        except Exception as exc:
            db_record(conn, run_id, args.station, candidate, "", None, f"{type(exc).__name__}: {exc}", started, now())
            print(f"  error {type(exc).__name__}: {exc}", flush=True)

    winner = winners[0] if winners else {}
    conn.execute(
        "update scan_runs set ended_at=?, outcome=?, winner_host=?, winner_ip=? where run_id=?",
        (now(), "200" if winners else "no-winner", winner.get("hostname"), winner.get("egress"), run_id),
    )
    conn.commit()
    conn.close()
    print(
        json.dumps(
            {
                "run_id": run_id,
                "tested": len(hosts),
                "winners": winners,
                "distinct_winner_ips": sorted({w["egress"] for w in winners}),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if winners else 2


if __name__ == "__main__":
    raise SystemExit(main())
