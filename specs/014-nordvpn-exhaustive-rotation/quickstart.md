# Quickstart — qualification exhaustive NordVPN

**Feature** : `014-nordvpn-exhaustive-rotation`

Ce guide vérifie la feature sans Docker pour le domaine, puis avec les six
stations pour le canary.

---

## 1. Pré-requis

- `logs/nordvpn_api.json` présent (snapshot API, 7 070 serveurs).
- `logs/registry.db` accessible ; backup fait.
- Python 3.12, dépendances du dépôt.
- Aucun trafic payant, aucun second compte.

---

## 2. Tests unitaires (sans réseau, sans Docker)

```powershell
python scripts/quality_gate.py --with-soak
```

Attendu : pytest, ruff, mypy, config, ratchet, cycles, benchmark, soak et
couverture passent ; aucun test n'ouvre de socket.

---

## 3. Ingestion shadow (aucun bind)

```powershell
python scripts/qualification_sweep.py --shadow --max 0
```

Vérifier :

```sql
SELECT COUNT(*) FROM qualification_candidates;
-- attendu : 7 070

SELECT status, COUNT(*) FROM qualification_candidates GROUP BY status;
-- attendu : pending=7070 (shadow), ou pending + skipped dédiés

SELECT hostname, potential_score, quota_remaining, rtt_ms, load
FROM qualification_candidates
ORDER BY potential_score DESC, quota_remaining DESC, rtt_ms ASC, load ASC, hostname ASC
LIMIT 20;
```

Vérifier que l'ordre n'utilise ni `country` ni `catalog.priority`.

---

## 4. Canary (une station, 100 hostnames)

```powershell
python scripts/qualification_sweep.py --station 1 --max 100 --epoch canary-1
```

Vérifier :

```sql
SELECT status, COUNT(*) FROM qualification_attempts
WHERE epoch_id='canary-1' GROUP BY status;

SELECT COUNT(*) FROM claims WHERE status='country_backoff';
-- attendu : 0

SELECT COUNT(*) FROM qualification_attempts
WHERE status='429' AND (reset_at IS NULL OR reset_at <= started_at);
-- attendu : 0
```

Attendu : 200/429/infra classés ; aucun pays bloqué ; aucun `429` en `dead`.

---

## 5. Balayage complet (six stations)

```powershell
python scripts/qualification_sweep.py --epoch epoch-20261109 --max 0
```

Le superviseur peut aussi lancer :

```powershell
python scripts/fresh_chain_supervisor.py
```

Vérifier la couverture :

```sql
SELECT status, COUNT(*) FROM qualification_candidates GROUP BY status;
SELECT tested, usable, quota, infra, dead, skipped, total
FROM qualification_epochs WHERE epoch_id='epoch-20261109';
```

Attendu : `tested + skipped = total` ; `cursor` vide en fin d'époque ;
`status='complete'`.

---

## 6. Dashboard / API

```powershell
curl http://127.0.0.1:4000/api/egress/qualification/summary
curl "http://127.0.0.1:4000/api/egress/qualification/next?limit=20"
curl http://127.0.0.1:4000/api/egress/qualification/epochs
curl http://127.0.0.1:4000/api/egress/qualification/quota
curl http://127.0.0.1:4000/api/egress/qualification/slo
curl http://127.0.0.1:4000/api/egress/qualification/metrics
curl "http://127.0.0.1:4000/api/egress/qualification/metrics?format=prometheus"
```

Vérifier :

- `total=7070`, `pending`, `usable`, `quota`, `auth`, `dead`, `skipped` ;
- les 20 prochains candidats sont dans l'ordre du score ;
- l'âge du dernier test est affiché ;
- le bouton lancer/pause agit sur l'époque.

---

## 7. Reprise après crash

1. Lancer une époque avec `--max 50`.
2. Tuer le process après 25 tentatives.
3. Relancer avec `--resume`.

```sql
SELECT COUNT(*) FROM qualification_attempts WHERE epoch_id='crash-1';
-- attendu : 25, pas 50

SELECT COUNT(DISTINCT hostname) FROM qualification_attempts WHERE epoch_id='crash-1';
-- attendu : 25, aucun doublon
```

---

## 8. Rotation sur 429

1. Prendre une IP `usable`.
2. Attendre ou simuler un 429.
3. Vérifier :

```sql
SELECT status, reset_at FROM egress_options WHERE egress_ip='<ip>';
-- attendu : quota, reset_at futur

SELECT hostname, potential_score FROM qualification_candidates
WHERE status='pending' ORDER BY potential_score DESC LIMIT 1;
```

La station doit claim le candidat suivant en moins de 60 s.

---

## 9. Non-régression pays

```sql
SELECT country, COUNT(*) FROM qualification_candidates
WHERE status='skipped' AND skipped_reason LIKE '%country%'
GROUP BY country;
-- attendu : 0 ligne
```

Aucun hostname ne doit être `skipped` à cause de son pays.

---

## 10. Performance

```powershell
python scripts/qualification_sweep.py --benchmark --hosts 500
```

Attendu : preflight de 500 hostnames < 5 min ; API summary p95 < 500 ms ;
UI p95 < 2 s.

