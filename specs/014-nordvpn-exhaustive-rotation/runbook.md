# Runbook — control plane NordVPN 014

**Scope** : qualification, découverte, watchdog, réparation, rotation et
cutover de la jambe NordVPN.
**Autorité** : `logs/registry.db`.
**Sécurité** : toute commande `--live` rebinde une station réelle. En doute,
rester en shadow.

---

## 1. Pré-vol

```powershell
python -m pytest tests/test_qualification_*.py -q
python scripts/config_audit.py
python scripts/module_size_audit.py --root . --max-lines 1500 --ratchet specs/014-nordvpn-exhaustive-rotation/module-baseline.json
python scripts/dependency_graph.py --root free/qualification --fail-on-cycles
```

Attendu : tests verts, `config_audit` 0/0, ratchet 0 violation, 0 cycle
qualification.

---

## 2. Shadow (aucun bind)

```powershell
python scripts/qualification_sweep.py --config config.yaml --shadow --db logs/qualification.db --top 20
```

Vérifier :

```sql
SELECT status, COUNT(*) FROM qualification_candidates GROUP BY status;
SELECT hostname, potential_score, rtt_ms, load FROM qualification_candidates
ORDER BY potential_score DESC, quota_remaining DESC, rtt_ms ASC, load ASC
LIMIT 20;
```

---

## 3. Découverte

```powershell
python scripts/qualification_sweep.py --config config.yaml --discovery --force --db logs/qualification.db
```

Vérifier :

```sql
SELECT source, status, server_count, transport_count, stale, error
FROM discovery_runs ORDER BY started_at DESC LIMIT 5;
```

Attendu : `server_count > 0`, `stale=0` si l’API est fraîche, aucun catalogue
vide.

---

## 4. Watchdog

```powershell
python scripts/qualification_sweep.py --config config.yaml --watchdog --watchdog-url http://127.0.0.1:4000 --db logs/qualification.db
```

Vérifier :

```sql
SELECT * FROM watchdog_state;
SELECT station, state, client_usable, reason FROM station_state ORDER BY station;
SELECT id, scope, station, cause, started_at, ended_at, remediation FROM incidents
WHERE ended_at IS NULL ORDER BY id DESC;
SELECT id, station, cause, result, started_at, finished_at FROM repair_jobs
ORDER BY id DESC LIMIT 10;
```

Attendu : heartbeat frais, une seule réparation active par station, aucun
incident `fleet` pour une panne locale.

---

## 5. Balayage live (une station)

```powershell
python scripts/qualification_sweep.py --config config.yaml --live --station 1 --max 5 --db logs/qualification.db
```

ou via `fresh_chain` :

```powershell
python scripts/fresh_chain.py --sweep --station 1 --max 5 --claims-db logs/qualification.db
```

Attendu : `200` promu, `429` en `quota` avec reset, `no_egress` en infra,
aucun hostname `dead` sur un seul échec de transport.

---

## 5 bis. Réparation réelle d'une station

Le pool délègue les rotations au superviseur (`repair_owner: supervisor`) ;
`ip_registry.allocator.mode` doit être `enforce` pour qu'il agisse. En
`observe`, il journalise `observe: station N would repair (...)` sans toucher
au tunnel.

Réparation manuelle bornée, une station à la fois :

```powershell
python scripts/fresh_chain.py --station 2 --claims --transport openvpn-tcp --delay 5 --rebind-limit 8 --max 5 --refresh
```

Attendu : `claim <host>`, `egress <ip>`, `free status=200 ... valid=True`,
`WINNER station=N`, pin `.env` persisté, puis `client_usable=true` dans
`/api/vpn-status`.

Règles apprises :

- `CatalogClaims.claim_next` préfère les preuves 200 ; le backlog non mesuré
  ne préempte plus un serveur connu-200.
- Un échec de bind remet le hostname en `pending` avec backoff (jamais
  `claimed` permanent).
- Un verrou `fresh_chain_<station>_<transport>.lock` appartenant à un PID mort
  est repris automatiquement.
