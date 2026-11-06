"""Backfill hostname mappings into the ip_state quota inventory.

The historical inventory has thousands of egress IPs with no hostname: the
old request rows did not record ``server_host``. The proxy now records it,
and the allocator records host/egress pairs in ``observations``,
``assignments`` and ``verified_spares``. This script unions every local
evidence source, fills the empty hostnames, and inserts previously unknown
egress IPs as candidates.

Idempotent. Usage::

    python scripts/backfill_ip_hostnames.py --dry-run
    python scripts/backfill_ip_hostnames.py
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "logs" / "ip_registry.db"
REQUESTS = ROOT / "logs" / "requests.db"

HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.nordvpn\.com$", re.I)
IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
BIND_RE = re.compile(
    r"bind\s+([a-z0-9][a-z0-9.-]*\.nordvpn\.com)\s+\(([^)]+)\)", re.I
)
EGRESS_RE = re.compile(r"egress\s+(\d{1,3}(?:\.\d{1,3}){3})")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")


def norm_host(value: object) -> str:
    host = str(value or "").strip().lower()
    return host if HOST_RE.match(host) else ""


def norm_ip(value: object) -> str:
    ip = str(value or "").strip()
    if not IP_RE.match(ip):
        return ""
    try:
        if any(int(part) < 0 or int(part) > 255 for part in ip.split(".")):
            return ""
    except ValueError:
        return ""
    return ip


def iso_to_ts(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _merge(evidence: dict[str, dict], ip: str, host: str, **fields) -> None:
    """Keep the strongest/most recent evidence for one egress IP."""
    ip = norm_ip(ip)
    host = norm_host(host)
    if not ip or not host:
        return
    item = evidence.setdefault(
        ip,
        {
            "host": "",
            "host_at": 0.0,
            "country": "",
            "status": None,
            "tested_at": 0.0,
            "ok_at": 0.0,
            "reset_at": 0.0,
            "latency_ms": None,
            "tests": 0,
            "successes": 0,
            "failures": 0,
        },
    )
    tested_at = float(fields.get("tested_at") or 0.0)
    # The newest host observation wins; ties keep the first (registry order).
    if host and tested_at >= float(item["host_at"] or 0.0):
        item["host"] = host
        item["host_at"] = tested_at
    country = str(fields.get("country") or "").strip()
    if country and not item["country"]:
        item["country"] = country
    status = fields.get("status")
    if status is not None and tested_at >= float(item["tested_at"] or 0.0):
        item["status"] = int(status)
        item["tested_at"] = tested_at
        reset_at = float(fields.get("reset_at") or 0.0)
        if reset_at:
            item["reset_at"] = reset_at
        latency = fields.get("latency_ms")
        if latency is not None:
            item["latency_ms"] = float(latency)
    ok_at = float(fields.get("ok_at") or 0.0)
    if ok_at > float(item["ok_at"] or 0.0):
        item["ok_at"] = ok_at
    item["tests"] = max(int(item["tests"]), int(fields.get("tests") or 0))
    item["successes"] = max(int(item["successes"]), int(fields.get("successes") or 0))
    item["failures"] = max(int(item["failures"]), int(fields.get("failures") or 0))


def _server_countries(conn: sqlite3.Connection) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for host, country in conn.execute(
            "SELECT hostname, country FROM servers WHERE hostname != ''"
        ):
            host = norm_host(host)
            if host and country:
                out[host] = str(country).strip()
    except sqlite3.Error:
        pass
    return out


def _api_countries() -> dict[str, str]:
    """Authoritative hostname→country from the current NordVPN API snapshot.

    The ``servers`` table accumulates stale rows: the same hostname can appear
    with several countries, and the last row won (386 wrong countries). The
    API snapshot is the live catalog and wins.
    """
    path = ROOT / "logs" / "nordvpn_api.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("servers") if isinstance(data, dict) else data
    out: dict[str, str] = {}
    for item in servers or []:
        if not isinstance(item, dict):
            continue
        host = norm_host(item.get("hostname"))
        if not host:
            continue
        loc = (item.get("locations") or [{}])[0]
        country = ""
        if isinstance(loc, dict):
            raw = loc.get("country")
            if isinstance(raw, dict):
                country = str(raw.get("name") or "").strip()
        if country:
            out[host] = country
    return out


def _from_requests(evidence: dict[str, dict]) -> int:
    if not REQUESTS.exists():
        return 0
    count = 0
    try:
        conn = sqlite3.connect(f"file:{REQUESTS}?mode=ro", uri=True, timeout=15.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT server_host, free_model_ip,
                   COUNT(*) AS tests,
                   SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successes,
                   MAX(timestamp) AS last_ts,
                   MAX(CASE WHEN success = 1 THEN timestamp END) AS last_ok_ts,
                   MAX(CASE WHEN free_status LIKE '%429%' THEN 1 ELSE 0 END) AS saw_429
            FROM requests
            WHERE server_host != '' AND free_model_ip != ''
            GROUP BY server_host, free_model_ip
            """
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return 0
    for row in rows:
        host = norm_host(row["server_host"])
        ip = norm_ip(row["free_model_ip"])
        if not host or not ip:
            continue
        successes = int(row["successes"] or 0)
        tests = int(row["tests"] or 0)
        status = 200 if successes > 0 else (429 if int(row["saw_429"] or 0) else None)
        _merge(
            evidence,
            ip,
            host,
            status=status,
            tested_at=iso_to_ts(row["last_ts"]),
            ok_at=iso_to_ts(row["last_ok_ts"]),
            tests=tests,
            successes=successes,
            failures=max(0, tests - successes),
        )
        count += 1
    return count


