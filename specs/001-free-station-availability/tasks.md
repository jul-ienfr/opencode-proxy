# Tasks — Disponibilité permanente des stations free

**Spec** : [spec.md](./spec.md) | **Plan** : [plan.md](./plan.md)
**Baseline live 2026-11-04 08:39Z** : `client_usable = 1/6`, 5 stations
`quota=bad-ip`, station 1 `status=error`. Mesure automatique active.

Légende : `[x]` fait, `[ ]` à faire, `[P]` parallélisable.

## Phase 1 — Lot 0 : mesurer (aucun risque)

- [x] **T001** — Mode `observe` dans `scripts/fresh_chain_supervisor.py` :
  évalue les besoins, logue `would repair`, ne lance aucune chaîne.
- [x] **T002** — `config.yaml` : `allocator.mode: observe`,
  `ip_registry.slo.{enabled,sample_interval_s}`.
- [x] **T003** — `scripts/station_slo_watch.py` : champs `station_usable`,
  `model_usable`, `client_usable` + journal d'incidents SQLite.
- [x] **T004** — `opencode.py` : auto-démarrage de l'échantillonneur + watchdog
  60 s, lock PID.
- [x] **T005** — `opencode.py` : `SqliteIpRegistry.reconcile()` au boot.
- [ ] **T006** — Test de contrat : en `observe`, aucune entrée `running`,
  aucun `_start_chain`, aucun `_write_allocator_state`.
  `tests/test_supervisor_observe.py`.
- [ ] **T007** — Verrou inter-processus allocateur ↔ manager
  (`vpn/manager.py`, `scripts/fresh_chain_supervisor.py`).
- [ ] **T008** — Alerte active par défaut (webhook si présent, sinon incident
  visible dans `/health`).
- [ ] **T009** — Rapport de baseline 7 jours : disponibilité par station,
  MTTR, fréquence, causes. `scripts/station_slo_report.py`.
- [ ] **T010** — Runbook « lire les incidents » dans `docs/`.

**Sortie** : baseline chiffrée, zéro action de réparation. Rollback :
`mode: off`.

## Phase 2 — Lot 1 : rendement des candidats

- [ ] **T011** — Supprimer `PROVEN_HOSTS` (`scripts/fresh_chain.py:38-52`) ;
  liste de secours uniquement après épuisement du catalogue, avec TTL.
- [ ] **T012** — Purger les pins `.env` et les `logs/compose-hostname*.yml`
  périmés au démarrage.
- [ ] **T013** [P] — Module de pré-qualification : hostname dans l'union des
  manifestes, transport connu, auth non cassée — avant `docker compose up`.
- [ ] **T014** [P] — Diversité : `max_per_24: 2`, `max_per_country: 2`,
  re-test après cooldown pays.
- [ ] **T015** — `probe_models` : 3 entrées.
- [ ] **T016** — Métrique `candidate_yield` par pays et par source.
- [ ] **T017** — Couverture pays exposée (`verified`, `queued`, `no-adapter`).

**Sortie** : `no-egress` < 50 % des binds, ≥ 2 IP vérifiées dans ≥ 20 pays.

## Phase 3 — Lot 2 : vérité quota

- [ ] **T018** — `reset_at` porté par l'IP, lu par le sélecteur ; `IpQuotaLease`
  devient une vue calculée.
- [ ] **T019** — Supprimer `min(retry_after, 120)` (`opencode.py`).
- [ ] **T020** — Clé de ledger sans jour UTC (`free/quota_ledger.py`).
- [ ] **T021** — `EXHAUSTED` exige `reset_at`, sinon `ERROR`.
- [ ] **T022** — `model` transmis à tous les re-picks (`opencode.py:14897+`).
- [ ] **T023** — Exclusion cumulative en stream (`free/pool.py:4051`).
- [ ] **T024** — Un seul score lu par le sélecteur ; suppression des trois
  classements concurrents.
- [ ] **T025** — Tests de contrat : 10 000 décisions sans sélection avec
  `reset_at` futur ; 0 re-pick sans `model`.
- [ ] **T026** — Mode ombre 48 h : comparaison ancien/nouveau classement.

**Sortie** : aucune violation de reset mesurée, écart de classement expliqué.

## Phase 4 — Lot 3 : MTTR

- [ ] **T027** — Supprimer le `settle_s` aveugle ; poll dès la fin du recreate.
- [ ] **T028** — Egress : 1 URL, 1 schéma, 2 tentatives, timeout 2 s ; une
  seule lecture d'egress.
- [ ] **T029** — Supprimer `--delay 8` ; budget de rebind par besoin.
- [ ] **T030** — `max_candidates_per_chain: 8`, `max_parallel_repairs: 2`.
- [ ] **T031** — Timeouts par tentative : TTFB 12–15 s, total 30 s, lecture
  60 s max.
- [ ] **T032** — Benchmark sur 50 réparations : p50 < 60 s, p95 < 120 s.

**Sortie** : MTTR p50 < 60 s. Rollback : valeurs de config précédentes.

## Phase 5 — Lot 4 : capacité

- [ ] **T033** — Warm pool vérifié (≥ 6 IP), API et promotion automatique.
- [ ] **T034** — Couverture pays : 2 IP vérifiées par pays, échantillonnage
  priorisé (pas d'énumération des 9 948 serveurs).
- [ ] **T035** — Exploration ≥ 5 % des probes.
- [ ] **T036** — `station_count` jusqu'à 10 ; au-delà du budget de sessions,
  `standby` et non `broken`.
- [ ] **T037** — Décision « 7e session » sur les chiffres du lot 3.

**Sortie** : ≥ 5/6 à 99,99 % sur 30 jours, 6/6 ≥ 99,9 %, aucun /24 > 2.

## Phase 6 — Lot 5 : maintenabilité

- [ ] **T038** — Fusion des stores : `_model_ip_health` → `IpQuotaLease` →
  `BucketState`, au fur et à mesure que le registre les couvre.
- [ ] **T039** — Tests de contrat : absence de littéraux hostname/pays,
  exclusivité IP, diversité, `model` sur failover.
- [ ] **T040** — Rotation des logs superviseur/chaînes ; décisions en JSONL.
- [ ] **T041** — Runbooks : 0/6, pool COOLING, AUTH_FAILED, pays épuisé.

## Dépendances

- T006–T010 avant T011 (on mesure avant de changer).
- T013 dépend de T009 (quels pays/serveurs rendent réellement).
- T018–T024 avant T027 (le MTTR ne sert à rien si les rotations sont inutiles).
- T032 conditionne T037 (décision 7e session sur données).

## Parallélisme

- T013/T014 en parallèle (fichiers distincts).
- T018/T020/T022 en parallèle (modules distincts), puis T024.
- T027/T028/T029 en parallèle, puis T032.
