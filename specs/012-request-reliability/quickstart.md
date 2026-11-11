# Quickstart — Fiabilité des requêtes free

## 1. Vérifier les correctifs P0

```powershell
# Config audit
python scripts/config_audit.py --strict

# Unités/contrats ciblés
python -m pytest tests/test_systemone.py tests/test_plan_011_baseline.py `
  tests/test_warp_rotation.py tests/test_warp_provisioner.py `
  tests/test_retry_until_success.py tests/test_empty_stream_asgi.py -q
```

## 2. Smoke SystemOne

```powershell
$body = @{
  model = "jev-1.13-free"
  state = "smoke test pool health"
  questions = @{ go = @{ type = "noul"; instructions = "go?" } }
} | ConvertTo-Json -Depth 5

curl.exe -s -o NUL -w "%{http_code}`n" -X POST http://127.0.0.1:4000/v1/systemone `
  -H "Content-Type: application/json" -d $body
```

Attendu : `200` ou `429` amont, **jamais** `503` « pool indisponible ».

## 3. Vérifier le canari IPv4

```powershell
curl.exe -s http://127.0.0.1:4000/api/vpn/socks5 | python -c "import sys,json
rows=[p for p in json.load(sys.stdin)['proxies'] if p.get('provider')=='warp']
print('enabled', sum(1 for p in rows if p['enabled']), 'total', len(rows))
print([(p['label'], p['enabled']) for p in rows])"
```

Attendu : 12 lignes déclarées, **7 actives** (6 IPv6 + `WARP 1` IPv4).

## 4. Vérifier NordVPN

```powershell
python scripts/refresh_nord_credentials.py --check
curl.exe -s http://127.0.0.1:4000/api/vpn-status | python -c "import sys,json
d=json.load(sys.stdin)
print(d.get('status'), d.get('usable_stations'))
print([(s['station'], s['vpn_status'], s.get('error')) for s in d.get('stations',[])])"
```

Attendu après réparation : une station `connected`, egress public, puis 6/6
progressivement. Pendant le parkage : aucune rafale `AUTH_FAILED`.

## 5. Surveiller 24 h

```powershell
curl.exe -s "http://127.0.0.1:4000/api/stats?from=2026-11-08&to=2026-11-08"
python - <<'PY'
import sqlite3
con=sqlite3.connect('file:logs/requests.db?mode=ro', uri=True)
for row in con.execute("select free_status,count(*) from requests where timestamp >= date('now') group by free_status"):
    print(row)
PY
```

Seuils : `503` = 0 ; `429` en baisse ; rotations < 150/jour ; p95 < 35 s.
