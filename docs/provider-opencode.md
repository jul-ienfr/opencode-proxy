# Provider OpenCode — observabilité, DB, performance, configuration

> [plan v10 §11.4] Tranches doc des Lots 4/5/6. État réel au 2026-08-25,
> vérifié par `tests/test_docs_drift.py` via `docs/_drift_manifest.json`.

## Observabilité par station

### Filtres `?station=N`

Les endpoints de données acceptent le filtre par station (colonne
`requests.station`, `NULL` = requêtes payées/directes) :

- `GET /api/stats?station=2`
- `GET /api/stats/timeseries?station=2`
- `GET /api/history?station=2&limit=20&offset=0` (limit clampé ≤200)

### Latence-adaptive (moteur §3.6)

`GET /api/vpn-status` expose :

- `latency` : `{\"<sid>|<ip>\": {count, ewma_ms, p95_ms, consecutive_slow}}`
  (EWMA calculée en espace log, p95 glissant sur fenêtre 20)
- `rotation_paused`, `global_degraded_remaining` : état des garde-fous

Décisions (bloc canonique `ip_rotation.latency_rotation`) :
`consecutive_slow ≥ 3` OU (`ewma > seuil_per_model` ET count ≥ 5) OU
(`p95 > p95_threshold` ET count ≥ 5). Warm-up : la 1ʳᵉ requête sur une IP
neuve n'est jamais comptée lente. Streaming : mesure en **TTFB**
(`stream_metric: ttfb`).

### Logs par station

`GET /api/vpn/station/{id}/logs?lines=80` — tail docker borné (10-300),
lecture seule, offloadé thread.

## Base de données (logs/requests.db)

### Schéma ajouté au Lot 4

```sql
ALTER TABLE requests ADD COLUMN station INTEGER DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_requests_station_ts ON requests(station, timestamp);
```

La valeur `station` provient automatiquement du contextvar free
(`_current_free_attempt`) dans `_save_request` — batch et fallback sync.

### Patterns d'écriture/lecture

- **Writer** : queue asyncio → batch ≤32 items ou 0,05 s, commit via
  `asyncio.to_thread` ; vidange du batch dans `finally` (jamais de doublon
  sur exception) ; `/health` sonde sous le même lock que le writer.
- **Lectures dashboard** : WHERE paramétré par `_build_where(station=...)`,
  index composé utilisé (vérifiable par `EXPLAIN QUERY PLAN`).
- **Maintenance** (hebdo, hors trafic) : `PRAGMA wal_checkpoint(TRUNCATE)`
  AVANT `VACUUM` ; exécution tolérante au busy (jamais de crash si lock).

## Performance — règles et budget

| Réglage | Valeur/règle | Source |
|---|---|---|
| Hedge winner | premier statut **<400** gagne ; erreurs HTTP = filet seulement | §14.1.5 |
| `hedge_delay_ms_per_model` | carte optionnelle par modèle + jitter ±20 % | §12.1.2 |
| Watchdog logs | auth+server_issue partagent UN `docker logs` ; churn-scan cadencé 60 s | §14.1.10 |
| `_debug` gating | sites chauds gardés ; ne pas sérialiser en boucle SSE | §14.3.1 |

Budgets mesurables : `python scripts/bench_perf.py` (lock <50 ms, atomic
write, sqlite index p95) — comparaison baseline, régression >20 % = exit 2.

## Configuration

### Overrides par station

```yaml
ip_rotation:
  per_station:
    "2":
      quota_per_ip: 500
      country_offset: 3
      country_offset_stride: 0   # neutralise le stride global pour cette station
```
Fusionné PAR-STATION (copie défensive — la base partagée reste intacte) ;
hot-reloadable via `update_config`. Le stride global
(`ip_rotation.country_offset_stride`) écarte les pays tirés entre stations.

### Bloc canonique `latency_rotation`

Source unique : `config.yaml` → `ip_rotation.latency_rotation`
(voir `docs/gluetun-station.md` pour la sémantique tunnel/DNS/MTU côté
conteneurs). Hot-reload : pool.update_config propage au moteur.

### Toggles d'urgence

| Toggle | Effet | Persistance |
|---|---|---|
| `rotation_paused` (POST /api/vpn/rotation-paused) | gèle soft/hard rotate | éphémère (volontaire) |
| `supervisor.enabled: false` | chemin orchestration legacy | config.yaml |
| `soft_rotate.paused` (§3.8) | idem rotation_paused, vue canonique | config.yaml |