def _from_registry(conn: sqlite3.Connection, evidence: dict[str, dict]) -> int:
    count = 0
    try:
        for row in conn.execute(
            "SELECT hostname, egress_ip, tested_at, status, reset_at, latency_ms "
            "FROM observations"
        ):
            host, ip = norm_host(row[0]), norm_ip(row[1])
            if not host or not ip:
                continue
            status = int(row[3]) if row[3] is not None else None
            _merge(
                evidence,
                ip,
                host,
                status=status,
                tested_at=float(row[2] or 0.0),
                ok_at=float(row[2] or 0.0) if status == 200 else 0.0,
                reset_at=float(row[4] or 0.0),
                latency_ms=row[5],
                tests=1,
                successes=1 if status == 200 else 0,
                failures=1 if status is not None and status != 200 else 0,
            )
            count += 1
    except sqlite3.Error:
        pass
    try:
        for row in conn.execute(
            "SELECT egress_ip, hostname, assigned_at, result FROM assignments"
        ):
            host, ip = norm_host(row[1]), norm_ip(row[0])
            if not host or not ip:
                continue
            result = str(row[3] or "")
            status = 200 if "200" in result else None
            _merge(
                evidence,
                ip,
                host,
                status=status,
                tested_at=float(row[2] or 0.0),
                ok_at=float(row[2] or 0.0) if status == 200 else 0.0,
            )
            count += 1
    except sqlite3.Error:
        pass
    try:
        for row in conn.execute(
            "SELECT hostname, country, egress_ip, last_200_at FROM verified_spares"
        ):
            host, ip = norm_host(row[0]), norm_ip(row[2])
            if not host or not ip:
                continue
            _merge(
                evidence,
                ip,
                host,
                country=row[1],
                status=200,
                tested_at=float(row[3] or 0.0),
                ok_at=float(row[3] or 0.0),
            )
            count += 1
    except sqlite3.Error:
        pass
    return count


def _from_inventory(evidence: dict[str, dict]) -> int:
    """Harvest the exhaustive inventory CSV (``hosts``/``countries`` columns)."""
    path = ROOT / "logs" / "usable_ips_inventory.csv"
    if not path.exists():
        return 0
    count = 0
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return 0
    for row in rows:
        ip = norm_ip(row.get("ip"))
        hosts = [part.strip() for part in str(row.get("hosts") or "").split(",")]
        countries = [
            part.strip() for part in str(row.get("countries") or "").split(",")
        ]
        if not ip:
            continue
        for index, raw_host in enumerate(hosts):
            host = norm_host(raw_host)
            if not host:
                continue
            country = countries[index] if index < len(countries) else ""
            _merge(evidence, ip, host, country=country)
            count += 1
    return count


