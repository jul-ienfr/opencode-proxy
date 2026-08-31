# Tuning opencode-proxy — DB, pool, cache, health (audit F-M4)

> Source: audit 2026-08-23 §8 + config.yaml + dashboard/api.py + gunicorn.conf.py

## Database SQLite (logs/requests.db)

```yaml
database:
  busy_timeout: 5000      # ms — wait on SQLITE_BUSY before error; 5000 balances contention vs latency
  cache_size: 64000       # pages → -64000 = ~256 MB (negative = KB). Large cache reduces I/O for token stats
  mmap_size: 268435456    # 268 MB mmap — memory-mapped I/O for fast scans, capped to avoid OOM on small hosts
  commit_interval: 5      # s — batch queue flush interval
  commit_batch: 10        # rows per commit batch (queue 10000, batch 32, timeout 50 ms in app/db)
```

- `journal_mode=WAL` + `synchronous=NORMAL` — WAL allows concurrent readers, NORMAL is safe with WAL (fsync at checkpoint only).
- WAL tradeoff: write throughput +10–30 % vs checkpoint stalls; mitigated by `wal_checkpoint_interval: 3600` (hourly TRUNCATE).
- `mmap 268 M` vs `cache_size 256 M`: mmap bypasses page cache for sequential scans, cache for random lookups — combined ~524 M worst-case, ok for 2 GB+ hosts, reduce `mmap` to 64 M on 1 GB hosts.
- `busy_timeout 5000` matches `health_cache_ttl 15` — health probes retry via thread offload, not via SQLite busy.

Mesure: `scripts/rotate_db.sh` (see `scripts/rotate_db.sh --vacuum`) + `SELECT COUNT(*)` before/after. Bench: `python -m pytest tests/test_db_wal.py -q` (future).

## HTTP pool httpx

```yaml
upstream:
  max_connections: 64
  max_keepalive: 32
  keepalive_expiry: 30
  pool: 5
```

- `max_connections 64` backpressure: above 64 concurrent upstream, httpx queues (timeout `pool 5` s) → 503 if saturated, protects Opencode. [P2.5 perf] défaut 500→64 : un proxy mono-utilisateur n'a jamais besoin de 500 conns ; FDs/mémoire libérés, pool toujours chaud. Hot-reloadable.
- `max_keepalive 32` reuse: 32 idle keep-alive slots, expiry 30 s — avoids TIME_WAIT storm.
- Bench: `scripts/bench.py --url http://localhost:4000 --stream`.

## Health caches

- `health_cache_ttl: 15` (global probe via docker exec + SOCKS5 GET) vs `status_cache_seconds: 2.0` (dashboard poll) — 15 s reduces docker exec storm, 2 s keeps GUI snappy.
- `maybe_reload_custom_routes()` poll `custom_routes_check_interval: 5` s — poll vs inotify: poll is cross-platform (Windows + Linux) and cheap (mtime stat), inotify would need `watchdog` dep and fails on network mounts.

## Gunicorn vs systemd

| Env | Entrypoint | Workers | Keepalive | Timeout |
|-----|------------|---------|-----------|---------|
| Prod (systemd) | `gunicorn -c gunicorn.conf.py opencode:app` | `cpu_count` (4 on 4c) | 15 | 600 |
| Dev | `python opencode.py --no-gui` | 1 (uvicorn) | 15 | 600 |

- `worker_connections 2000`, `graceful_timeout 5` — [P1.4] pas de `max_requests` : le recycle périodique de l'unique worker détruirait l'état mémoire (piles VPN, SSE, caches) et couperait les streams en pleine session.

## Cache prompt

```yaml
cache:
  max_size: 1000
  ttl: 300
  min_prompt_size: 2000
```

- `CACHE_MIN_PROMPT_SIZE 2000` tokens: below, cache overhead > gain. `max_size 1000` entries, TTL 300 s — bench: `scripts/bench.py` cache hit rate.

## Capture

```yaml
traffic:
  max_frames: 500
  body_cap: 65536
  max_bytes: 33554432  # 32 MiB ring
```

- Pure-ASGI ring buffer 500 frames, 64 KiB per body, 32 MiB total — budgets for VPS with 1 GB RAM.

## Clés plan-30/08 (optimisation / fiabilisation perf)

Nouvelles clés `ip_rotation` (toutes bornées, hot-reloadées par
`VPNManager.update_config` ; `bad_ttl*` relues depuis `self._config`) :

