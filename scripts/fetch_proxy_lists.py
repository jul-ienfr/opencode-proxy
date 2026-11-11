#!/usr/bin/env python3
"""Collecte, normalise et déduplique des listes publiques de proxys.

Écrit un snapshot reproductible dans ``logs/proxy_lists/`` :

- ``pool.jsonl``   : un endpoint canonique par ligne (JSON)
- ``pool.txt``     : ``scheme://host:port`` pour ``scripts/test_proxy_pool.py``
- ``summary.json`` : compteurs par source et par schéma
- ``raw/``         : réponse brute de chaque source

Ce script ne fait aucune validation réseau des proxys : il collecte seulement.
La validation (egress réel + free 200) reste le rôle de
``scripts/test_proxy_pool.py`` puis, à terme, du registre ``public_proxies``.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "logs" / "proxy_lists"
USER_AGENT = "opencode-proxy-list-fetcher/1.0 (+https://opencode.ai)"
SUPPORTED_SCHEMES = ("http", "https", "socks5")

SOURCES: list[dict[str, Any]] = [
    {
        "name": "thespeedx_socks5",
        "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "scheme": "socks5",
    },
    {
        "name": "thespeedx_http",
        "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "scheme": "http",
    },
    {
        "name": "monosans_socks5",
        "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
        "scheme": "socks5",
    },
    {
        "name": "monosans_http",
        "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "scheme": "http",
    },
    {
        "name": "proxifly_socks5",
        "url": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
        "scheme": "socks5",
    },
    {
        "name": "proxifly_http",
        "url": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
        "scheme": "http",
    },
    {
        "name": "proxifly_https",
        "url": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt",
        "scheme": "https",
    },
    {
        "name": "hookzof_socks5",
        "url": "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
        "scheme": "socks5",
    },
    {
        "name": "roosterkid_https",
        "url": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
        "scheme": "https",
    },
    {
        "name": "shiftytr_socks5",
        "url": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
        "scheme": "socks5",
    },
    {
        "name": "mmpx12_socks5",
        "url": "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt",
        "scheme": "socks5",
    },
    {
        "name": "mmpx12_http",
        "url": "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
        "scheme": "http",
    },
    {
        "name": "mmpx12_https",
        "url": "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt",
        "scheme": "https",
    },
    {
        "name": "zloi_socks5",
        "url": "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt",
        "scheme": "socks5",
    },
    {
        "name": "zloi_http",
        "url": "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
        "scheme": "http",
    },
    {
        "name": "zloi_https",
        "url": "https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt",
        "scheme": "https",
    },
    {
        "name": "aliilapro_socks5",
        "url": "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt",
        "scheme": "socks5",
    },
    {
        "name": "jetkai_socks5",
        "url": "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt",
        "scheme": "socks5",
    },
    {
        "name": "clarketm_http",
        "url": "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
        "scheme": "http",
    },
    {
        "name": "sunny_http",
        "url": "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt",
        "scheme": "http",
    },
    {
        "name": "proxyscrape_socks5",
        "url": "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=socks5&proxy_format=protocolipport&format=text",
        "scheme": "socks5",
    },
    {
        "name": "proxyscrape_http",
        "url": "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=protocolipport&format=text",
        "scheme": "http",
    },
    {
        "name": "geonode_socks5",
        "url": "https://proxylist.geonode.com/api/proxy-list?limit=500&page={page}&sort_by=lastChecked&sort_type=desc&protocols=socks5",
        "kind": "geonode",
        "scheme": "socks5",
        "max_pages": 4,
    },
]


@dataclass
class SourceStat:
    name: str
    url: str
    ok: bool = False
    lines: int = 0
    parsed: int = 0
    rejected: int = 0
    duplicates: int = 0
    added: int = 0
    error: str | None = None


@dataclass
class Endpoint:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    sources: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def host_fmt(self) -> str:
        return f"[{self.host}]" if ":" in self.host else self.host

    @property
    def auth(self) -> str:
        if not (self.username or self.password):
            return ""
        user = urllib.parse.quote(self.username or "", safe="")
        pwd = urllib.parse.quote(self.password or "", safe="")
        return f"{user}:{pwd}@"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.auth}{self.host_fmt}:{self.port}"

    @property
    def key(self) -> str:
        auth = ""
        if self.username or self.password:
            auth = f"{self.username or ''}:{self.password or ''}@"
        return f"{self.scheme}://{auth}{self.host.lower()}:{self.port}"

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "password": self.password,
            "url": self.url,
            "sources": self.sources,
        }
        if self.meta:
            row["meta"] = self.meta
        return row


def _usable_host(host: str) -> bool:
    value = host.strip().strip("[]")
    if not value:
        return False
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return "." in value and not value.startswith(".")
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def normalize_line(line: str, default_scheme: str) -> Endpoint | None:
    value = line.strip()
    if not value or value.startswith("#") or value.startswith("//"):
        return None
    value = value.split("#", 1)[0].strip()
    if not value:
        return None
    value = value.split()[0]
    if "://" not in value and value.count(":") > 1 and not value.startswith("["):
        value = ":".join(value.split(":")[:2])

    scheme = default_scheme
    rest = value
    if "://" in value:
        scheme, rest = value.split("://", 1)
        scheme = scheme.lower()
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme not in SUPPORTED_SCHEMES:
        return None

    try:
        parsed = urllib.parse.urlsplit(f"//{rest}")
        host = parsed.hostname or ""
        port = int(parsed.port or 0)
    except (ValueError, TypeError):
        # Certaines listes (zloi/hideip.me) ajoutent un suffixe pays :
        # ``ip:port:Country``. urlsplit voit alors un port invalide et
        # rejette la ligne. On récupère le dernier ``:port`` numérique et on
        # ignore le reste.
        match = re.match(r"^(?P<hostpart>.+):(?P<port>\d+)(?::.*)?$", rest)
        if match is None:
            return None
        try:
            parsed = urllib.parse.urlsplit(f"//{match.group('hostpart')}:{match.group('port')}")
            host = parsed.hostname or ""
            port = int(match.group("port"))
        except (ValueError, TypeError):
            return None
    if not host or not (1 <= port <= 65535) or not _usable_host(host):
        return None

    username = urllib.parse.unquote(parsed.username) if parsed.username else None
    password = urllib.parse.unquote(parsed.password) if parsed.password else None
    return Endpoint(
        scheme=scheme,
        host=host.strip("[]"),
        port=port,
        username=username or None,
        password=password or None,
    )


def fetch_text(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def fetch_source(source: dict[str, Any], timeout: float, max_pages: int) -> tuple[str, list[Endpoint], SourceStat]:
    stat = SourceStat(name=str(source["name"]), url=str(source["url"]))
    default_scheme = str(source.get("scheme") or "http")
    kind = str(source.get("kind") or "lines")
    chunks: list[str] = []
    endpoints: list[Endpoint] = []

    try:
        if kind == "geonode":
            limit = 500
            pages = min(int(source.get("max_pages") or max_pages), max_pages)
            for page in range(1, pages + 1):
                raw = fetch_text(str(source["url"]).format(page=page), timeout)
                chunks.append(raw)
                payload = json.loads(raw)
                rows = payload.get("data") or []
                stat.lines += len(rows)
                for row in rows:
                    host = str(row.get("ip") or "").strip()
                    try:
                        port = int(row.get("port") or 0)
                    except (TypeError, ValueError):
                        port = 0
                    if not host or not (1 <= port <= 65535) or not _usable_host(host):
                        stat.rejected += 1
                        continue
                    protocols = row.get("protocols") or [default_scheme]
                    for protocol in protocols:
                        scheme = str(protocol).lower()
                        if scheme == "socks5h":
                            scheme = "socks5"
                        if scheme not in SUPPORTED_SCHEMES:
                            continue
                        ep = Endpoint(scheme=scheme, host=host, port=port)
                        ep.meta = {
                            key: row[key]
                            for key in ("country", "city", "asn", "isp", "anonymityLevel", "latency", "upTime")
                            if row.get(key) is not None
                        }
                        endpoints.append(ep)
                        stat.parsed += 1
                if len(rows) < limit:
                    break
        else:
            raw = fetch_text(str(source["url"]), timeout)
            chunks.append(raw)
            lines = raw.splitlines()
            stat.lines = len(lines)
            for line in lines:
                ep = normalize_line(line, default_scheme)
                if ep is None:
                    if line.strip() and not line.strip().startswith("#"):
                        stat.rejected += 1
                    continue
                endpoints.append(ep)
                stat.parsed += 1
        stat.ok = True
    except Exception as exc:
        stat.error = f"{type(exc).__name__}: {exc}"[:300]

    return "\n".join(chunks), endpoints, stat


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "source"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--source", action="append", default=[], help="nom de source (répétable)")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    max_pages = max(1, int(args.max_pages))

    selected = [s for s in SOURCES if not args.source or s["name"] in set(args.source)]
    if not selected:
        print("aucune source sélectionnée", file=sys.stderr)
        return 2

    by_key: dict[str, Endpoint] = {}
    stats: list[SourceStat] = []
    started = time.time()

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(fetch_source, source, args.timeout, max_pages) for source in selected]
        for future in futures:
            raw, endpoints, stat = future.result()
            stats.append(stat)
            (raw_dir / f"{_safe_name(stat.name)}.txt").write_text(raw, encoding="utf-8")
            for ep in endpoints:
                current = by_key.get(ep.key)
                if current is None:
                    ep.sources = [stat.name]
                    by_key[ep.key] = ep
                    stat.added += 1
                else:
                    stat.duplicates += 1
                    if stat.name not in current.sources:
                        current.sources.append(stat.name)
            print(
                f"{stat.name:24} ok={stat.ok!s:5} lines={stat.lines:6} parsed={stat.parsed:6} "
                f"added={stat.added:5} dup={stat.duplicates:5} rejected={stat.rejected:5}"
                + (f" error={stat.error}" if stat.error else ""),
                file=sys.stderr,
                flush=True,
            )

    endpoints = sorted(by_key.values(), key=lambda ep: (ep.scheme, ep.host, ep.port))
    pool_jsonl = out_dir / "pool.jsonl"
    pool_jsonl.write_text(
        "".join(json.dumps(ep.as_dict(), ensure_ascii=False) + "\n" for ep in endpoints),
        encoding="utf-8",
    )
    pool_txt = out_dir / "pool.txt"
    pool_txt.write_text("".join(ep.url + "\n" for ep in endpoints), encoding="utf-8")

    per_scheme: dict[str, int] = {}
    for ep in endpoints:
        per_scheme[ep.scheme] = per_scheme.get(ep.scheme, 0) + 1
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_s": round(time.time() - started, 2),
        "sources_total": len(selected),
        "sources_ok": sum(1 for stat in stats if stat.ok),
        "total_unique": len(endpoints),
        "unique_hosts": len({ep.host for ep in endpoints}),
        "per_scheme": dict(sorted(per_scheme.items())),
        "outputs": {
            "pool_jsonl": str(pool_jsonl),
            "pool_txt": str(pool_txt),
            "raw_dir": str(raw_dir),
        },
        "sources": [
            {
                "name": stat.name,
                "url": stat.url,
                "ok": stat.ok,
                "lines": stat.lines,
                "parsed": stat.parsed,
                "added": stat.added,
                "duplicates": stat.duplicates,
                "rejected": stat.rejected,
                "error": stat.error,
            }
            for stat in sorted(stats, key=lambda stat: stat.name)
        ],
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({k: summary[k] for k in ("total_unique", "unique_hosts", "per_scheme")}, ensure_ascii=False))
    print(f"saved {pool_jsonl}")
    return 0 if endpoints else 1


if __name__ == "__main__":
    raise SystemExit(main())
