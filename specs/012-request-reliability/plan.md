# Implementation Plan: Fiabilité des requêtes free

**Branch**: `012-request-reliability` | **Date**: 2026-11-08 | **Spec**: [spec.md](./spec.md)

**Input**: Audit [docs/AUDIT_REQUETES_2026-11-08.md](../../docs/AUDIT_REQUETES_2026-11-08.md).

## Summary

Réduire les échecs de la jambe free en corrigeant les trois causes mesurées le
2026-11-08 :

1. `auto_egress` n'est pas accepté par les chemins free non-stream/SystemOne
   (`503` immédiat alors que WARP est disponible) ;
2. la flotte NordVPN est en `AUTH_FAILED` et doit être parkée/réparée ;
3. les endpoints WARP IPv4 partagent des `/32` NAT épuisés et les rotations
   suivent le bruit plutôt que le quota journalier.

Le plan est volontairement **free-only** : aucun second compte, aucun payant,
aucun direct résidentiel, aucune attente comme substitut.

## Technical Context

**Language/Version**: Python 3.12+, PowerShell 5.1/7 pour les scripts Windows.

**Primary Dependencies**: FastAPI/uvicorn, SQLite, `curl_cffi`, Docker CLI,
`caomingjun/warp`, Gluetun, pytest.

**Storage**: `logs/requests.db` (tentatives), `logs/registry.db` (rotation_jobs,
quotas, leases), `config.yaml` (intention opérateur).

**Testing**: pytest unitaire/contrat sans Docker ; runner Docker factice ;
smoke live opt-in.

**Target Platform**: Windows 10/11 + Docker Desktop, conteneurs Linux.

**Project Type**: service web + CLI/scripts + GUI Svelte.

**Performance Goals**: p95 succès < 35 s ; slot wait ≤ 0,25 s ; rotations
< 150/jour ; `503 pool indisponible` = 0.

**Constraints**: free-only, `enforce_vpn_only=true`, pas de second compte,
pas d'attente comme solution, un seul processus proxy.

**Scale/Scope**: 6 stations Docker + 6 conteneurs WARP (12 endpoints, dont un
canari IPv4), 3 modèles free principaux.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principe | Gate | Statut |
|---|---|---|
| I — disponibilité mesurée | `client_usable` exige une preuve, pas un statut | **PASS** — les succès restent mesurés par `requests`/`free_model_usage` |
| II — trois cycles de vie | station, session, IP séparés | **PASS** — le parkage NordVPN ne condamne pas les IP ; le quota est keyé par IP |
| III — source de vérité | `registry.db` autorité | **PASS** — les jobs de rotation restent dans `rotation_jobs` |
| IV — reset fournisseur | aucune IP avant `reset_at` | **PASS** — `FreeUsageLimitError` mène à un blocage jusqu'au reset |
| V — unicité IP | 1 egress = 1 station | **PASS** — le canari IPv4 ne partage pas de bucket avec les `/64` |
| VI — indépendance stations | pas de quota pays | **PASS** — inchangé |
| VII — preuve de succès | 200 avec contenu | **PASS** — le correctif ne réintroduit pas de faux 200 |
| VIII — un seul réparateur | superviseur propriétaire | **PASS** — le parkage est déclaratif, pas un second réparateur |
| IX — score vs portes dures | pas de contournement | **PASS** — le quota journalier reste une porte dure |
| X — dégradation explicite | pas de faux 6/6 | **PASS** — un pool vide reste un `503` honnête |
| XI — fallbacks multiples | WARP/NordVPN/proxys/direct | **PASS** — WARP reste le fallback free principal |
| XII — options GUI effectives | chaque option a un consommateur | **PASS** — les clés `disabled_stations`/rotation ont un effet runtime |
| XIII — données traçables | migrations, backups | **PASS** — pas de migration destructive |
| XIV — modularité | modules < 1 500 lignes | **PASS** — changements ciblés |

**Aucune violation à justifier.**

## Project Structure

### Documentation (this feature)

```text
specs/012-request-reliability/
├── spec.md
├── plan.md
├── research.md
├── quickstart.md
├── contracts/
│   └── request-reliability.md
└── tasks.md
```

### Source Code (repository root)

```text
opencode.py                     # modes free non-stream/SystemOne
streaming/retry_until_success.py # honnêteté des statuts finaux (déjà corrigé)
free/pool.py                    # sélection, quotas, rotations
free/warp_rotation.py           # budget et déclencheurs
free/warp_provisioner.py        # specs physiques
ops/providers/warp.py           # desired state
config.yaml                     # canari IPv4, disabled_stations, budgets
scripts/config_audit.py         # audit de config
tests/                          # unités/contrats sans Docker
```

**Structure Decision**: projet unique, service Python existant. Aucun nouveau
module n'est nécessaire pour la Phase 0 ; les ajouts Phase 1 restent dans
`free/pool.py`, `free/warp_rotation.py` et `config.yaml`.

## Phase 0 — Correctifs immédiats (P0)

