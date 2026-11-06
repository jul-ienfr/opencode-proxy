# Implementation Plan: Disponibilité permanente des stations free

**Branch**: `001-free-station-availability` | **Date**: 2026-11-04 | **Spec**: [spec.md](./spec.md)

**Input**: Spec 001 + audit `docs/AUDIT_SYSTEME_FREE_2026-11-04.md`

## Summary

Le système ne tient pas le SLO parce qu'une réparation coûte **13 min en
médiane** et que la station est détruite pendant toute la réparation. Le plan
vise donc deux variables, pas une : **fréquence × MTTR**. Objectif : MTTR
< 60 s (meilleur cas mesuré : 27 s) et ≤ 4 réparations/mois/station, soit
≤ 4 min d'indispo mensuelle.

Approche : mesurer d'abord sans agir, augmenter le rendement des candidats
(98 % de `no-egress` aujourd'hui), rendre le reset fournisseur infranchissable,
puis seulement raccourcir le pipeline. Chaque lot a un critère d'acceptation,
un rollback et un mode ombre.

## Technical Context

**Language/Version**: Python 3.11+ (3.12 sur la machine), asyncio, FastAPI.

**Primary Dependencies**: Docker Compose + gluetun (digest épinglé), SQLite,
`curl_cffi`, pytest. Aucune dépendance nouvelle.

**Storage**: SQLite `logs/ip_registry.db` (autorité), `logs/catalog_claims.db`
(claims), JSON de cache (`free_pool.json`, `vpn_state{n}.json`) — cache, jamais
vérité.

**Testing**: pytest ; tests de contrat pour les invariants (reset, exclusivité,
diversité, absence de littéraux) ; tests d'intégration Compose sur 2 stations ;
mesure live sur 7 jours minimum ou 50 réparations.

**Target Platform**: Windows (Docker Desktop), service supervisé ; compatible
Linux (systemd existant).

**Performance Goals**: MTTR p50 < 60 s, p95 < 120 s ; détection < 10 s ;
bascule client invisible sur les stations saines ; disponibilité ≥ 99,9 % par
station, 99,99 % visée.

**Constraints**: 6 sessions NordVPN maximum, budget de rebind borné par le
compte, trafic ~20 req/jour (pic 10×), pas de 7e slot sans décision explicite.

**Scale/Scope**: 6 stations aujourd'hui, N configurable (1 à 10 conteneurs) ;
9 948 serveurs au catalogue ; 3 435 IP candidates.

## Constitution Check

| Porte | État | Preuve / action |
|---|---|---|
| I. Disponibilité mesurée | ❌ | échantillonneur SLO jamais lancé (Lot 0) |
| II. Trois ressources séparées | ❌ | 7 stores, 5 alphabets (Lots 1–2) |
| III. Source de vérité unique | ❌ | le registre SQLite n'est pas lu à la requête (Lot 2) |
| IV. Reset = barrière dure | ❌ | plafond 120 s, ledger daté, lease sans `reset_at` (Lot 2) |
| V. IP exclusive | ✅ | `assign_observed` + index d'unicité |
| VI. Diversité | ❌ | `max_per_24: 6`, `country_rotation: false` (Lot 1) |
| VII. Preuve de contenu | ✅ | `probe_validate_sse: true` |
| VIII. Autonomie + interrupteur | ❌ | superviseur gated sur `mode`, pas de mode `observe` (Lot 0) |
| IX. Score ordonne | ❌ | note non lue par le sélecteur (Lot 2) |
| X. Dégradation explicite | ⚠️ | `capacity_degraded` existe, pas d'incident ni d'alerte par défaut |

## Décisions d'architecture

1. **Un propriétaire par cycle de vie.** Le registre SQLite est le seul writer
   de l'état d'une IP. `IpQuotaLease` devient une vue calculée, non persistée.
   Les cinq alphabets sont ramenés à une machine à états unique :
   `candidate → validating → warm → active → draining → cooling(reset_at) →
   dead`, plus `retired`.
2. **`reset_at` porté par l'IP, pas par le jour.** Toute sélection, tout probe
   et tout claim vérifient `reset_at > now` en premier. Aucune exception, aucun
   plafond de cooldown ne peut le raccourcir.
3. **Un seul score.** La note de la spec est calculée à partir des observations
   et lue par le sélecteur. Les trois classements actuels sont supprimés.
4. **Pré-qualifier avant de détruire.** Un candidat doit avoir un hostname
   présent dans l'union des manifestes, un transport connu et un historique
   d'auth non cassé **avant** le recreate. Si aucun candidat n'est
   pré-qualifié, on alerte au lieu de boucler 40 fois.
5. **Réparation bornée par un budget de temps.** Chaque étape a un plafond :
   détection 10 s, décision 2 s, recreate 8 s, egress 5 s, probe 5 s, écriture
   1 s. Un candidat qui dépasse est abandonné, pas attendu.
6. **Observation native.** SLO par station toutes les 30 s, journal
   d'incidents SQLite, endpoint et alerte. Le SLO se calcule, il ne se déclare
   pas.
7. **Strangler, pas big-bang.** Les anciens stores sont supprimés dans un ordre
   défini, avec une période où l'ancien et le nouveau sont comparés en lecture
   seule (mode ombre) avant bascule.

## Budget MTTR

| Étape | Budget | Aujourd'hui |
|---|---|---|
| Détection (event-driven + poll 5 s) | 10 s | ~11 s |
| Décision + claim | 2 s | inclus dans 41 s |
| Pré-qualification (hors chemin critique) | 0 s | absente |
| Recreate Compose | 8 s | 5–9 s |
| Egress (1 URL, 1 schéma, 2 tentatives) | 5 s | 0–45 s |
| Probe (1 modèle, 1 schéma) | 5 s | 5 s |
| Écriture registre + pin | 1 s | ~1 s |
| **Total p50** | **31 s** | **785 s** |

Marge ×2 → **60 s**. Le meilleur cas mesuré (27 s) prouve que le budget est
atteignable ; l'écart est entièrement dû aux attentes fixes et au budget de
rebind.

## Lots

### Lot 0 — Mesurer (aucun risque)

- Implémenter `allocator.mode: observe` (tourne, journalise, n'agit pas).
- Échantillonneur SLO 30 s par station + table `incidents` (début, fin, cause).
- `SqliteIpRegistry.reconcile()` au boot.
- Verrou inter-processus allocateur ↔ manager.
- Alerte active par défaut (webhook si présent, sinon incident dashboard).

**DoD** : 7 jours de données, baseline MTTR/fréquence/disponibilité publiée,
zéro action de réparation. **Rollback** : `mode: off`.

### Lot 1 — Rendement des candidats

- Pré-qualification (manifeste, transport, auth) avant recreate.
- Suppression de `PROVEN_HOSTS`, des pins `.env`, des overrides périmés.
- Diversité réelle : `max_per_24: 2`, `max_per_country: 2`, re-test après
  cooldown pays.
- `probe_models` à 3 entrées.
- Métrique `candidate_yield` par pays et par source.

**DoD** : `no-egress` < 50 % des binds (contre 98 %), ≥ 2 IP vérifiées dans
≥ 20 pays. **Rollback** : liste de pré-qualification désactivée.

### Lot 2 — Vérité quota

- `reset_at` dans le modèle d'IP, lu par le sélecteur ; suppression du
  `min(retry_after, 120)` et de la clé de ledger datée.
- `EXHAUSTED` exige `reset_at`, sinon `ERROR`.
- `model` transmis à tous les re-picks ; exclusion cumulative en stream.
- Un seul score lu par le sélecteur ; `IpQuotaLease` en lecture seule.
- Mode ombre : comparaison ancien/nouveau classement pendant 48 h.

**DoD** : 0 sélection avec `reset_at` futur sur 10 000 décisions (test +
mesure), 0 re-pick sans `model`, écart de classement expliqué. **Rollback** :
feature flag `quota_gate_v2`.

### Lot 3 — MTTR

- Suppression du `settle_s` aveugle : poll dès la fin du recreate.
- Egress : 1 URL, 1 schéma, 2 tentatives, timeout 2 s, en parallèle du probe
  quand possible.
- Suppression du `--delay 8` ; budget de rebind par besoin, pas par chaîne.
- `max_candidates_per_chain: 8`, `max_parallel_repairs: 2`.
- Timeouts par tentative : TTFB 12–15 s, total 30 s, lecture 60 s maximum.

**DoD** : MTTR p50 < 60 s, p95 < 120 s sur 50 réparations. **Rollback** :
valeurs de config précédentes.

### Lot 4 — Capacité et couverture

- Warm pool vérifié (≥ 6 IP), API et promotion automatique.
- Couverture pays : objectif 2 IP vérifiées par pays, échantillonnage priorisé
  par score (pas d'énumération des 9 948 serveurs).
- Exploration ≥ 5 % des probes.
- `station_count` jusqu'à 10, stations au-delà du budget en `standby`.
- Décision « 7e session » prise sur les chiffres du lot 3.

**DoD** : ≥ 5/6 utilisables 99,99 % sur 30 jours, 6/6 ≥ 99,9 %, aucun /24
au-delà de 2 stations.

### Lot 5 — Maintenabilité (continu)

- Suppression des stores redondants dans l'ordre : `_model_ip_health` →
  `IpQuotaLease` → `BucketState`, au fur et à mesure que le registre les
  couvre.
- Tests de contrat : reset, exclusivité, diversité, absence de littéraux,
  `model` sur le failover.
- Rotation des logs superviseur/chaînes ; logs structurés JSONL pour les
  décisions.
- Un runbook par incident type (0/6, pool COOLING, AUTH_FAILED, pays épuisé).

**DoD** : un seul store persistant, zéro littéral de hostname/pays dans la
sélection, chaque invariant couvert par un test qui échoue s'il régresse.

## Project Structure

```text
free/
├── ip_registry/          # domaine, registre SQLite, scoring, allocateur
│   ├── domain.py         # machine à états unique
│   ├── sqlite_registry.py
│   └── adapters/
├── pool.py               # sélection runtime
├── ip_quota.py           # vue dérivée, plus de persistance
├── quota_ledger.py       # clé sans jour
└── catalog_claims.py

scripts/
├── fresh_chain.py        # pipeline de réparation
├── fresh_chain_supervisor.py
├── station_slo_watch.py  # à auto-démarrer
└── vpn_alerts.py

tests/
├── contract/             # invariants (reset, diversité, hardcode)
├── integration/          # 2 stations Compose
└── unit/
```

## Risks

| Risque | Probabilité | Mitigation |
|---|---|---|
| Le compte NordVPN bloque les rebinds | moyenne | budget mesuré, backoff, 2 chaînes max |
| La pré-qualification assèche le pool | moyenne | seuil de yield, fallback catalogue, alerte |
| Le mode `observe` n'existe pas | certaine | l'implémenter avant tout le reste |
| Un refactor d'état casse la sélection | moyenne | mode ombre 48 h + flag par lot |
| Le SLO reste sous 99,9 % | moyenne | décision 7e session sur données, pas sur intuition |
| Les logs non tournés masquent un incident | faible | rotation au lot 5, taille bornée |

## Complexity Tracking

| Violation | Pourquoi | Alternative rejetée |
|---|---|---|
| Deux systèmes de réparation transitoires (manager + allocateur) | le manager doit garder la main sur l'auth | un seul propriétaire dès le lot 0, verrou commun |
| JSON conservés pour l'état manager | migration trop risquée en une fois | big-bang SQLite rejeté : perte de session en cours |
| Score et gates séparés | un score ne doit jamais contourner `reset_at` | score unique qui décide tout : dangereux |
