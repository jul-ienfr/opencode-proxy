# Inventaire de démantèlement — monolithes NordVPN

**Feature** : `014-nordvpn-exhaustive-rotation`
**Baseline** : `module-baseline.json` (642 fichiers, ratchet 0 violation)
**Règle** : strangler par slices, parité, shadow, canary, rollback. Aucun big-bang.

---

## 1. Baseline

| Monolithe | Lignes | Rôle actuel | Cible |
|---|---:|---|---|
| `opencode.py` | 31 347 | composition root, requêtes, free leg, routage | entrypoint fin + `free/request_pipeline/` |
| `vpn/manager.py` | 11 385 | cycle de vie station, AUTH, transport, santé, rotation | adapteur fin + `free/station/` |
| `free/pool.py` | 8 190 | sélection, quota, rotation, pool public, accounting | `free/pool/` |
| `dashboard/api.py` | 6 845 | routes, SQL, read models | `dashboard/routers/` + read models |
| `scripts/fresh_chain.py` | 2 535 | scan de réparation | `free/qualification/sweep.py` + CLI fine |
| `scripts/fresh_chain_supervisor.py` | 1 926 | watchdog/réparation | watchdog/repair + superviseur fin |

---

## 2. Seams identifiés

### `vpn/manager.py`
- cycle de vie station (`start`, `stop`, `restart`, `recreate`) ;
- AUTH/session (Gluetun, credentials, AUTH_FAILED) ;
- transport/bind (WireGuard, OpenVPN UDP/TCP, DNS) ;
- santé/probe (egress, SOCKS, modèle) ;
- rotation (pin, restore, soft rotate).

### `free/pool.py`
- sélection de station/IP ;
- quota/leases ;
- rotation proactive ;
- pool public ;
- accounting des requêtes.

### `scripts/fresh_chain.py`
- construction des candidats ;
- claims ;
- bind/probe ;
- audit des tentatives ;
- restauration.

### `dashboard/api.py`
- routes HTTP ;
- requêtes SQL ;
- read models ;
- vues SSE/JSON.

### `opencode.py`
- pipeline de requête ;
- jambe free ;
- routage modèle ;
- composition root.

---

## 3. Slices

| Slice | Source | Cible | Dépend de | Parité | Rollback |
|---|---|---|---|---|---|
| S1 | `vpn/manager.py` cycle de vie | `free/station/lifecycle.py` | — | caractérisation + shadow | flag |
| S2 | `vpn/manager.py` AUTH/session | `free/station/auth_session.py` | S1 | tests AUTH + canary | flag |
| S3 | `vpn/manager.py` transport/bind | `free/station/transport_bind.py` | S1-S2 | tests transports | flag |
| S4 | `vpn/manager.py` santé/probe | `free/station/health_probe.py` | S1-S3 | probes réelles | flag |
| S5 | `vpn/manager.py` rotation | `free/station/rotation.py` | S1-S4 | cutover | flag |
| S6 | `free/pool/__init__.py` sélection/quota | `free/pool/selection.py`, `quota.py` | S1 | tests sélection | flag |
| S7 | `free/pool/__init__.py` rotation/public/accounting | `free/pool/rotation.py`, `public_pool.py` | S6 | tests rotation | flag |
| S8 | `scripts/fresh_chain.py` | `free/qualification/sweep.py` + CLI | S1-S7 | même décisions | flag |
| S9 | `dashboard/api.py` read models | `dashboard/routers/read_models.py` | — | mêmes réponses | flag |
| S10 | `dashboard/api.py` routers | `dashboard/routers/` | S9 | mêmes réponses | flag |
| S11 | `opencode.py` pipeline | `free/request_pipeline/request.py` | S1-S10 | mêmes requêtes | flag |
| S12 | `opencode.py` free leg/routing | `free/request_pipeline/` | S11 | canary | flag |
| S13 | `opencode.py` composition root | entrypoint | S11-S12 | smoke | flag |

---

## 4. Critères par slice

- tests de caractérisation écrits **avant** l’extraction ;
- shadow : ancien et nouveau chemin comparés ;
- canary : une station, un pays, puis six stations ;
- rollback : flag, ancien chemin conservé jusqu’au canary ;
- parité fonctionnelle : mêmes entrées → mêmes sorties ;
- p95 ≤ +5 % vs baseline ;
- fichier legacy décroît ;
- module cible < 1 500 lignes ;
- 0 cycle, domaine pur ;
- ADR/runbook/implementation-log mis à jour.

---

## 5. Suivi

| Slice | Statut | Preuve |
|---|---|---|
| S6 | partial | package `free/pool/` ; `selection.py` + `quota.py` extraits, gros sélecteurs restants |
| S7 | partial | `rotation.py` + `public_pool.py` extraits, accounting restant |
| S1-S5, S8-S13 | planned | ce document |
| `free/qualification` | active | 522 tests, coverage 93 %, 0 cycle |
| `scripts/fresh_chain.py --sweep` | active | bridge shadow/live |
| `catalog_claims` | migrated | lit `qualification_candidates`, fallback legacy |
| `country_backoff` | removed | test + gate architecture |
| `rotation_planner` | concurrent | max_concurrent=6 |

Le statut détaillé des tâches est dans `tasks.md` (T150-T174).
