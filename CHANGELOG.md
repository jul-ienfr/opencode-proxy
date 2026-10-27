# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]
### Added
- [2026-10-27] Catalogue vivant `live_models` : ajout automatique des modèles annoncés par `/models` (deux jambes), retrait des modèles dépréciés/absents, migration vers la jambe active
- [2026-10-27] `GET /v1/models` compatible Anthropic Models API (`capabilities.effort`, `thinking`, limites) ; `max.supported` reflète réellement le catalogue
- [2026-10-27] Notices clients pour les modèles retirés : `410` explicite, en-têtes `X-OpenCode-Model-Deprecated` / `X-OpenCode-Notice`, mémorisées 7 jours
- [2026-10-27] Pop-up dashboard + événement SSE `models_updated` lors d'un ajout/retrait/dépréciation, avec rattrapage par `revision`
- [2026-10-26] Roadmap stations/IP : `ops/health.py`, `ops/fleet.py`, `ops/jobs.py`, `ops/updater.py`, `free/quality.py`, `free/probe.py`, `free/selector.py`
- [2026-10-26] Sélection par score mesuré (latence/succès/quota/charge/stabilité) + sonde active sans quota
- [2026-10-26] API flotte : start/stop/restart/connect/disconnect par station, jobs 202, snapshot read-only
- [2026-10-26] Persistance quota périodique + flush shutdown ; anti-flap ; stack `.env` par station
- Audit routabilité `docs/audit-routabilite-stations-2026-09-08.md` + ADR-007 (PC-1→PC-16 implémentés)
- `scripts/config_coverage.py` + gate `--baseline` (O1 : zéro clé morte)
- Métriques `/metrics` : `pool_station_usable`, `pool_usable_stations`, `pool_usable_floor`, `free_429_by_model`
- Config : `boot_stagger_s`, `server_list_refresh_interval_s`, `free_model_spread`/`free_model_candidates`, `bad_ttl_by_cause`, `egress_allow_any_country`, `enforce_vpn_only`
- Flotte mixte O3 (`auto_mixed_stacks`) : slots WG / OV-TCP / OV-UDP, retours WG réservés aux slots WG
- Runbook : patterns 1/N (AUTH_FAILED, TLS obsolète, restart loop)
- ADR docs `docs/adr/ADR-*.md` (migration 306 tags historiques)
- pyproject.toml (ruff/mypy/pytest), SERVER_COUNTRIES source unique, deps pinnées

### Fixed
- [2026-10-26] Usage Anthropic : `input_tokens` exclut désormais cache read/write (OpenAI les inclut dans `prompt_tokens`) ; `prompt_cache_miss_tokens` n'est plus compté comme cache_creation → contexte ~2× surestimé par Claude Code, autocompact prématuré / thrashing
- [2026-10-26] `tool_calls` amont sans nom/id (dsh `requires toolCallId matching its tool source`) : nom inféré des arguments, id synthétisé, appel invalide écarté ou flux rejoué
- [2026-10-26] Flapping station : resets voie secondaire ignorés côté statut, churn borné, anti-flap, frontière de guérison
- [2026-10-26] `boot_error` stale recalculé ; `disconnect` persistant ; `toggle/connect/heal` fleet-wide
- [2026-10-26] Mise à jour d'image coordonnée flotte ; `control_api_key` migré hors de `config.yaml`
- Watchdog : budget `max_restarts_per_hour` appliqué toutes voies + décision unique par tick (F1/F2)
- Pool free : bad-mark par cause + garde N-2 (jamais 1/6), rotation sans coupure (H5/H6)
- Canary WG : bring-up échoué = indéterminé, plus de freeze 10 min (F3/H7)
- Pins pays hors `server_countries` refusés + loggés (F4) ; healthcheck compose = egress réelle (H3)
- `[free-usage]` : `client_ip` vs `egress_ip` désambiguïsés (F9) ; 4 clés mortes câblées (F8 partiel)
- `free_quota.py` dead module removed (F-H1)
- `threading.Lock + cycle` → index modulo atomique (F-H3)
- `control_api_key` auto-gen + `DASHBOARD_REQUIRE_TOKEN` fail-closed (F-H6)
- CI strict gate (F-H8)

### Changed
- `docker-compose.yml` SERVER_COUNTRIES via ${SERVER_COUNTRIES}
- `gunicorn` prod vs `uvicorn` dev alinhamento (F-L3)
- `protocol_mapping.py` dedup (F-M7) — pending
- `free_discovery` centralisation (F-L4) — pending

## [2026-08-23] — Audit initial
- Audit `docs/audit-2026-08-23.md` score 6.2/10
