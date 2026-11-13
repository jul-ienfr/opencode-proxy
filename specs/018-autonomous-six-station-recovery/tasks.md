# Tasks — Récupération autonome des six stations

**Feature** : `018-autonomous-six-station-recovery`
**Input** : `spec.md`, `plan.md`, ADR-017-01
**Date** : 2026-11-14

Légende : `[P]` = parallélisable ; `P0` = bloquant la release ;
`P1` = requis pour la cible ; `P2` = qualité.
Statut : `fait`, `en cours`, `à faire`.

---

## Phase 1 — Primitive rotate

### T201 — Un seul `/v1/vpn/rotate` par chaîne (P0) — à faire
- **Fichiers** : `scripts/fresh_chain.py`,
  `free/ip_registry/adapters/gluetun_control_v2.py`,
  `tests/test_rotation_contract.py`.
- **Action** : remplacer la boucle `binder.assign(host)` par
  `rotate_outcome(candidates, constraints)` avec la liste ordonnée du registre,
  `transport_policy=None`, `max_candidates`, `max_attempts`.
- **Acceptation** : un seul job `rotate` par chaîne ; aucun switch par host.
- **Test** : binder factice qui vérifie un appel et l'ordre des candidats.

### T202 — Traiter tous les états terminaux sans disable (P0) — à faire
- **Fichiers** : `scripts/fresh_chain.py`,
  `free/ip_registry/adapters/gluetun_control_v2.py`.
- **Action** : `committed` ⇒ winner ; `exhausted`/`rolled_back` ⇒ nouvelle liste
  ou candidat suivant, jamais de station désactivée.
- **Acceptation** : `exhausted` ne bloque pas la station suivante.
- **Test** : contrat rotate + test de chaîne.

### T203 — Le fork épuise candidats et transports (P0) — en cours
- **Fichiers** : `gluetun/internal/control/jobs/rotation.go`,
  `executor.go`, `preflight.go`.
- **Action** : vérifier que le job rotate essaie WG → UDP → TCP par candidat,
  s'arrête au premier egress prouvé et rollback le candidat échoué.
- **Acceptation** : un job expose les tentatives par host et par transport.
- **Test** : `internal/control/jobs` + live sur une station cassée.

---

## Phase 2 — Qualité des candidats

### T204 — Warm pool et preuves fraîches (P1) — fait
- **Fichiers** : `free/chain_registry.py`, `scripts/fresh_chain.py`.
- **Action** : preuves `last_ok_at` < 6 h, warm pool non expiré, public récent
  avant embedded.
- **Acceptation** : aucun hostname dont la dernière preuve date de > 6 h n'est
  classé premier.
- **Test** : `test_registry_hosts_filters_by_transport` + test de tri.

### T205 — Qualification live avant classement (P1) — en cours
- **Fichiers** : `scripts/fresh_chain.py`, `free/pool`,
  `free/qualification`.
- **Action** : mesurer le tunnel et le DNS, pas seulement un score historique ;
  un host qui échoue 2 fois de suite est écarté pour la chaîne.
- **Acceptation** : les candidats morts ne consomment pas plus de 2 tentatives.
- **Test** : test de chaîne avec hosts morts puis vivant.

### T206 — Exclusions et budgets (P1) — fait
- **Fichiers** : `scripts/fresh_chain.py`, `config.yaml`.
- **Action** : `max_candidates=20`, `max_attempts=10`, timeouts 20/10 s,
  `max_cycles=2`, IP partagée exclue.
- **Acceptation** : un candidat mort est abandonné en ≤ 50 s.
- **Test** : test de budget et de timeout.

### T207 — Jamais de re-score par le fork (P0) — fait
- **Fichiers** : `free/ip_registry/adapters/gluetun_control_v2.py`.
- **Action** : transmettre la liste dans l'ordre, sans score.
- **Acceptation** : l'ordre reçu par le fork est identique à l'ordre envoyé.
- **Test** : contrat rotate.

---

## Phase 3 — Authentification et conteneur

### T208 — Politique AUTH_FAILED (P0) — en cours
- **Fichiers** : `scripts/fresh_chain_supervisor.py`,
  `scripts/refresh_nord_credentials.py`, `config.yaml`.
- **Action** : refresh des identifiants de service sur `AUTH_FAILED`, cooldown
  global borné, repli WG, pas de boucle d'auth.
- **Acceptation** : `AUTH_FAILED`/min tend vers 0 après refresh ; aucune rafale
  de connexions.
- **Test** : log de réparation + compteur.

