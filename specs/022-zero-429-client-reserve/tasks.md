# Tasks — Zéro 429 client — réserve d'IPs prouvées

**Feature** : `022-zero-429-client-reserve`
**Input** : `spec.md`, `plan.md`
**Date** : 2026-11-15

Légende : `[P]` = parallélisable ; `P0` = bloquant ; `P1` = cible ; `P2` = qualité.
Statut : `fait`, `en cours`, `à faire`.

---

## Phase 0 — Mesure et séparation (P0)

### T001 — Table `client_reserve` (P0) — à faire
- **Fichiers** : `free/quota_ledger.py`, migration `registry.db`.
- **Action** : créer `client_reserve(ip, hostname, station, verified_at, expires_at, source)` avec index unique sur `ip`.
- **Acceptation** : migration idempotente, table lisible par le pool.
- **Test** : `tests/test_client_reserve.py::test_migration`.

### T002 — Marqueur `reserved_for_client` (P0) — à faire
- **Fichiers** : `free/quota_ledger.py`, `warm_pool`.
- **Action** : une IP entre dans `client_reserve` après un 200 confirmé ; elle est retirée du pool probe.
- **Acceptation** : une IP réservée n'est jamais proposée aux probes.
- **Test** : unitaire.

### T003 — Probes interdites sur la réserve (P0) — à faire
- **Fichiers** : `free/ip_registry/adapters/probe.py`, `scripts/fresh_chain.py`.
- **Action** : refuser `client_reserve` dans `StationProbe` et dans la sélection de candidats.
- **Acceptation** : 100 probes → 0 sélection d'IP réservée.
- **Test** : unitaire + compteur.

### T004 — Pool client réserve-only (P0) — à faire
- **Fichiers** : `free/pool/__init__.py`.
- **Action** : `_station_usable` exige une entrée `client_reserve` valide.
- **Acceptation** : aucune requête client sur une IP non réservée.
- **Test** : `test_client_reserve.py::test_pool_reserve_only`.

---

## Phase 1 — Remplissage et budget (P1)

### T005 — Remplissage de la réserve (P1) — à faire
- **Fichiers** : `scripts/fresh_chain_supervisor.py`.
- **Action** : en arrière-plan, qualifier des IPs non réservées jusqu'à `reserve_target` (défaut 3).
- **Acceptation** : `client_reserve_ips >= 2` stable.
- **Test** : live + métrique.

### T006 — Budget probe (P1) — à faire
- **Fichiers** : `free/quota_ledger.py`, `config.yaml`.
- **Action** : `probe_budget_pct` (défaut 50 %) ; les probes ne consomment pas plus.
- **Acceptation** : `client_used` reste 0 sur les IPs réservées.
- **Test** : unitaire + live.

### T007 — Remplacement d'une IP consommée (P1) — à faire
- **Fichiers** : `free/pool/__init__.py`, superviseur.
- **Action** : une IP de réserve dont le bucket tombe sous le seuil sort et est remplacée.
- **Acceptation** : la réserve ne sert jamais une IP épuisée.
- **Test** : unitaire.

---

## Phase 2 — Fallback avant 429 (P1)

### T008 — Ordre de fallback (P1) — à faire
- **Fichiers** : `free/pool/__init__.py`, `opencode.py`.
- **Action** : réserve → WARP → proxys → direct (si autorisé) → 429.
- **Acceptation** : un WARP disponible est utilisé avant le 429.
- **Test** : `test_zero_429.py::test_fallback_before_429`.

### T009 — 429 explicite (P1) — à faire
- **Fichiers** : `opencode.py`.
- **Action** : 429 JSON avec `Retry-After` exact et `reason` (`reserve_empty`, `all_fallbacks_dead`, `upstream_429`).
- **Acceptation** : aucun 429 vide.
- **Test** : unitaire.

---

## Phase 3 — Observabilité (P1)

### T010 — Métriques (P1) — à faire
- **Fichiers** : `opencode.py::/metrics`.
- **Action** : `client_reserve_ips`, `client_429_total{reason}`, `probe_budget_remaining`.
- **Acceptation** : métriques visibles.
- **Test** : scrape.

### T011 — Alertes (P1) — à faire
- **Fichiers** : `scripts/fresh_chain_supervisor.py`, `observability/incidents.py`.
- **Action** : alerte `reserve < 2`, critique `reserve = 0`.
- **Acceptation** : incident ouvert.
- **Test** : simulation.

---

## Phase 4 — Tests de charge (P2)

### T012 — 1000 requêtes client (P2) — à faire
- **Fichiers** : `tests/test_zero_429.py`.
- **Action** : réserve non vide, 1000 requêtes, compter les 429.
- **Acceptation** : 0 429.
- **Test** : pytest marker `slow`.

### T013 — 100 probes + 100 clients (P2) — à faire
- **Fichiers** : `tests/test_zero_429.py`.
- **Action** : vérifier que les probes ne touchent pas la réserve.
- **Acceptation** : `client_used` = 0 sur la réserve.

### T014 — Runbook (P2) — à faire
- **Fichiers** : `ops/runbooks/zero-429-reserve.md`.
- **Action** : décrire la réserve, les alertes, le remplacement, le fallback.
- **Acceptation** : un opérateur sait quoi faire quand `reserve < 2`.
