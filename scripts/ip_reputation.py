#!/usr/bin/env python3
"""Réputation d'une IP — sans jamais contacter l'IP elle-même.

Croise les sources publiques (ipinfo, AlienVault OTX) et, si les clés d'API
sont présentes, VirusTotal et AbuseIPDB.  Ajoute les preuves locales : l'IP
est-elle dans les listes de proxys ou dans le catalogue d'egress ?

Une seule alerte antivirus n'est pas une preuve : le verdict exige plusieurs
sources indépendantes.  Le script ne se connecte jamais à la cible ; il ne
fera donc pas sonner à nouveau le firewall.

Exemples :
    python scripts/ip_reputation.py --ip 103.26.10.23
    python scripts/ip_reputation.py --ip 103.26.10.23 --json
    python scripts/ip_reputation.py --ip 103.26.10.23 --offline
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROXY_LISTS = ROOT / "logs" / "proxy_lists"
REGISTRY = ROOT / "logs" / "registry.db"


def _get_json(url: str, *, timeout: float = 10.0, headers: dict[str, str] | None = None):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "opencode-ip-reputation/1.0", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.HTTPError):
        return None


def local_evidence(ip: str, *, root: Path = ROOT) -> dict[str, Any]:
    lists: list[str] = []
    for name in ("pool.jsonl", "pool.txt", "working.jsonl"):
        path = root / "logs" / "proxy_lists" / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if ip in text:
            lists.append(name)
    catalog: list[dict[str, Any]] = []
    registry = root / "logs" / "registry.db"
    if registry.is_file():
        try:
            conn = sqlite3.connect(registry)
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT provider, host, port, egress_ip, status, last_status "
                "FROM egress_options WHERE host=? OR egress_ip=?",
                (ip, ip),
            ):
                catalog.append(dict(row))
            conn.close()
        except sqlite3.Error:
            pass
    return {"lists": lists, "catalog": catalog}


def _vt(ip: str, key: str, fetcher) -> dict[str, Any] | None:
    payload = fetcher(
        f"https://www.virustotal.com/api/v3/ip_addresses/{urllib.parse.quote(ip)}",
        headers={"x-apikey": key},
    )
    if not isinstance(payload, dict):
        return None
    attributes = (payload.get("data") or {}).get("attributes") or {}
    stats = attributes.get("last_analysis_stats") or {}
    return {
        "malicious": int(stats.get("malicious") or 0),
        "suspicious": int(stats.get("suspicious") or 0),
        "harmless": int(stats.get("harmless") or 0),
        "reputation": attributes.get("reputation"),
    }


def _abuse(ip: str, key: str, fetcher) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"ipAddress": ip, "maxAgeInDays": "90"})
    payload = fetcher(
        f"https://api.abuseipdb.com/api/v2/check?{query}",
        headers={"Key": key, "Accept": "application/json"},
    )
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") or {}
    return {
        "score": int(data.get("abuseConfidenceScore") or 0),
        "reports": int(data.get("totalReports") or 0),
        "country": data.get("countryCode") or "",
        "isp": data.get("isp") or "",
    }


def _otx(ip: str, fetcher) -> dict[str, Any] | None:
    payload = fetcher(
        f"https://otx.alienvault.com/api/v1/indicators/IPv4/{urllib.parse.quote(ip)}/general"
    )
    if not isinstance(payload, dict):
        return None
    pulses = payload.get("pulse_info") or {}
    return {
        "pulses": int(pulses.get("count") or 0),
        "validation": (payload.get("validation") or [{}])[0].get("name") if payload.get("validation") else "",
    }


def _ipinfo(ip: str, fetcher) -> dict[str, Any] | None:
    payload = fetcher(f"https://ipwho.is/{urllib.parse.quote(ip)}")
    if not isinstance(payload, dict) or payload.get("success") is False:
        return None
    return {
        "country": payload.get("country") or "",
        "asn": (payload.get("connection") or {}).get("asn"),
        "org": (payload.get("connection") or {}).get("org") or payload.get("org") or "",
        "proxy": bool(payload.get("security", {}).get("proxy")),
        "vpn": bool(payload.get("security", {}).get("vpn")),
    }


def verdict(report: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    malicious = 0
    suspicious = 0
    vt = report.get("virustotal") or {}
    if vt:
        if int(vt.get("malicious") or 0) >= 2:
            malicious += 1
            reasons.append(f"VirusTotal: {vt['malicious']} moteurs malveillants")
        elif int(vt.get("malicious") or 0) or int(vt.get("suspicious") or 0):
            suspicious += 1
            reasons.append("VirusTotal: signalement mineur")
    abuse = report.get("abuseipdb") or {}
    if abuse:
        if int(abuse.get("score") or 0) >= 75 and int(abuse.get("reports") or 0) >= 5:
            malicious += 1
            reasons.append(f"AbuseIPDB: score {abuse['score']} sur {abuse['reports']} rapports")
        elif int(abuse.get("score") or 0) > 0:
            suspicious += 1
            reasons.append(f"AbuseIPDB: score {abuse['score']}")
    otx = report.get("otx") or {}
    if otx and int(otx.get("pulses") or 0) >= 5:
        malicious += 1
        reasons.append(f"OTX: {otx['pulses']} pulses")
    elif otx and int(otx.get("pulses") or 0) > 0:
        suspicious += 1
        reasons.append(f"OTX: {otx['pulses']} pulse(s)")
    info = report.get("ipinfo") or {}
    if info.get("proxy") or info.get("vpn"):
        suspicious += 1
        reasons.append("ipwho.is: proxy/VPN déclaré")
    if report.get("local", {}).get("lists"):
        suspicious += 1
        reasons.append("présente dans une liste de proxys locale")
    if malicious >= 2:
        return "malicious", reasons
    if malicious == 1 or suspicious:
        return "suspicious", reasons
    return "unknown", reasons or ["aucune source ne signale cette IP"]


def check_ip(ip: str, *, offline: bool = False, fetcher=None, root: Path = ROOT) -> dict[str, Any]:
    fetcher = fetcher or _get_json
    report: dict[str, Any] = {"ip": ip, "offline": offline, "local": local_evidence(ip, root=root)}
    if not offline:
        report["ipinfo"] = _ipinfo(ip, fetcher)
        report["otx"] = _otx(ip, fetcher)
        vt_key = os.getenv("VIRUSTOTAL_API_KEY", "").strip()
        if vt_key:
            report["virustotal"] = _vt(ip, vt_key, fetcher)
        abuse_key = os.getenv("ABUSEIPDB_API_KEY", "").strip()
        if abuse_key:
            report["abuseipdb"] = _abuse(ip, abuse_key, fetcher)
    report["verdict"], report["reasons"] = verdict(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", action="append", required=True)
    parser.add_argument("--offline", action="store_true", help="preuves locales seulement")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    reports = [check_ip(ip, offline=args.offline) for ip in args.ip]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for report in reports:
            print(f"{report['ip']}: {report['verdict'].upper()}")
            for reason in report["reasons"]:
                print(f"  - {reason}")
            info = report.get("ipinfo") or {}
            if info:
                print(f"  ASN: {info.get('asn')} · {info.get('org')} · {info.get('country')}")
            if report["local"]["lists"]:
                print(f"  listes locales: {', '.join(report['local']['lists'])}")
    return 0 if all(report["verdict"] != "malicious" for report in reports) else 2


if __name__ == "__main__":
    sys.exit(main())
