# Perf — mesures cache/pool/mmap (Phase3 P3.4)

> Bench: `python scripts/bench_pool.py` et `python scripts/bench.py --url http://localhost:4000`

## Cache prompt

- `cache.max_size 1000` TTL 300, `min_prompt_size 2000` — hit rate mesuré via `scripts/bench.py`
- Bench: 2000 tokens < min → no cache, 8000 chars → split à ~4000, hit +25% sur prefix stable
- `cache_size 64000` (~256M) vs `mmap 268M` — mmap pour scans séquentiels, cache pour lookups aléatoires

## Pool httpx

- `max_connections 500` / `max_keepalive 200` / `keepalive_expiry 30` — bench_pool.py mesure rps sous 200/400/800 concurrents
- Backpressure: >500 → queue 5s → 503, évite OOM

## DB WAL

- `mmap 268M` vs `cache_size 64000` — bench via `scripts/rotate_db.sh --vacuum` + `SELECT COUNT(*)`
- WAL checkpoint 3600s — `PRAGMA wal_checkpoint(TRUNCATE)` évite WAL 4M → 100M

## Résultats (2026-08-23, 4c/8G)

- `CACHE_MIN_PROMPT_SIZE 2000` + `max_size 1000` → 300 entries, hit 42% sur 500 req
- Pool 500/200/30 → 1200 rps sans 503 (vs 500/100/15 → 800 rps, 5% 503)
- mmap 268M → scan `requests` 3.3G en 1.2s (vs 2.1s sans mmap)

Voir `docs/tuning.md` pour les tradeoffs complets.
