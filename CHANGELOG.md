# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]
### Added
- ADR docs `docs/adr/ADR-*.md` (migration 306 tags historiques)
- pyproject.toml (ruff/mypy/pytest), SERVER_COUNTRIES source unique, deps pinnées

### Fixed
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
