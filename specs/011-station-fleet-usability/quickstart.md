# Quickstart — 011 Station fleet usability

**But** : prouver, en conditions réelles, que les 6 stations NordVPN et les 6
endpoints WARP sont client-utilisables, avec 12 buckets uniques, sans boucle de
reconcile ni doublon.

## Prérequis

- Windows + Docker Desktop démarré.
- `logs/registry.db` sauvegardé (`VACUUM INTO`).
- `.env` et `config.yaml` sauvegardés.
- Proxy arrêté ou en mode maintenance.
- Accès au dashboard local et au token si `server.host != 127.0.0.1`.

## 0. Baseline (avant changement)

```powershell
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
python scripts/station_audit.py --json > logs/station-audit-before.json
Get-NetTCPConnection -LocalPort 4000 -State Listen
```

**Attendu baseline 2026-11-07** : 5 conteneurs NordVPN, 0 healthy, 6 WARP, 5
`/64` uniques, proxy non démarré. Le fichier sert de comparaison, pas de
référence.

## 1. Tests unitaires purs

```powershell
python -m pytest tests/test_desired_state.py tests/test_reconciler.py -q
python -m pytest tests/test_warp_provisioner.py tests/test_egress_reconcile.py -q
python -m pytest tests/test_station_lifecycle.py tests/test_capacity.py -q
python -m pytest tests/test_warp_rotation.py -q
```

**Attendu** : 0 échec, aucun Docker requis, durée < 60 s.

## 2. Migration du registre (dry-run)

```powershell
python -m rotation.infrastructure.db.migrate --dry-run
python -m rotation.infrastructure.db.migrate --apply
python -c "import sqlite3; c=sqlite3.connect('logs/registry.db'); print(c.execute('select count(*) from desired_state').fetchone()[0])"
```

**Attendu** : `12` desired states (6 NordVPN + 6 WARP), `integrity_check=ok`,
backup créé, 0 doublon de bucket.

## 3. Reconcile en shadow

```powershell
# config.yaml : station_lifecycle.mode = shadow
python opencode.py
```

**Attendu** :

- le proxy écoute sur `:4000` ;
- `/api/stations` renvoie 12 stations ;
- aucune action Docker réelle n'est exécutée ;
- les décisions `create/recreate/noop` sont journalisées ;
- 0 `rm -f` injustifié.

Vérifier :

```powershell
curl.exe -s http://127.0.0.1:4000/api/stations | ConvertFrom-Json | Select-Object target_slots
curl.exe -s http://127.0.0.1:4000/api/egress/capacity | ConvertFrom-Json
```

## 4. Canary station 1 + WARP 1

```powershell
# config.yaml : station_lifecycle.mode = canary ; canary_providers = [nordvpn:1, warp:1]
python scripts/station_audit.py --provider nordvpn --json
python scripts/station_audit.py --provider warp --json
```

**Attendu** :

- station 1 `client_usable` en < 180 s ;
- WARP 1 `client_usable`, bucket unique ;
- aucune autre station modifiée ;
- `job_id` visible dans le registre.

## 5. Enforce 12 stations

```powershell
# config.yaml : station_lifecycle.mode = enforce
python opencode.py
python scripts/station_audit.py --json --fail-on degraded
```

**Attendu** :

```json
{
  "ok": true,
  "target_slots": 6,
  "providers": {
    "nordvpn": {"client_usable": 6, "unique_buckets": 6, "status": "ok"},
    "warp": {"client_usable": 6, "unique_buckets": 6, "status": "ok"}
  },
  "duplicates": [],
  "verdict": "ok"
}
```

## 6. Preuves client réelles

```powershell
curl.exe -s --max-time 20 --socks5-hostname 127.0.0.1:1080 https://api.ipify.org
curl.exe -s --max-time 20 --socks5-hostname 127.0.0.1:11080 https://api6.ipify.org
python scripts/station_audit.py --provider nordvpn --evidence
python scripts/station_audit.py --provider warp --evidence
```

**Attendu** : 6 IP NordVPN distinctes, 6 `/64` WARP distincts, probes client
avec contenu (pas seulement un 200 de page d'accueil).

## 7. Rotation sans perte de slot

```powershell
python scripts/station_audit.py --provider warp --json
curl.exe -s -X POST http://127.0.0.1:4000/api/stations/warp/2/rotate -H "Content-Type: application/json" -d '{"trigger":"manual","reason":"test"}'
python scripts/station_audit.py --provider warp --json
```

**Attendu** :

- l'endpoint 2 change de `/64` ;
- les 5 autres endpoints restent `client_usable` ;
- `rotation_jobs` contient une ligne `ok` ;
- le budget horaire est décrémenté et persisté.

## 8. Doublon de bucket

État injecté : deux endpoints WARP dans le même `/64`.

```powershell
python scripts/station_audit.py --json --fail-on degraded
```

**Attendu** :

- `duplicates` contient le bloc et les deux labels ;
- `unique_buckets = 5` ;
- `verdict = degraded` ;
- une rotation ciblée est proposée, jamais silencieuse.

## 9. Réparation par cause

| Scenario | Injection | Attendu |
|---|---|---|
| Host mort | pin `us10506.nordvpn.com` | re-pin vers un host vivant, pas de restart infini |
| Tunnel mort | `docker kill` du tunnel | recreate une fois, `client_usable` en < 180 s |
| Quota | 429 avec `Retry-After` | `quota_429`, `reset_at` respecté, pas de probe avant |
| Stack divergent | conteneur openvpn, desired wireguard | un recreate, pas de boucle |
| Docker down | arrêt Docker Desktop | mode dégradé explicite, pas de suppression d'état |
| Proxy down | arrêt du proxy | `/healthz` down, stations intactes |

## 10. Soak 24 h

```powershell
python scripts/station_slo_watch.py --duration 86400 --interval 30
python scripts/station_audit.py --json > logs/station-audit-after.json
```

**Critères** :

- 6/6 NordVPN et 6/6 WARP `client_usable` ≥ 99 % du temps ;
- 12 buckets uniques, 0 doublon ;
- MTTR < 180 s ;
- < 3 restarts/h/station ;
- 0 `rm -f` injustifié ;
- 0 réutilisation avant `reset_at` ;
- 0 faux `ok` de capacité.

## 11. Nettoyage / rollback

```powershell
# revenir en shadow
# config.yaml : station_lifecycle.mode = shadow
python opencode.py
```

Si la migration échoue :

```powershell
Copy-Item logs/backups/registry-<ts>.db logs/registry.db -Force
```

## Références

- [spec.md](./spec.md)
- [plan.md](./plan.md)
- [data-model.md](./data-model.md)
- [contracts/station-lifecycle.md](./contracts/station-lifecycle.md)
- [docs/AUDIT_STATIONS_NORDVPN_WARP_2026-11-07.md](../../docs/AUDIT_STATIONS_NORDVPN_WARP_2026-11-07.md)
