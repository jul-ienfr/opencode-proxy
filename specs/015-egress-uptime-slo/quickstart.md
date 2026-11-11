# Quickstart — validation de la disponibilité des egress

**Feature** : `015-egress-uptime-slo`
**But** : vérifier la baseline, rejouer les scénarios d'acceptation et prouver
le SLO sans attendre la fin de l'implémentation.

---

## 1. État courant en 30 secondes

```powershell
# Proxy vivant ?
(Invoke-WebRequest http://127.0.0.1:4000/v1/models -UseBasicParsing).StatusCode

# Les deux modèles free répondent ?
$body = @{ model='deepseek-v4.1-flash-free'; messages=@(@{role='user'; content='OK'}); max_tokens=5 } | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:4000/v1/chat/completions -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 120

# Flotte et capacité
Invoke-RestMethod http://127.0.0.1:4000/api/pool-status | ConvertTo-Json -Depth 4
Invoke-RestMethod http://127.0.0.1:4000/api/free-health | ConvertTo-Json -Depth 4

# Dimensionnement : exactement 6 stations, jamais une 7e
$s = (Invoke-RestMethod http://127.0.0.1:4000/api/pool-status).stations
"stations=$($s.Count)"
if ($s.Count -ne 6) { throw "Dimensionnement invalide : $($s.Count) stations" }

# Superviseur
Get-Content logs\fresh_chain_supervisor.log -Tail 30
```

Lecture : un proxy 200 + des modèles 200 + des stations 0/6 signifient
« service rendu par les egress de secours », pas « proxy cassé ». Le SLO cible
porte sur les stations.

---

## 2. Baseline à figer avant tout correctif

| Métrique | Commande | Cible |
|---|---|---|
| Stations `client_usable` | `/api/pool-status` | 6/6 |
| Pause AUTH | `Select-String auth pause logs\fresh_chain_supervisor.log` | ≤ 60 s |
| Chaînes actives | `cycle: broken=... running=...` | 3–4 slots utiles |
| Capacité élargie (hors 6/6) | `/api/free-health` | secours disponible |
| Latence modèle | requête réelle | p95 ≤ 30 s |
| SLO 1 h | `/api/availability?window=1h` (après T032) | ≥ 99 % |

Conserver les 30 dernières lignes de log et la sortie JSON dans
`docs/evidence/015-<date>/`.

---

## 3. Tests ciblés des correctifs P0

```powershell
# Gagnant frais, guard AUTH, réparateur unique, ledger
python -m pytest tests\test_station_allocator_supervisor.py -q
python -m pytest tests\test_station_repair.py -q
python -m pytest tests\test_registry_db_runtime.py -q
python -m pytest tests\test_fresh_chain_lock.py -q

# Gates
python -m ruff check .
python -m pytest -q
```

Chaque test P0 doit échouer avant le correctif et passer après. Conserver la
sortie dans `docs/evidence/015-<date>/pytest-p0.txt`.

---

## 4. Scénarios d'acceptation manuels

### AT-01 — OpenVPN UDP bloqué

1. Choisir une station de test (par ex. s6) et noter son IP et son transport.
2. Bloquer UDP 1194 pour cette station (règle pare-feu locale ou profil de test
   du binder).
3. Provoquer une réparation (API dashboard ou `POST /api/repair` selon
   l'endpoint actif).
4. Vérifier dans le log : `openvpn-udp` → `openvpn-tcp` → `wireguard`, puis un
   200.
5. Mesurer le temps entre la panne et le 200 : cible ≤ 60 s.

### AT-03 — Orage AUTH

1. Provoquer deux AUTH_FAILED sur deux stations en moins de 60 s (fixture de
   test ou identifiants invalides sur un profil jetable).
2. Vérifier : pause ≤ 60 s, aucune station WireGuard arrêtée, retour 6/6
   ≤ 2 min.
3. Vérifier que le log ne contient plus `auth pause 894s`.

### AT-04 — IP épuisée

1. Consommer le quota d'une IP jusqu'au seuil de drain (fixture ou trafic
   synthétique).
2. Vérifier : claim d'un spare, bind, 200, bascule, libération de l'ancienne IP.
3. Mesurer le cutover : p95 ≤ 20 s.

### AT-08 — NordVPN coupé

1. Arrêter les six stations NordVPN (ou le provider) pendant 15 min.
2. Vérifier que WARP/proxys maintiennent le service (requêtes 200), que le
   verdict 6/6 stations passe explicitement à 0/6 avec la cause
   `provider_down`, et qu'aucune station 7 n'est créée.
3. Vérifier que la réserve WARP/proxys n'était pas consommée avant l'incident.
4. Vérifier l'incident dans `/api/availability` avec la cause `provider_down`.

> WARP/proxys sont des egress de secours du **service**, pas des stations. Ils
> ne transforment jamais un 0/6 stations en 6/6.

### AT-09 — Reboot

1. Noter les leases, `reset_at`, `parked_until`.
2. Redémarrer le proxy.
3. Vérifier : aucun `parked_until`/`blocked_until` fantôme, leases cohérents,
   warm pool restauré.

---

## 5. SLO et alertes

```powershell
# Après T032/T034
Invoke-RestMethod "http://127.0.0.1:4000/api/availability?window=1h"
Invoke-RestMethod "http://127.0.0.1:4000/api/availability?window=24h"
Invoke-RestMethod "http://127.0.0.1:4000/api/availability?window=7d"
Invoke-RestMethod "http://127.0.0.1:4000/api/availability?window=30d"

# Métriques Prometheus
(Invoke-WebRequest http://127.0.0.1:4000/metrics -UseBasicParsing).Content |
  Select-String "availability_|six_of_six|cutover_|standby_"
```

Règle de lecture : une valeur sans `sample_count` et sans bornes de fenêtre
n'est pas une preuve (constitution I).

---

## 6. Rollback

Les changements sont derrière des flags :

```yaml
ip_registry:
  allocator:
    transport_ladder: false      # revenir au cycle historique
    standby_cutover: false       # revenir au bind direct
    max_parallel_repairs: 2      # valeur historique
availability:
  slo_v2: false                  # revenir au readmodel actuel
```

Procédure :
1. Basculer le flag à `false`.
2. Redémarrer le proxy et le superviseur.
3. Vérifier `/api/pool-status` et `/api/free-health`.
4. Conserver les logs et le rapport d'incident.

Aucun rollback ne doit supprimer `registry.db` ni les observations ; les
migrations sont réversibles par backup (`VACUUM INTO` vérifié).

---

## 7. Checklist opérateur avant de déclarer la phase terminée

- [ ] 6/6 `client_usable` pendant 1 h (Phase 0) / 24 h (Phase 2).
- [ ] Aucune pause AUTH > 60 s.
- [ ] Aucun gagnant frais effacé.
- [ ] Aucun 429 causé par nos sondes.
- [ ] Cutover p95 ≤ 20 s avec standby.
- [ ] WARP/proxys non consommés quand NordVPN est sain.
- [ ] SLO calculé depuis les échantillons bruts, avec bornes et `sample_count`.
- [ ] Reboot sans état fantôme.
- [ ] `pytest`, `ruff`, `config_audit`, `platform_audit`, option-map verts.
- [ ] Preuves archivées dans `docs/evidence/015-<date>/`.