def _from_backups(evidence: dict[str, dict]) -> int:
    """Harvest hostnames from the pre-migration registry backups."""
    count = 0
    for path in (ROOT / "logs").glob("ip_registry.db.bak-*"):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
            rows = conn.execute(
                "SELECT egress_ip, hostname, country, last_status, last_tested_at, "
                "last_ok_at, latency_ms, tests, successes, failures "
                "FROM ip_state WHERE hostname != ''"
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            continue
        for row in rows:
            host, ip = norm_host(row[1]), norm_ip(row[0])
            if not host or not ip:
                continue
            status = int(row[3]) if row[3] is not None else None
            _merge(
                evidence,
                ip,
                host,
                country=row[2],
                status=status,
                tested_at=float(row[4] or 0.0),
                ok_at=float(row[5] or 0.0),
                latency_ms=row[6],
                tests=int(row[7] or 0),
                successes=int(row[8] or 0),
                failures=int(row[9] or 0),
            )
            count += 1
    return count


def _from_chain_artifacts(evidence: dict[str, dict]) -> int:
    count = 0
    for path in (ROOT / "logs").glob("fresh_chain_state_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in data.get("results") or []:
            if not isinstance(row, dict):
                continue
            host, ip = norm_host(row.get("host")), norm_ip(row.get("ip"))
            if not host or not ip:
                continue
            raw_status = row.get("status")
            status = raw_status if isinstance(raw_status, int) else None
            _merge(
                evidence,
                ip,
                host,
                country=row.get("country"),
                status=status,
                tested_at=0.0,
                ok_at=0.0,
            )
            count += 1
    for path in (ROOT / "logs").glob("fresh_chain_*.log"):
        last_host = ""
        last_country = ""
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = BIND_RE.search(line)
            if match:
                last_host, last_country = match.group(1), match.group(2)
                continue
            match = EGRESS_RE.search(line)
            if not match or not last_host:
                continue
            stamp = TS_RE.match(line.strip())
            tested_at = iso_to_ts(stamp.group(1)) if stamp else 0.0
            _merge(
                evidence,
                match.group(1),
                last_host,
                country=last_country,
                tested_at=tested_at,
            )
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(str(REGISTRY), timeout=30.0)
    conn.row_factory = sqlite3.Row
    countries = _server_countries(conn)
    countries.update(_api_countries())
    existing = {
        str(row["egress_ip"]): dict(row)
        for row in conn.execute("SELECT * FROM ip_state")
    }
    evidence: dict[str, dict] = {}
    sources = {
        "requests": _from_requests(evidence),
        "registry": _from_registry(conn, evidence),
        "inventory": _from_inventory(evidence),
        "backups": _from_backups(evidence),
        "chain": _from_chain_artifacts(evidence),
    }
    for ip, item in evidence.items():
        if not item.get("country"):
            item["country"] = countries.get(item["host"], "")

    updated = inserted = skipped = 0
    now = time.time()
    for ip, item in evidence.items():
        host = item["host"]
        if not host:
            continue
        country = str(item.get("country") or "")
        if ip in existing:
            row = existing[ip]
            new_host = str(row.get("hostname") or "") or host
            new_country = str(row.get("country") or "") or country
            if new_host == str(row.get("hostname") or "") and new_country == str(
                row.get("country") or ""
            ):
                skipped += 1
                continue
            updated += 1
            if not args.dry_run:
                conn.execute(
                    "UPDATE ip_state SET hostname=?, country=?, updated_at=? "
                    "WHERE egress_ip=? AND (hostname='' OR country='')",
                    (new_host, new_country, now, ip),
                )
            continue
        inserted += 1
        if not args.dry_run:
            conn.execute(
                """
                INSERT OR IGNORE INTO ip_state(
                    egress_ip, hostname, country, state, last_status,
                    last_tested_at, last_ok_at, reset_at, latency_ms,
                    tests, successes, failures, updated_at
                ) VALUES (?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ip,
                    host,
                    country,
                    item.get("status"),
                    item.get("tested_at") or None,
                    item.get("ok_at") or None,
                    item.get("reset_at") or None,
                    item.get("latency_ms"),
                    int(item.get("tests") or 0),
                    int(item.get("successes") or 0),
                    int(item.get("failures") or 0),
                    now,
                ),
            )
    # [2026-11-03] Correct wrong countries: the stale `servers` table had 386
    # hostname rows in the wrong country. The live API snapshot wins.
    corrected = 0
    for row in conn.execute(
        "SELECT egress_ip, hostname, country FROM ip_state WHERE hostname != ''"
    ).fetchall():
        expected = countries.get(str(row[1] or "").lower())
        if not expected:
            continue
        if str(row[2] or "").strip().lower() == expected.lower():
            continue
        corrected += 1
        if not args.dry_run:
            conn.execute(
                "UPDATE ip_state SET country=?, updated_at=? WHERE egress_ip=?",
                (expected, now, row[0]),
            )
    # [2026-11-03] A 429 row without a reset is dead until the next UTC
    # midnight. Leaving reset_at NULL made `_registry_hosts` retry all 441
    # burned IPs on every chain (~80 s + AUTH each). The midnight estimate is
    # the provider's own daily reset; the row becomes eligible again after it.
    reset_filled = 0
    for row in conn.execute(
        "SELECT egress_ip, last_tested_at FROM ip_state "
        "WHERE last_status = 429 AND (reset_at IS NULL OR reset_at = 0)"
    ).fetchall():
        ts = float(row[1] or now)
        reset_at = ts - (ts % 86400.0) + 86400.0
        if reset_at <= now:
            reset_at = now + 86400.0
        reset_filled += 1
        if not args.dry_run:
            conn.execute(
                "UPDATE ip_state SET reset_at=?, updated_at=? WHERE egress_ip=?",
                (reset_at, now, row[0]),
            )
    if not args.dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM ip_state").fetchone()[0]
    with_host = conn.execute(
        "SELECT COUNT(*) FROM ip_state WHERE hostname != ''"
    ).fetchone()[0]
    without_host = total - with_host
    conn.close()

    print("sources:", sources)
    print(f"evidence IPs: {len(evidence)}")
    print(
        f"updated: {updated} | inserted: {inserted} | already mapped: {skipped} "
        f"| countries corrected: {corrected} | resets filled: {reset_filled}"
    )
    print(f"ip_state: {total} total | {with_host} with hostname | {without_host} without")
    if args.dry_run:
        print("dry-run: no changes written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
