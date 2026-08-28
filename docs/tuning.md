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

## Refs

- `config/settings.py:36-73` + `dashboard/api.py:457,1822` — see inline comments.
- `gunicorn.conf.py` — workers `cpu_count`, `UvicornWorker`, `preload False`.
