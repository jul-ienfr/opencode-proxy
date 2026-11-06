import json
import re
import sqlite3
from pathlib import Path

root = Path(r"C:\Users\julie\Downloads\opencode-proxy-one")
logs = root / "logs"

ip_to_host: dict[str, str] = {}
host_to_country: dict[str, str] = {}

# 1. State JSON files: results[].host / results[].ip
for p in logs.glob("fresh_chain_state_*.json"):
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        continue
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        host = str(r.get("host") or "").strip()
        ip = str(r.get("ip") or "").strip()
        country = str(r.get("country") or "").strip()
        if host and ip:
            ip_to_host[ip] = host
        if host and country:
            host_to_country[host] = country

# 2. fresh_chain logs: "bind <host> (<country>)" then "egress <ip>"
bind_re = re.compile(r"bind\s+([a-z0-9][a-z0-9.-]*\.nordvpn\.com)\s+\(([^)]+)\)", re.I)
egress_re = re.compile(r"egress\s+(\d{1,3}(?:\.\d{1,3}){3})")
for p in logs.glob("fresh_chain_*.log"):
    last_host = None
    last_country = None
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = bind_re.search(line)
            if m:
                last_host = m.group(1).strip()
                last_country = m.group(2).strip()
                continue
            m = egress_re.search(line)
            if m and last_host:
                ip = m.group(1)
                ip_to_host[ip] = last_host
                if last_country:
                    host_to_country[last_host] = last_country
    except Exception:
        continue

print("ip->host mappings:", len(ip_to_host))
print("host->country mappings:", len(host_to_country))

# 3. Update ip_state rows with empty hostname.
con = sqlite3.connect(str(root / "logs" / "ip_registry.db"), timeout=30)
con.row_factory = sqlite3.Row
rows = con.execute("select egress_ip from ip_state where hostname = ''").fetchall()
missing = [str(r["egress_ip"]) for r in rows]
print("rows without hostname:", len(missing))
covered = [ip for ip in missing if ip in ip_to_host]
print("covered by mapping:", len(covered))
updated = 0
for ip in covered:
    host = ip_to_host[ip]
    country = host_to_country.get(host, "")
    con.execute(
        "update ip_state set hostname=?, country=coalesce(nullif(country,''), ?) where egress_ip=? and hostname=''",
        (host, country, ip),
    )
    updated += 1
con.commit()
print("updated:", updated)
print("with hostname now:", con.execute("select count(*) from ip_state where hostname != ''").fetchone()[0])
print("without hostname now:", con.execute("select count(*) from ip_state where hostname = ''").fetchone()[0])
con.close()
