# Tasks — Zéro 429 client — réserve d'IPs prouvées

**Feature** : `022-zero-429-client-reserve`
**Input** : `spec.md`, `plan.md`
**Date** : 2026-11-15

Légende : `[P]` = parallélisable ; `P0` = bloquant ; `P1` = cible ; `P2` = qualité.
Statut : `fait`, `en cours`, `à faire`.

---

## Phase 0 — Mesure et séparation (P0)

### T001 — Table `client_reserve` (P0) — fait
- **Fichiers** : `free/quota_ledger.py`, migration `registry.db`.
- **Action** : créer `client_reserve(ip, hostname, station, verified_at, expires_at, source)` avec index unique sur `ip`.
- **Acceptation** : migration idempotente, table lisible par le pool.
- **Test** : `tests/test_client_reserve.py::test_migration`.

### T002 — Marqueur `reserved_for_client` (P0) — fait
- **Fichiers** : `free/quota_ledger.py`, `warm_pool`.
- **Action** : une IP entre dans `client_reserve` après un 200 confirmé ; elle est retirée du pool probe.
- **Acceptation** : une IP réservée n'est jamais proposée aux probes.
- **Test** : unitaire.

### T003 — Probes interdites sur la réserve (P0) — fait
- **Fichiers** : `free/ip_registry/adapters/probe.py`, `scripts/fresh_chain.py`.
- **Action** : refuser `client_reserve` dans `StationProbe` et dans la sélection de candidats.
- **Acceptation** : 100 probes → 0 sélection d'IP réservée.
- **Test** : unitaire + compteur.

### T004 — Pool client réserve-only (P0) — fait
- **Fichiers** : `free/pool/__init__.py`.
- **Action** : `_station_usable` exige une entrée `client_reserve` valide.
- **Acceptation** : aucune requête client sur une IP non réservée.
- **Test** : `test_client_reserve.py::test_pool_reserve_only`.

---

## Phase 1 — Remplissage et budget (P1)

### T005 — Remplissage de la réserve (P1) — en cours (ajout auto + alerte)
- **Fichiers** : `scripts/fresh_chain_supervisor.py`.
- **Action** : en arrière-plan, qualifier des IPs non réservées jusqu'à `reserve_target` (proposé 6, configurable).
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

### T010 — Métriques (P1) — fait
- **Fichiers** : `opencode.py::/metrics`.
- **Action** : `client_reserve_ips`, `client_429_total{reason}`, `probe_budget_remaining`.
- **Acceptation** : métriques visibles.
- **Test** : scrape.

### T011 — Alertes (P1) — fait
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

### T014 — Runbook (P2) — fait
- **Fichiers** : `ops/runbooks/zero-429-reserve.md`.
- **Action** : décrire la réserve, les alertes, le remplacement, le fallback.
- **Acceptation** : un opérateur sait quoi faire quand `reserve < 2`.

## Phase 5 — Alignement explicite (P1)

### T015 — Migration douce (P1) — à faire
- **Fichiers** : `scripts/fresh_chain_supervisor.py`.
- **Action** : le remplissage de réserve ne recrée jamais une station `client_usable` ; il attend une station libre.
- **Acceptation** : aucune station saine recréée pendant un remplissage.
- **Test** : scénario 6/6 + remplissage.

### T016 — Exclusion des IPs grillées (P1) — à faire
- **Fichiers** : `free/quota_ledger.py`, `free/pool/__init__.py`.
- **Action** : une IP `probe-format-poisoned` (24 h) ne peut pas entrer dans la réserve.
- **Acceptation** : 0 IP poison dans `client_reserve`.
- **Test** : unitaire.

### T017 — Critère 6/6 simultané (P1) — à faire
- **Fichiers** : `observability/fleet_slo.py` (ou équivalent), `scripts/fresh_chain_supervisor.py`.
- **Action** : mesurer `client_usable` sur le même cycle ; exiger 6/6 et `warm_pool >= 6`.
- **Acceptation** : le SLO n'est validé que sur un échantillon simultané.
- **Test** : échantillon live.

### T018 — Options, backup, cross-platform (P2) — à faire
- **Fichiers** : `config.yaml`, `dashboard/`, `free/quota_ledger.py`, `tests/`.
- **Action** : `client_reserve.enabled`, `reserve_target`, `probe_budget_pct`, `reserve_min`, `direct_allowed` avec consommateur + test ; table incluse dans le backup ; code sans API Windows hors adapter ; direct utilisé uniquement si `direct_allowed`/`proxy_mode` l'autorise.
- **Acceptation** : `config_audit` sans option morte, backup contient `client_reserve`.
- **Test** : `config_audit --strict`.

## Phase 6 — Angles morts production (P1)

### T019 — Qualification shadow sans station de réserve (P1) — à faire
- **Fichiers** : `scripts/fresh_chain.py`, `scripts/fresh_chain_supervisor.py`.
- **Action** : les probes de remplissage utilisent le mode shadow ; aucune station de réserve n'est immobilisée.
- **Acceptation** : `reserve_target = 6` et les probes tournent quand même.
- **Test** : live.

### T020 — Budget client par IP (P1) — à faire
- **Fichiers** : `free/quota_ledger.py`, `config.yaml`.
- **Action** : `client_budget_pct` (80 %) ; une IP sort de la réserve sous le seuil.
- **Acceptation** : aucune IP servie jusqu'à l'épuisement.
- **Test** : unitaire.

### T021 — TTL et re-vérification (P1) — à faire
- **Fichiers** : `free/quota_ledger.py`, `free/pool/__init__.py`.
- **Action** : `reserve_ttl_s` (300 s) ; une IP plus vieille est re-probée ou sortie.
- **Acceptation** : 0 IP périmée servie.
- **Test** : unitaire.

### T022 — Claim atomique (P1) — à faire
- **Fichiers** : `free/quota_ledger.py`.
- **Action** : `BEGIN IMMEDIATE` + `UPDATE ... RETURNING` ; une IP = un client à la fois.
- **Acceptation** : aucun doublon de claim sous concurrence.
- **Test** : test concurrent.

### T023 — Chaos et CI (P2) — à faire
- **Fichiers** : `tests/test_zero_429.py`, CI.
- **Action** : tests IP qui tombe, WARP mort, DB lock ; nightly `slow`.
- **Acceptation** : les tests passent et la non-régression 6/6 est vérifiée.
