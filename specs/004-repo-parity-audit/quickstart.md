# Quickstart — Vérifier l'état de la jambe free

**Feature**: `004-repo-parity-audit`  
Toutes les commandes se lancent depuis la racine du dépôt. PowerShell,
`pythonw` en arrière-plan, pas de secret dans les commandes.

## 1. Vérifier que le proxy écoute

```powershell
netstat -ano | Select-String ":4000\s+.*LISTENING"
```

Attendu : une ligne `0.0.0.0:4000 … LISTENING <PID>`.

## 2. Vérifier l'IP directe

```powershell
curl.exe -s --max-time 15 https://api.ipify.org
```

Attendu : `37.69.230.71` (ou l'IP publique courante de l'hôte).

## 3. Ping free

```powershell
$body = @{ model = "deepseek-v4.1-flash-free"; messages = @(@{ role = "user"; content = "ping" }); stream = $false } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri "http://127.0.0.1:4000/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 6
```

Attendu : `choices[0].message.content = "pong"`.

## 4. Vérifier la garde `<env>` et `tool_choice: none`

```powershell
$body = @{
  model = "deepseek-v4.1-flash-free"
  messages = @(
    @{ role = "system"; content = "You are an AI agent running in OpenCode, a coding agent harness.`n<env></env>" },
    @{ role = "user"; content = "Dis bonjour en un mot." }
  )
  tools = @(@{ type = "function"; function = @{
    name = "read"; description = "Read a file."
    parameters = @{ type = "object"; properties = @{ path = @{ type = "string" } } }
  }})
  tool_choice = "none"
  stream = $false
} | ConvertTo-Json -Depth 8
Invoke-RestMethod -Uri "http://127.0.0.1:4000/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 6
```

Attendu : 200, pas de `429 FreeUsageLimitError`.

## 5. Vérifier le stream

```powershell
$body = @{ model = "deepseek-v4.1-flash-free"; messages = @(@{ role = "user"; content = "Réponds exactement: OK" }); stream = $true; max_tokens = 16 } | ConvertTo-Json -Depth 5
curl.exe -s -N -X POST "http://127.0.0.1:4000/v1/chat/completions" -H "Content-Type: application/json" -d $body --max-time 120 | Select-Object -Last 5
```

Attendu : des lignes `data: …` et `data: [DONE]`.

## 6. Vérifier WARP (station 2)

```powershell
docker ps --format "{{.Names}} {{.Status}}" | Select-String "warp"
curl.exe -s --max-time 20 --socks5-hostname 127.0.0.1:11080 https://api.ipify.org
python -W ignore scripts\test_free_egress.py --proxy socks5h://127.0.0.1:11080 --client cli --project global --model deepseek-v4.1-flash-free --timeout 60 2>$null
```

Attendu : `104.28.230.202`, puis un JSON `status: 200`.

## 6.1 Vérifier le pont direct et la rotation

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_direct_socks5.ps1
curl.exe -s --max-time 20 --socks5-hostname 127.0.0.1:11084 https://api.ipify.org
```

Attendu : `37.69.230.71` (ou l'IP publique de l'hôte).

Le pool est en `proxy_mode: socks5` avec **trois entrées** : WARP IPv4
(`11080`, `warp-ipv4`), WARP IPv6 (`11080`, `warp-ipv6`, `ipv6: true`) et direct
(`11084`, `direct`). Vérifier l'IPv6 :

```powershell
curl.exe -6 -s --max-time 20 --socks5-hostname 127.0.0.1:11080 https://cloudflare.com/cdn-cgi/trace
```

Attendu : `ip=2a09:bac5:...`, `warp=on`.

Puis plusieurs requêtes et la DB :

```powershell
python -W ignore .artifacts\audit-2026-11-05\query_recent_requests.py
```

Attendu : des lignes `free_model_ip` `2a09:bac5:32d4:be::13:23e`,
`socks5:warp-ipv4` et `socks5:direct`, toutes `success: 1`.

## 7. Vérifier les plafonds de cooldown

```powershell
Select-String -Path config.yaml -Pattern "cooldown_cap_s|ledger_max_block_s"
```

Attendu :

```
free:
  cooldown_cap_s: 1200
ip_quota:
  ledger_max_block_s: 1200
```

## 8. Vérifier le ledger

```powershell
python -c "import sqlite3; c=sqlite3.connect('logs/registry.db'); print(c.execute('select name from sqlite_master where type=''table''').fetchall())"
```

Puis, si la table `quota_buckets` existe :

```powershell
python -c "import sqlite3, json; c=sqlite3.connect('logs/registry.db'); c.row_factory=sqlite3.Row; rows=[dict(r) for r in c.execute('select ip, day, bucket, requests, successes, failures, retry_after, reset_at, exhausted from quota_buckets order by last_seen_at desc limit 10')]; print(json.dumps(rows, indent=1))"
```

## 9. Lancer la suite ciblée

```powershell
python -W ignore -m pytest tests -q -p no:warnings -k "free or impersonation or official or wire or identity or quota or pool or trial_dead"
```

Attendu : exit 0.

## 10. Redémarrer le proxy

```powershell
$pid = (Get-NetTCPConnection -LocalPort 4000 -State Listen).OwningProcess
Stop-Process -Id $pid -Force
Start-Sleep -Seconds 2
Start-Process -FilePath "C:\Users\julie\AppData\Local\Programs\Python\Python312\pythonw.exe" -ArgumentList "opencode.py" -WorkingDirectory (Get-Location)
Start-Sleep -Seconds 8
netstat -ano | Select-String ":4000\s+.*LISTENING"
```

## Ne pas faire

- **Ne pas tester la réutilisation de session** sur l'IP directe : c'est le test
  de shadow-ban d'afree, et il peut coûter l'IP jusqu'à minuit UTC. Utiliser
  WARP ou une IP VPN sacrifiable.
- **Ne pas lancer six stations VPN** sans avoir mesuré la limite de sessions :
  six stations + un test = `AUTH_FAILED`.
- **Ne pas changer l'UA** sans re-capture 2.2.5 : le tuple UA/TLS/corps doit
  rester cohérent.
- **Ne pas supprimer `free/captures/2.2.5.json`** : c'est la référence du wire.
- **Ne pas committer de token NordVPN** ou de credential dans `config.yaml`,
  `credentials.env` ou les docs.

## Où lire ensuite

- [spec.md](./spec.md) — objectifs et critères.
- [research.md](./research.md) — fiche par dépôt.
- [plan.md](./plan.md) — changements et décisions.
- [measurements.md](./measurements.md) — mesures datées.
- [contracts/](./contracts/) — invariants.
- [../../docs/AUDIT_REPOS_2026-11-06.md](../../docs/AUDIT_REPOS_2026-11-06.md)
  — audit consolidé.
