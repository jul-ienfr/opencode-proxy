# ADR-006 — Refonte architecture : contrats gelés (zéro régression)

Date: 2026-09-07
Status: accepted
Contexte: god files (`opencode.py` 14 906 l, `vpn_manager.py` 6 723 l,
`dashboard/api.py` 4 584 l), `app/` squelette vide (ADR-004 jamais rempli).
Tag socle : `pre-refonte` (= commit `0c7c27b`).
Harnais : `tests/test_phase0_contracts.py` (25 tests, verts le 2026-09-07).

## Décision

Refonte par strangler (1 module = 1 PR, tests verts entre), AUCUN changement
de comportement observable. Contrats gelés jusqu'à la Phase 9 incluse :

1. **Routes HTTP** — socle proxy : `POST /v1/messages`,
   `POST /anthropic/v1/messages`, `POST /v1/messages/count_tokens`,
   `POST /v1/chat/completions`, `POST /v1/responses`, `GET /v1/models`,
   `GET /health`, `GET /metrics`, `/api/circuit-breakers`,
   `/api/key-pauses`, `/api/free-models`, `/api/free-discovery/refresh` ;
   dashboard : familles `/api/config`, `/api/stats`, `/api/logs`,
   `/api/history`, `/api/vpn-status`, `/api/quotas`, `/api/costs`,
   `/api/events`, racine `/`, mount `/static`. Pas de collision
   dashboard ↔ chemins `/v1/*`.
2. **Façades d'import** — `config` (22 symboles dont `MODELS`, `ROUTES`,
   `get_model_config`, `yaml_get/set`), `dashboard` (`register_dashboard`,
   `log`, `build_display`), `opencode` (`app`, `lifespan`,
   `_acquire_instance_lock`, `get_model_config`, `_route_for`).
   Chaque extraction garde un re-export à l'ancien chemin.
3. **Schéma SQLite** — `requests` : 34 colonnes figées ; INSERT = 32
   placeholders ; `free_model_usage` : 11 colonnes. Fichier
   `logs/requests.db`, WAL, batch queue.
4. **Config** — `CONFIG_KEYS` (7 clés exposables), sections `config.yaml`
   (`server`, `upstream`, `routing`, `models`, `ip_rotation`),
   hot-reload `custom_routes.json` (poll 5 s), secrets `.env` uniquement.
5. **Entrypoint** — `python opencode.py [--no-gui|--gui] [--port N]` ;
   GUI tray par défaut, fallback terminal ; lock mono-instance
   `logs/opencode-{PORT}.lock` + message FATAL figé ; API+dashboard sur
   `OPENCODE_PORT` (défaut 4000).
6. **Règles de PR** — déplacement pur d'abord (pas de logique métier dans
   le même commit), `ruff check` + `mypy` + `pytest -k "not docker"` verts,
   boot local + 1 appel streaming/non-streaming avant merge.

## Conséquences

- `tests/test_phase0_contracts.py` est bloquant : le casser = stopper la
  refonte et réparer avant toute extraction.
- Les shims temporaires (`vpn_manager.py`, `protocol_mapping.py`,
  `free_ip_pool.py` en re-export) vivent jusqu'en Phase 9, suppression
  un par un avec preuve de non-usage (`grep`).
- Les side-effects d'import (`config/settings.py` : thread
  upstream-models-fetch, `load_env_file()`/`load_yaml_config()` au
  top-level) migrent vers le `lifespan` explicite dès la Phase 1.
- Sens d'import imposé : `config` n'importe ni `vpn_manager` ni
  `dashboard` (injection via `app.state`).