### T209 — Identifiants/image périmés ⇒ recreate (P0) — fait
- **Fichiers** : `free/ip_registry/adapters/compose_binder.py`,
  `hybrid_binder.py`.
- **Action** : comparer `OPENVPN_USER`, `OPENVPN_PASSWORD`,
  `WIREGUARD_PRIVATE_KEY` et l'image ; forcer Compose si différent.
- **Acceptation** : un conteneur avec d'anciens secrets est recréé.
- **Test** : `test_credentials_stale_detects_a_rotation`,
  `test_stale_credentials_force_a_compose_recreate`.

### T210 — Récupération conteneur (P0) — en cours
- **Fichiers** : `gluetun_control_v2.py`, `hybrid_binder.py`,
  `fresh_chain.py`.
- **Action** : `control-unavailable`, `crashed`, `awaiting tunnel` ⇒ recreate
  Compose avec l'image et les secrets courants.
- **Acceptation** : une station crashée revient sans commande manuelle.
- **Test** : `test_binder_maps_a_crashed_station_to_a_compose_fallback_hint`.

### T211 — DNS transitoire (P1) — en cours
- **Fichiers** : `gluetun/internal/vpn/ladder.go`,
  `gluetun/internal/dns/ladder/*`.
- **Action** : egress retenté (fait), health DNS tolérant, aucun teardown d'un
  tunnel sain sur une erreur DNS passagère.
- **Acceptation** : une coupure DNS de 5 s ne tue pas un tunnel.
- **Test** : test ladder + live.

---

## Phase 4 — Acceptance et SLO

### T212 — Test zéro churn sur station saine (P1) — à faire
- **Fichiers** : `tests/test_fresh_chain_supervisor.py` (ou équivalent).
- **Action** : station saine + image plus récente ⇒ aucune chaîne, aucun
  recreate, `StartedAt` inchangé.
- **Acceptation** : 0 recreate sur 24 h.
- **Test** : test dédié.

### T213 — Réparation autonome de bout en bout (P0) — à faire
- **Fichiers** : logs superviseur, `logs/fresh_chain_N.log`.
- **Action** : casser une station de test, laisser le superviseur seul, vérifier
  `healthy` + `egress.verified=true`.
- **Acceptation** : aucune commande manuelle.
- **Test** : observation live.

### T214 — SLO de réparation (P1) — à faire
- **Fichiers** : `scripts/station_slo_watch.py`, métriques superviseur.
- **Action** : mesurer détection, durée, p95 switch, taux de commit,
  `AUTH_FAILED`/min, recreates.
- **Acceptation** : NFR-001..003 mesurés sur 24 h.
- **Test** : échantillons vs calcul manuel.

### T215 — Benchmark du chemin chaud (P2) — à faire
- **Fichiers** : `scripts/bench_*.py`.
- **Action** : 20 switches sur une station de test.
- **Acceptation** : p95 ≤ 45 s, aucun `docker exec` sur le chaud.
- **Test** : rapport.

### T216 — Acceptance 6/6 et soak (P0) — à faire
- **Fichiers** : `config.yaml`, `registry.db`, dashboard.
- **Action** : vérifier 6/6 `client_usable`, IP distinctes, proxies HTTP/SOCKS5,
  1 h puis 24 h.
- **Acceptation** : SC-001..006.
- **Test** : contrôle live + SLO.

### T217 — Une station sans 200 frais n'est pas « récupérée » (P0) — fait
- **Fichiers** : `scripts/fresh_chain_supervisor.py`,
  `tests/test_fresh_chain_supervisor.py`.
- **Action** : `_station_recovered` exige `model_usable=true` ; aucune exception
  par raison de panne. Une station `healthy` sans preuve 200 reste dans la
  chaîne de réparation.
- **Acceptation** : aucune station n'est comptée `client_usable` sans 200 frais.
- **Test** : `test_recovered_requires_a_fresh_model_proof`,
  `test_recovered_rejects_an_unusable_tunnel`.

---

## Ordre d'exécution

1. T201–T203 (rotate unique) — débloque la vitesse et l'autonomie.
2. T204–T207 (candidats) — évite de brûler le budget.
3. T208–T211 (auth, conteneur, DNS) — répare les causes racines.
4. T212–T216 (preuves) — acceptance 6/6.

## Preuves attendues

```text
python -m pytest tests/test_rotation_contract.py \
  tests/test_fresh_chain_auto_transport.py -q
go test ./internal/control/jobs ./internal/vpn/... -count=1
supervisor log: 0 manual repair, 0 idempotency_conflict
state: 6/6 client_usable, distinct egress IPs
```