- La file de réparation est `logs/registry.db` ; les tests utilisent
  `OPENCODE_REPAIR_QUEUE_DB` pour ne jamais y écrire.

### Redémarrage du proxy (activation code/config)

```powershell
# arrêter l'ancien processus pythonw opencode.py --gui
$old = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*opencode.py --gui*' }
$old | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Milliseconds 900
Start-Process pythonw -ArgumentList 'opencode.py --gui' -WorkingDirectory `
  'C:\Users\julie\Downloads\opencode-proxy-one' -WindowStyle Hidden
```

Vérifier : `/api/vpn-status` répond, `quota_estimate=200`, providers
`nordvpn,warp,proxies,direct`, carte free à 5 entrées.

Après un changement de `quota_per_ip`, purger les 429 persistés :

```powershell
Invoke-RestMethod -Method Post -ContentType application/json `
  -Body '{"scope":"all"}' http://127.0.0.1:4000/api/free/quota/clear
```

Un watchdog qui voit seulement un quota épuisé écrit `DRAINING` et ferme
l'incident : ce n'est pas une panne de tunnel.

---

## 5 ter. Qualifieurs dédiés (stations 7–10)

Les six stations de production ne doivent jamais porter un balayage live. Le
manager `free/qualification/qualifiers.py` démarre les services Compose 7–10
en WireGuard et fait tourner une époque live par slot.

GUI — carte **« Qualifieurs dédiés »** (page Qualification) :

1. cocher `activer`, saisir `nombre` (1 à 4), **Appliquer** ;
2. vérifier le tableau des slots (`running`, `qualifier-7-*`, hosts/promus) ;
3. **Tout arrêter** pour annuler les tâches et stopper les conteneurs.

API équivalente :

```powershell
Invoke-RestMethod -Method Post -ContentType application/json `
  -Body '{"enabled":true,"count":1}' `
  http://127.0.0.1:4000/api/egress/qualification/qualifiers
```

Contrôles :

```powershell
docker ps --filter name=opencode-vpn-7
# CONTAINER ID ... Up ... opencode-vpn-7

python -c "import sqlite3;c=sqlite3.connect('logs/registry.db');print(list(c.execute(\"select epoch_id,status,tested,usable,round(finished_at-started_at,1) from qualification_epochs where epoch_id like 'qualifier-%' order by started_at desc limit 5\")))"
```

Règles :

- `shadow_mode: false` autorise les époques live ; `daemon_shadow: true` garde
  le tick automatique du superviseur en shadow ;
- le qualifieur est **WireGuard seul** : le control API change de hostname en
  ~5–15 s ; UDP/TCP restent réservés aux réparations `fresh_chain` ;
- `max_count: 4` mais `recommended_max: 2` : au-delà de 2, les sessions
  NordVPN supplémentaires peuvent provoquer des `AUTH_FAILED` sur les
  stations de production ;
- 1 slot ≈ 220–280 hosts/h, 2 ≈ 440–560 hosts/h, plafond pratique ~500/h ;
- si le proxy a démarré avant la fonctionnalité, le POST persiste la demande
  et répond `restart_required: true` ; redémarrer le proxy pour attacher le
  manager ;
- les conteneurs 7–10 sont arrêtés au shutdown et par **Tout arrêter**.

---

### Serveurs Dedicated IP : toujours `skipped`

Un hostname du groupe `legacy_dedicated_ip` ne peut jamais aboutir avec un
compte standard : le tunnel WireGuard est accepté (`vpn/status=running`) mais
le handshake reste muet (`rx_packets=0`, `public_ip=""`), d'où des tentatives
`bind:control-no-egress`. Ce n'est ni une panne de station ni une IP épuisée.

- `free/qualification/source.py` les marque `skipped` / `dedicated` dès
  l'ingestion ;
