#!/usr/bin/env python3
"""Classe les IP observées : datacenter / résidentiel / mobile, et par type
de serveur NordVPN (standard, p2p, dedicated, double, obfuscated, onion).

Entrées :
- `logs/ip_registry.db` (observations : egress_ip, hostname, status) ;
- `logs/nordvpn_classified.json` (type/catégorie par hostname) ;
- `ip-api.com/batch` pour l'ASN et la classe de l'egress réel.

Sortie : `logs/observed_classification.json` + résumé 200/403/429 par classe.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "logs" / "ip_registry.db"
CATALOG = ROOT / "logs" / "nordvpn_classified.json"
OUT = ROOT / "logs" / "observed_classification.json"
FIELDS = "status,country,city,isp,org,as,asname,mobile,proxy,hosting,query"


def catalog_index() -> dict[str, dict]:
    try:
        data = json.loads(CATALOG.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for server in data.get("servers") or []:
        host = str(server.get("hostname") or "").lower()
        if host:
            out[host] = server
    return out


def observations() -> list[dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT egress_ip, hostname, status, retry_after_s, tested_at
        FROM observations
        WHERE egress_ip IS NOT NULL AND egress_ip != ''
        ORDER BY id DESC
        """
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def classify_ips(ips: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for start in range(0, len(ips), 100):
        chunk = ips[start : start + 100]
        try:
            response = requests.post(
                "http://ip-api.com/batch",
                params={"fields": FIELDS},
                json=[{"query": ip} for ip in chunk],
                timeout=30,
            )
            data = response.json()
        except Exception as exc:
            print("batch error", start, type(exc).__name__, exc)
            data = []
        for item in data:
            if not isinstance(item, dict) or item.get("status") != "success":
                continue
            if item.get("hosting") or item.get("proxy"):
                klass = "datacenter"
            elif item.get("mobile"):
                klass = "mobile"
            else:
                klass = "residential"
            out[item.get("query")] = {
                "class": klass,
                "asn": item.get("as") or "",
                "org": item.get("org") or "",
                "isp": item.get("isp") or "",
                "country": item.get("country") or "",
                "hosting": bool(item.get("hosting")),
                "proxy": bool(item.get("proxy")),
                "mobile": bool(item.get("mobile")),
            }
        time.sleep(2.0)
    return out


def main() -> int:
    cat = catalog_index()
    rows = observations()
    unique = sorted({row["egress_ip"] for row in rows})
    print("observations", len(rows), "unique ips", len(unique))
    ip_info = classify_ips(unique)
    enriched = []
    for row in rows:
        host = str(row.get("hostname") or "").lower()
        server = cat.get(host, {})
        info = ip_info.get(row["egress_ip"], {})
        enriched.append(
            {
                **row,
                "server_types": server.get("types") or ["unknown"],
                "server_country": server.get("country") or "",
                "catalog_class": server.get("class") or "unknown",
                "egress_class": info.get("class") or "unknown",
                "asn": info.get("asn") or "",
                "org": info.get("org") or "",
                "isp": info.get("isp") or "",
            }
        )
    buckets: dict[str, Counter] = defaultdict(Counter)
    for item in enriched:
        key_class = item["egress_class"]
        buckets[f"class/{key_class}"][item["status"]] += 1
        for typ in item["server_types"]:
            buckets[f"type/{typ}"][item["status"]] += 1
        buckets[f"country/{item['server_country'] or '?'}"][item["status"]] += 1
    summary = {
        f"{name}": {
            "200": counter.get(200, 0),
            "403": counter.get(403, 0),
            "429": counter.get(429, 0),
            "other": sum(
                value
                for status, value in counter.items()
                if status not in (200, 403, 429)
            ),
        }
        for name, counter in sorted(buckets.items())
    }
    OUT.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "summary": summary,
                "rows": enriched,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