| Clé | Défaut | Bornes | Objet |
|---|---|---|---|
| `bad_ttl` | `10` (ex-`20`) | 1–4 320 min | TTL de base du ban fast-pin A2, en **minutes** |
| `bad_ttl_factor` | `2` | 1–10 | Multiplicateur progressif à chaque re-échec |
| `bad_ttl_max` | `1440` | 1–43 200 min | Plafond du ban progressif |
| `breaker_warmup_grace_s` | `60` | 10–600 s | Fenêtre post-boot : failures de sondes non comptées breaker |
| `station_max_rotations_per_hour` | `10` | 1–720 | Seuil anti-churn A4 |
| `rotation_storm_cooldown_s` | `600` | 30–7 200 s | Cooldown forcé au-dessus du seuil |
| `cascade_max_duration_s` | `120` | 30–600 s | Budget de la rotation en cascade |
| `stack_age_guard_s` | `600` | 60–3 600 s | Âge max d'un stack avant relecture |
| `wg_canary_poll_interval_s` | `4` | 0.5–60 s | Cadence des sondes canary WG |
| `station_bad_ttl_s` | `60` | 1–3 600 s | TTL `bad` après 429 côté `FreeIPPool` |
| `station_connect_retry_interval_s` | `300` | 5–3 600 s | Intervalle retry docker station down |

Autres sections :

- `supervisor.warmup_excluded_requests` (0–1000, défaut `1`) — requêtes
  warm-up exclues des statistiques de latence (post-rotation).
- `database.weekly_purge_days` (défaut `90`, `0` = off) — purge hebdo des
  lignes > N jours dans `requests` + `free_model_usage` (dim. 03:00, avant
  le VACUUM). Job manuel : `powershell scripts/rotate_db.ps1 [-DryRun] [.Vacuum]`.

Sonde externe : `GET /health` renvoie un payload léger par défaut (B3).
Pour les graphes agrégés de consommation, utiliser `/health?detail=full`
(usage par modèle inclus) ou le dashboard Grafana (collecteur dédié
`/api/dashboard/*`).

## AUTH_FAILED NordVPN — diagnostic

A5 constat incident du 30/08 : AUTH_FAILED n'est **pas** un bug du proxy
mais la limite littérale des sessions simultanées NordVPN (6 sessions/
compte, tunnel par session consommé — 4 stations + 2 autres machines/routeur
atteignent très facilement la limite).

Signatures :

- Log NordVPN : `403 Forbidden` à la demande de session suivie par
  `AUTH_FAILED` côté gluetun.
- Station qui fly très vite avec probes ascendante OK (tunnel local up)
  puis down dès la rotation effective (soumission de la session).

Actions :

1. Compter les sessions actives sur le compte NordVPN (IHM compte
   nordvpn.com → "Devices") — libérer un slot avant toute recherche code.
2. Le watchdog gère AUTH_FAILED via restart + re-élection du pays
   (chemin existant, inchangé A5 = **veto** au gel des stacks ; pas de
   masquage automatique, pas d'état figé) ; la station reste dans la
   boucle dès qu'une session redevient disponible.
3. Le flip automatique reste disponible via `auto_flip_cooldown_min` /
   `auto_ov_return_min` — le compte de sessions n'entre en jeu que quand
   une *session* ne peut être ouverte, pas en tant que cap d'IP.

Points **non touchés volontairement** (veto confirmé 31/08) :

- A5 : pas de gel de stacks (`auto_flip wg→ov`, suppression station
  flip-to-wg) — l'utilisateur a confirmé que le proxy **doit pouvoir
  utiliser tous les modes automatiquement**, le problème AUTH_FAILED se
  corrige par la gestion de sessions compte (ou réduction des stations),
  pas par une option qui fige le pool.
- B4 : image gluetun épinglée à `v3.41.3` (digest stable, éviter la
  casse SOCKS5 observée sur un digest précédent). Ne jamais dégrader :
  rester sur `v3.41.3` ou monter ; si `latest` se recasse plus tard,
  la hot-lane `/api/vpn/flip` reste le recours manuel.

## Refs

- `config/settings.py:36-73` + `dashboard/api.py:457,1822` — see inline comments.
- `gunicorn.conf.py` — workers `cpu_count`, `UvicornWorker`, `preload False`.