---

## 11. Stations parallèles et cutover

1. Lancer six workers.
2. Provoquer un 429 sur la station 3 et un `AUTH_FAILED` sur la station 5.

```sql
SELECT station, state, current_egress_ip, standby_egress_ip
FROM station_runtime ORDER BY station;

SELECT station, from_egress_ip, to_egress_ip, duration_ms, result
FROM station_cutovers ORDER BY started_at DESC LIMIT 10;

SELECT station, COUNT(*) FROM incidents
WHERE scope='station' GROUP BY station;
```

Attendu : seules 3 et 5 changent d'état ; 1/2/4/6 conservent leur IP ; le
cutover de 3 est p95 < 2 s ; `station_runtime` est à jour.

---

## 12. Cycle multi-transports

Forcer un hostname qui supporte les trois transports :

```text
wireguard   -> no_egress
openvpn-udp -> 429
openvpn-tcp -> 200
```

```sql
SELECT transport, round, status FROM qualification_attempts
WHERE hostname='<host>' ORDER BY started_at;
```

Attendu : `wireguard`, `openvpn-udp`, `openvpn-tcp` dans cet ordre. Puis forcer
les trois en échec et vérifier un second round `wireguard`, sans `dead`.

Vérifier la santé :

```sql
SELECT transport, attempts, successes, quota, infra, degraded
FROM transport_health ORDER BY transport;
```

---

## 13. Watchdog et découverte

1. Tuer le tunnel de la station 2.
2. Vérifier `station_state` et `incidents` :

```sql
SELECT station, state, client_usable, last_probe_at, reason
FROM station_state WHERE station IN (1,2,3,4,5,6);

SELECT scope, station, cause, started_at, ended_at, remediation, result
FROM incidents ORDER BY id DESC LIMIT 10;
```

Attendu : une ligne `scope='station'`, `station=2` ; les autres stations
inchangées. Puis couper l'API catalogue :

```sql
SELECT source, status, server_count, stale, error
FROM discovery_runs ORDER BY started_at DESC LIMIT 5;
```

Attendu : fallback cache/embedded, `stale=1`, `server_count > 0`, file non
vidée.

---

## 14. Réparations

```sql
SELECT id, station, cause, status, attempts, result, last_error
FROM repair_jobs ORDER BY id DESC LIMIT 10;
```

Attendu : une seule réparation active par station, `registry.db` comme
autorité, `attempts` borné, `escalated` après `max_repair_attempts`.

---

## 15. Gates qualité

```powershell
python -m pytest tests/test_architecture.py tests/test_quality_gate.py -q
python -m pytest --cov=free --cov-report=term-missing -q
ruff check free tests
mypy free/qualification
lint-imports
python scripts/config_audit.py
python scripts/platform_audit.py --strict
python -m pytest tests/test_fault_injection.py -q
python benches/bench_qualification.py --budgets
```

Attendu : tous les gates du contrat `contracts/quality-gates.md` sont verts ;
un gate rouge bloque la release. Le soak 24 h est nightly.

---

## 16. Démantèlement des monolithes

Pour chaque slice (manager, pool, fresh_chain, dashboard, opencode) :

```powershell
python -m pytest tests/test_characterization_*.py -q
python scripts/module_size_audit.py --ratchet
python benches/bench_qualification.py --compare-baseline
```

Vérifier :

```text
vpn/manager.py        < 1 500 ou adapteur fin
free/pool.py          < 1 500
scripts/fresh_chain.py < 500 (CLI)
dashboard/api.py      < 1 500 ou routers
opencode.py           composition root < 1 500
D-02 / D-20           closed
p95                   ≤ +5 % vs baseline
```

Un slice sans tests de caractérisation, sans shadow/canary ou avec un flag
permanent n'est pas terminé.

---

## 17. Critères de sortie

- [ ] 7 070 candidats ingérés.
- [ ] 0 `country_backoff`.
- [ ] 0 `429` en `dead`.
- [ ] 100 % des tentatives ont `provider` et `hostname`.
- [ ] Époque complète avec `tested + skipped = total`.
- [ ] Reprise sans doublon.
- [ ] Rotation sur 429 < 60 s p95.
- [ ] Six stations parallèles ; 6/6 ≥ 99 % quand une IP 200 existe.
- [ ] Cutover p95 < 2 s avec standby ; 0 coupure si second tunnel.
- [ ] 0 pause globale ; 0 incident de station enregistré comme `fleet`.
- [ ] Cycle WG → OVPN UDP → OVPN TCP → WG observé ; OpenVPN diagnostiqué.
- [ ] `station_state`, `incidents`, `repair_jobs` et `discovery_runs` peuplés.
- [ ] Découverte 6 h, fallback cache/embedded, `stale` explicite.
- [ ] `vpn/manager.py`, `free/pool.py`, `dashboard/api.py`, `opencode.py`
  < 1 500 lignes ou entrées fines ; `fresh_chain.py` CLI < 500.
- [ ] D-02 et D-20 closes ; parité de comportement et p95 ≤ +5 % prouvées.
- [ ] 0 fenêtre visible.
- [ ] `platform_audit --strict` = 0 violation.
