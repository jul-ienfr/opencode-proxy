# Plan — Proxy plus performant + fiable, sans régression (6 stations)

Date : 2026-09-06. Dimensionnement vérifié : `config.yaml:262` `station_count: 6`,
`docker-compose.yml:69-380` 6 tunnels `opencode-vpn` → `opencode-vpn-6`.

Docs existants à ne pas dupliquer : `docs/perf.md`, `docs/tuning.md`,
`docs/PLAN-performance.md`, `docs/PLAN-commun-performance-raisonnement.md`.

## 0. Recommandations tranchées

1. Pool curl `opencode.py:1581-1635` : `M=3→4/station` (24 sessions au lieu de
   18), garder `hedge_max 2` (`config.yaml:202-207`). Pas 5 : +RAM/churn
   NordVPN pour gain marginal.
2. Watchdog silence paid : 90 s sans byte → failover, pas alerte seule.
   `read 600s` (`config.yaml:12-16`) masque les tunnels morts ; le chemin free
   a déjà `cancel_streams` + probe (`ip_probe_budget:20s`), le paid non.
3. Coverage CI : rester à `--cov-fail-under=35` + figer baseline
   `scripts/bench_perf.py`. Monter à 60 % après le chantier, pas pendant.

## 1. Lot 0 — Baseline et garde-fous (0 risque, avant tout tuning)

- [ ] Figer `logs/bench_baseline.json` via `scripts/bench_perf.py --baseline`,
      ajouter job CI `--json` seuil 20 %.
- [ ] Ajouter compteurs : `TTFB upstream`, `curl checkout wait`,
      `DB QueueFull` (`opencode.py:1085-1094`), `hit-rate conversion`
      (`protocol_mapping.py:874-941`), `fetch_quotas sur 429`
      (`opencode.py:7023,7290`).
- [ ] Expliciter `-k "not docker"` en CI ; planifier `-m docker` +
      `vpn_e2e_smoke.py` sur runner avec daemon.
- [ ] Gate rapide dev à chaque lot :
      `pytest -k "not docker" -q` + ciblé
      `test_conversion_golden.py test_proxy.py test_streaming_sse.py
      test_sse_keepalive.py test_sse_coalesce.py test_b2_stream_double_failover.py
      test_plan30_optimisation.py`.

## 2. Lot 1 — Perf chemin chaud (sans changer le contrat)

Chemin chaud déjà sain : `httpx.AsyncClient` partagé `opencode.py:1732-1769`,
`http2`, `Limits(64/32/30s)`, `Timeout(5/600/10/5)` ; streaming
`_sse_pump opencode.py:7686-7801` coalescé 64 KiB + keepalive 15 s ;
conversion `protocol_mapping.py:874-941` cache LRU 512 + `orjson` fast-path.

- [ ] Pool curl : rendre `M` configurable par station, mesurer `wait p95`
      avant/après, passer à 4 seulement si `wait>200ms p95`.
      Goulot actuel : 1 req free avec hedge prend 2 stations ; ~10 req
      parallèles → `checkout 5s + overflow TLS`.
- [ ] Mutualiser un `AsyncClient` partagé au lieu de recréer :
      `_execute_web_fetch opencode.py:8095`, probes IP/geo
      `opencode.py:5445,5468,5518`, `dashboard/api.py:1305`.
- [ ] Web search : garder `_DDG_SEM=3 opencode.py:83` + `to_thread(DDGS)`,
      timeout strict `config.yaml:361-367`, pas de sem+ sans mesure.
- [ ] `tiktoken` : garder `len//3` par chunk `opencode.py:8658`, `encode`
      uniquement pour `count_tokens opencode.py:11183` en `to_thread` +
      garde `>500 Ko`.
- [ ] Rendre `orjson==3.11.7` obligatoire (fallback stdlib 5-10x plus lent).
- [ ] Dashboard : garder lectures RO sans lock `dashboard/api.py:365-462`,
      TTL 2 s, ne jamais re-coupler au writer.

## 3. Lot 2 — Fiabilité runtime (sans changer le routage)

- [ ] Breaker paid `opencode.py` : `half_open` retourne toujours `True` →
      thundering herd. Aligner sur breaker VPN : sonde half-open unique.
- [ ] Failover clés : `_key_failover_index` jamais incrémenté → sticky.
      Incrémenter + borner boucle par `len(API_KEYS)` ; `fetch_quotas` sur
      chemin 429 en cache court, pas +1 RTT bloquant.
- [ ] `config-watch` : `clear()+update()` sans `_reload_lock`
      `config/settings.py:1381` → torn read. Passer en swap atomique.
- [ ] Rotation 6 stations / 2 workers : garder 2 (protège le compte NordVPN
      unique : max 2 in-flight / 30 s entre AUTH). Ajouter file prioritaire +
      respawn worker mort dans `_ensure_workers` ; garder single-flight +
      shield + cancel coopératif.
- [ ] Timeouts : garder `read 600s`, ajouter watchdog silence paid 90 s sans
      byte → failover. `sse_keepalive 15s` inchangé.
- [ ] SQLite `app/db/__init__.py:290-332` + `opencode.py:689-723` : 6 stations
      ≈ 6x débit writer. Garder `Queue(10000)+batch 32/50ms`, `VACUUM`/purge
      90j uniquement fenêtre `03:00-05:00 config.yaml:269`, alerter sur
      `QueueFull → fallback synchrone`.
- [ ] `StationSupervisor.restart station_supervisor.py:85-100` :
      check-then-set sans lock → double `restart()` possible. Ajouter lock
      par station.

## 4. Lot 3 — Validation sans régression

- [ ] Chaque lot derrière flag `config.yaml` + rollback `enabled:false`.
- [ ] Goldens `test_conversion_golden.py` : tout écart = échec,
      régénération explicite + diff relu.
- [ ] Gate complète avant merge : CI `ruff + mypy + pytest
      --cov-fail-under=35 + pip-audit + gitleaks + docker compose config`.
- [ ] Ajouter : test charge SSE (stall >15 s), fuite pool curl 6 stations,
      chaos stream+rotation mocké, hit-rate cache conversion.
- [ ] Monter `--cov-fail-under` 35→60 quand hot-path verrouillé.