### P0.1 — `auto_egress` dans les chemins free non-stream et SystemOne

- Introduire `_FREE_POOL_MODES = ("vpn", "socks5", "auto_egress")`.
- Utiliser ce tuple dans :
  - `_try_free_model_first` (`opencode.py:15497`, `15837`, `15854`, `15868`) ;
  - `_systemone_free_via_pool` (`opencode.py:27758`, `28136`).
- Le `503` fail-closed ne doit rester que si le pool est réellement vide.
- Tests : SystemOne avec `proxy_mode=auto_egress` et pool factice ; test
  générique non-stream ; test « pool vide → 503 ».
- Effet attendu : supprime les 2 647 faux `503` et remonte le taux de tentative
  hors smoke de 70,7 % à ~80 %.

### P0.2 — Parkage NordVPN

- Valider `NORDVPN_TOKEN` avec `scripts/refresh_nord_credentials.py --check`
  (fait : token 200, credentials dérivables).
- `--apply` pour régénérer les credentials de service, backup automatique.
- Parquer les 6 stations (`disabled_stations: [1..6]`) tant que l'auth n'est
  pas prouvée sur une seule station.
- Réactiver 1 → 6 stations après un egress public par station.
- Effet attendu : `AUTH_FAILED` 6 052 → < 50/jour, moins de bruit de rotation.
- **Correction induite** : `FreeIPPool.enabled`, `on_request` et
  `get_status().enabled` étaient liés à `_vpn.enabled` (Docker). Parquer
  NordVPN désactivait aussi WARP. Ils acceptent désormais « Docker **ou**
  endpoints SOCKS5 actifs ».

### P0.3 — Canari IPv4 WARP

- Garder `WARP 1` (IPv4) `enabled: true`.
- Passer `WARP 2..6` IPv4 `enabled: false`.
- Les six IPv6 restent actifs.
- Aucun `WarpRotator` ne tourne un endpoint IPv4 (déjà garanti par
  `free/pool.py` et `free/warp_rotation.py`).
- Test de config : 12 lignes déclarées, 7 actives (6 IPv6 + 1 canari IPv4).
- Effet attendu : ~830 essais morts/jour supprimés.

## Phase 1 — Réduction structurelle (P1)

1. **Quota journalier** : parser `FreeUsageLimitError` + `Retry-After` ;
   bloquer `(modèle, IP)` jusqu'au reset ; ne pas appliquer le gel anti-churn
   de 180 s à ce cas ; autoriser une rotation contrôlée toutes les 5–10 min.
2. **Budget de rotation** : `station_max_rotations_per_hour: 3`,
   intervalle 600–900 s, brancher la config sur `WarpRotator`.
3. **Slot wait** : `model_slot_wait_s: 0.25` ; mesurer la distribution.
4. **Repli modèle** : deepseek → mimo → glm, opt-in, sans changer le modèle
   silencieusement.

## Phase 2 — Observabilité (P2)

- `requests` : `client_request_id`, `attempt_no`, `final_status`.
- `rotation_jobs` : `finished_at`, `result`, `reason`.
- `/api/free-health` : `served`/`unproven` doivent refléter les succès réels.
- Alertes : 429/tentative, essais sur IP morte, rotations/h, p95 slot wait,
  `AUTH_FAILED`.

## Testing Strategy

- **Unités sans Docker** : `_FREE_POOL_MODES`, `_systemone_free_via_pool`
  auto_egress, config du canari, budget de rotation, slot wait.
- **Contrats** : `tests/test_plan_011_baseline.py` (7 endpoints actifs),
  `tests/test_systemone.py`, `tests/test_retry_until_success.py`.
- **Live opt-in** : un appel `/v1/systemone` → 200/429, jamais 503 ;
  `/api/vpn-status` sans `AUTH_FAILED` après réparation.
- **Gate** : `pytest -k "not docker"` + `scripts/config_audit.py --strict`.

## Rollout & Rollback

1. P0.1 code + tests, restart à chaud.
2. P0.3 config (hot-reload), vérifier `GET /api/vpn/socks5`.
3. P0.2 credentials + parkage, vérifier `/api/vpn-status`.
4. Surveiller 24 h : `503`, `429`, `AUTH_FAILED`, rotations, p95.
5. Rollback : réactiver les IPv4 un par un, restaurer `disabled_stations: []`,
   revert du tuple `_FREE_POOL_MODES` si le pool est instable.

## Risks

- **WARP-only** après parkage NordVPN : capacité réduite ; ne pas parquer si
  WARP n'a pas au moins 4 `/64` sains.
- **Rotation contrôlée** : si le budget est trop bas, les buckets journaliers
  restent épuisés ; surveiller le taux de 429.
- **Canari IPv4** : peut rester à 429 ; c'est un canari, pas une source de
  capacité.
- **Credentials NordVPN** : `--apply` modifie `credentials.env` et
  `vpn_configs/credentials.txt` ; backups automatiques.