- `QualificationStore.upsert_candidates` met aussi à jour `status` et
  `skipped_reason` pour ces skips durs (`dedicated`, `denylisted`,
  `unsupported-transport`, `removed-from-catalog`) sans écraser
  `operator-paused` ni un statut mesuré ;
- après une ré-ingestion, contrôler :

```powershell
python -c "import sqlite3;c=sqlite3.connect('logs/registry.db');print(list(c.execute(\"select status,skipped_reason,count(*) from qualification_candidates group by status,skipped_reason\")))"
```

Attendu : `dedicated` uniquement dans `skipped`, aucun Dedicated IP encore
`pending` ou `claimed`.

### Époques interrompues et compteurs

- Un arrêt de slot (GUI « Tout arrêter », `count` réduit, redémarrage du
  proxy) annule `run_epoch` : l'époque est fermée en `cancelled`, l'host en
  cours de qualification est relâché en `pending` s'il n'a pas encore de
  verdict, et aucune époque ne reste `running`.
- Au premier tick suivant, le manager ferme aussi les époques orphelines de
  sa station (`close_running_epochs("qualifier-N-", status="partial")`).
- `tested` / `usable` / `quota` / `infra` d'une époque sont désormais les
  compteurs **de cette époque**, pas les totaux de la flotte. `last_promoted`
  dans le snapshot d'un slot est le nombre de 200 promus pendant la dernière
  époque ; la taille de la réserve se lit dans
  `/api/egress/qualification/summary` (`usable`, `pending`, ...).

---

## 6. Rotation / cutover

```python
# Exemple opérateur, dans un shell Python avec le runtime chargé
from free.qualification import RotationExecutor, RotationPlanner, CutoverService
```

Règles :

- standby validé avant promotion ;
- ancienne IP conservée jusqu’au 200 de la nouvelle ;
- rollback si switch/probe échoue ;
- dernière station protégée sans standby ;
- jamais deux stations sur la même IP.

---

## 7. Pause / reprise

- Pause globale legacy : `global_pause_enabled: true` (déconseillé).
- Pause par station : `StationWorker.pause("operator")`.
- Reprise : `StationWorker.resume()`.
- Vérifier `station_runtime.state`.

---

## 8. Incidents

1. Lire `incidents` et `station_state`.
2. Vérifier le heartbeat `watchdog_state`.
3. Si `AUTH_FAILED` : quarantaine station, pas host.
4. Si `no_egress` : changer transport/station avant backoff long.
5. Si `429` : respecter `reset_at`, ne pas re-tester avant.
6. Si réparation échoue `max_repair_attempts` : escalade, pas de boucle.

---

## 9. Canary six stations

```powershell
python scripts/canary_qualification.py --api-base http://127.0.0.1:4000 --target 6 --output logs/canary-014.json
```

Le runner vérifie : 6/6 `client_usable`, SLO 6/6 et disponibilité, heartbeat
watchdog sans incident ouvert, découverte fraîche, les trois transports
exercés avec succès, et au moins un cutover `committed`. Sortie 0 seulement si
toutes les preuves sont présentes.

Une fois la checklist entièrement cochée :

```powershell
python scripts/release_qualification.py --canary logs/canary-014.json --output logs/release-014-evidence.json
```

Le manifeste refuse un canary échoué ou une checklist incomplète, et archive
les SHA-256 des preuves.

---

## 10. Rollback

1. Backup : `VACUUM INTO logs/registry.db.bak-<date>`.
2. Désactiver `egress_catalog.qualification.enabled`.
3. Restaurer le backup si migration cassée.
4. `global_pause_enabled: true` uniquement pour un rollback temporaire.
5. Vérifier `PRAGMA integrity_check`.

---

## 11. Qualité

```powershell
python scripts/bench_qualification.py --candidates 5000 --json
python scripts/module_size_audit.py --ratchet specs/014-nordvpn-exhaustive-rotation/module-baseline.json
python scripts/dependency_graph.py --root free/qualification --fail-on-cycles
```

Budgets : upsert < 2 ms/item, score p95 < 0,5 ms, claim p95 < 20 ms.
