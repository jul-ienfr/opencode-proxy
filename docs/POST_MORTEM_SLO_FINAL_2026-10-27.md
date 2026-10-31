# Rapport SLO final — fenêtre 24 h

- Généré : `2026-10-27T13:50:33Z`
- Fenêtre demandée : `2026-10-26T13:49:28Z` → `2026-10-27T13:50:28Z`
- Source : `logs\station_slo_final.jsonl`

# Rapport SLO stations

- Fenêtre : `2026-10-26T13:49:28Z` → `2026-10-27T13:49:56Z` (1440 échantillons)
- Échantillons valides : 1440 / erreurs : 0
- Latence API : p50 16 ms · p95 250 ms · max 1297 ms
- Âge de l'état : p95 60 s · max 79 s
- Flotte complète (healthy+routable) : 1331/1440 = 92.4 %
- Routable 6/6 : 1331/1440 = 92.4 %
- `boot_error` : 78/1440 = 5.4 %
- `stale` : 0/1440 = 0.0 %
- `refresh_error` : 0/1440 = 0.0 %

| Station | Disponible | Avec IP | Degraded | Error | Secondary | Flap max | Rotations |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 97.6 % | 81.2 % | 1.7 % | 2.4 % | 0.0 % | 26 | 22 |
| 2 | 99.8 % | 97.9 % | 0.0 % | 0.2 % | 0.0 % | 5 | 14 |
| 3 | 97.6 % | 86.9 % | 1.3 % | 2.4 % | 0.0 % | 24 | 20 |
| 4 | 99.9 % | 77.7 % | 0.1 % | 0.1 % | 0.0 % | 10 | 10 |
| 5 | 99.7 % | 88.2 % | 0.0 % | 0.3 % | 0.0 % | 5 | 23 |
| 6 | 99.7 % | 75.2 % | 0.1 % | 0.3 % | 0.0 % | 22 | 19 |

## Qualité et upstream
- Probe TTFB (station-échantillon) : p50 1269 ms · p95 2086 ms · max 9915 ms
- TTFB requêtes (EWMA) : p50 7355 ms · p95 22892 ms
- TTFB requêtes (p95 échantillonné) : p50 6156 ms · p95 43156 ms
- Taux de succès (snapshots) : moyenne 99.6 % · min 33.3 %
- 429 récents : max 1 / station
- Rotations (fenêtre) : hard=0 · soft=0
- Fallback payant (fenêtre) : total 0
- Statuts free (fenêtre) : leg_status:free:200=+2935, leg_status:free:429=+13

| Station | Probe p50 | Probe p95 | EWMA p50 | EWMA p95 | Succès | 429 max | Score moyen |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1419 ms | 5432 ms | 6969 ms | 25928 ms | 100.0 % | 0 | 0.699 |
| 2 | 1258 ms | 1700 ms | 13298 ms | 20625 ms | 100.0 % | 1 | 0.693 |
| 3 | 1394 ms | 5416 ms | 15476 ms | 21818 ms | 100.0 % | 1 | 0.680 |
| 4 | 1214 ms | 1632 ms | 22892 ms | 22892 ms | 100.0 % | 1 | 0.656 |
| 5 | 1323 ms | 1756 ms | 3231 ms | 10054 ms | 100.0 % | 1 | 0.733 |
| 6 | 924 ms | 1464 ms | 5899 ms | 31713 ms | 100.0 % | 1 | 0.709 |

## Motifs non-routables
- `auth-cooling` : 46
- `status=error` : 30
- `bad_until 57s` : 5
- `status=connecting` : 4
- `bad_until 21s` : 3
- `bad_until 59s` : 3
- `bad_until 56s` : 3
- `bad_until 24s` : 3
- `bad_until 32s` : 2
- `bad_until 53s` : 2
