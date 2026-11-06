#!/usr/bin/env python3
"""Classifie le catalogue NordVPN : datacenter / résidentiel / mobile.

Sources :
- `logs/nordvpn_api.json` (catalogue officiel, 7 000+ serveurs) ;
- `ip-api.com/batch` pour l'ASN et les drapeaux `hosting/proxy/mobile` de
  l'IP de station.

Sortie : `logs/nordvpn_classified.json` + résumé par classe, type et pays.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "logs" / "nordvpn_api.json"
OUT = ROOT / "logs" / "nordvpn_classified.json"
IPAPI = "http://ip-api.com/batch"
FIELDS = "status,country,city,isp,org,as,asname,mobile,proxy,hosting,query"
TYPE_MAP = {
    "legacy_standard": "standard",
    "legacy_p2p": "p2p",
    "legacy_dedicated_ip": "dedicated",
    "legacy_double_vpn": "double",
    "legacy_obfuscated_servers": "obfuscated",
    "legacy_onion_over_vpn": "onion",
}


def server_types(server: dict) -> list[str]:
    out = []
    for group in server.get("groups") or []:
        identifier = str(group.get("identifier") or "")
        if identifier in TYPE_MAP:
            out.append(TYPE_MAP[identifier])
    return sorted(set(out)) or ["unknown"]


def load_catalog() -> list[dict]:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    servers = []
    for item in data:
        locations = item.get("locations") or []
        location = locations[0] if locations else {}
        country = (location.get("country") or {}).get("name") or ""
        city = (location.get("country") or {}).get("city", {}).get("name") if isinstance(location.get("country"), dict) else ""
        servers.append(
            {
                "hostname": item.get("hostname"),
                "name": item.get("name"),
                "station": item.get("station"),
                "load": item.get("load"),
                "status": item.get("status"),
                "country": country,
                "city": city or "",
                "types": server_types(item),
            }
        )
    return servers


def classify(stations: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for start in range(0, len(stations), 100):
        chunk = stations[start : start + 100]
        payload = [{"query": ip} for ip in chunk]
        try:
            response = requests.post(
                IPAPI, params={"fields": FIELDS}, json=payload, timeout=30
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
            result[item.get("query")] = {
                "class": klass,
                "asn": item.get("as") or "",
                "asname": item.get("asname") or "",
                "org": item.get("org") or "",
                "isp": item.get("isp") or "",
                "country": item.get("country") or "",
                "city": item.get("city") or "",
                "hosting": bool(item.get("hosting")),
                "proxy": bool(item.get("proxy")),
                "mobile": bool(item.get("mobile")),
            }
        print("batch", start, "ok", len(data), flush=True)
        time.sleep(2.0)
    return result


def main() -> int:
    servers = load_catalog()
    unique = sorted({s["station"] for s in servers if s.get("station")})
    print("servers", len(servers), "unique stations", len(unique))
    info = classify(unique)
    for server in servers:
        details = info.get(server.get("station") or "", {})
        server.update(details)
        server.setdefault("class", "unknown")
    summary = {
        "servers": len(servers),
        "stations": len(unique),
        "by_class": dict(Counter(s.get("class") for s in servers)),
        "by_type": dict(Counter(t for s in servers for t in s.get("types") or [])),
        "by_class_type": {
            f"{klass}/{typ}": count
            for (klass, typ), count in Counter(
                (s.get("class"), t)
                for s in servers
                for t in (s.get("types") or ["unknown"])
            ).most_common(20)
        },
        "by_country_class": {
            f"{country}/{klass}": count
            for (country, klass), count in Counter(
                (s.get("country"), s.get("class")) for s in servers
            ).most_common(30)
        },
    }
    OUT.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "summary": summary,
                "servers": servers,
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
