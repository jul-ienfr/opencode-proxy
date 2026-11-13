# Implementation Plan: Récupération autonome des six stations

**Branch**: `018-autonomous-six-station-recovery` | **Date**: 2026-11-14 | **Spec**: [spec.md](./spec.md)

**Input**: `spec.md`, ADR-017-01 (`specs/017-fork-soft-migration/decisions.md`),
code fork 016, superviseur `scripts/fresh_chain_supervisor.py`.

## Summary

Le code doit passer de « réparation assistée » à « récupération autonome ».
Le point central est **T119/T201** : le superviseur cesse de faire un switch par
hostname et envoie un seul `rotate` ordonné au fork. Le fork exécute les
candidats et le ladder complet, prouve l'egress et rollback ; le superviseur
garde le registre, les quotas, les claims, le warm pool, les probes et les
recreates d'image/secrets.

Les autres corrections sont des garde-fous autour de cette primitive :
qualification fraîche des candidats, politique `AUTH_FAILED`, détection
d'identifiants/image périmés, récupération conteneur, DNS transitoire, SLO.

## Décision d'architecture

Voir ADR-017-01 : **le fork répare la station, le superviseur répare la flotte.**

- Fork : `rotate` (candidats + tri-stack + egress + rollback), état, jobs.
- Superviseur : registre, quotas, claims, warm pool, probes, image/secrets,
  inter-stations.
- Le fork ne choisit jamais une IP ; le superviseur ne micromanage plus les
  transports.

## Contexte technique

- Fork Go : `/v1/vpn/rotate`, `/v1/vpn/state`, `/v1/vpn/transport`,
  `/v1/egress`, transport ladder, jobs idempotents.
- Superviseur Python : `fresh_chain_supervisor.py`, `fresh_chain.py`,
  `GluetunControlV2Binder`, `HybridBinder`, `ComposeHostnameBinder`.
- `registry.db` : IP, leases, quotas, claims, warm pool, preuves.
- Compose : `logs/compose-hostname-N.yml` + `logs/compose-control-plane-N.yml`,
  ports loopback 8001-8006.

## Phases

### Phase 0 — Mesure et invariants (1 jour)

- Mesurer l'état courant : 6/6 ? réparations/min ? `AUTH_FAILED`/min ?
- Vérifier que le superviseur ne répare que les stations inutilisables.
- Geler les invariants : six stations, free, trois transports, zéro churn.

**Sortie** : baseline chiffrée et dashboard de réparation.

### Phase 1 — Primitive `rotate` dans le fork (2 jours)

- **T201** : `_run_claims` appelle `GluetunControlV2Binder.rotate_outcome` avec
  la liste ordonnée, `transport_policy=None` (auto), `max_candidates`,
  `max_attempts`. Un seul job par chaîne.
- **T202** : traiter `committed`, `exhausted`, `rolled_back` sans désactiver la
  station ; `exhausted` ⇒ candidat suivant / nouvelle liste.
- **T203** : le fork essaie WG → UDP → TCP par candidat ; le job expose les
  tentatives et le transport commité.

**Sortie** : une chaîne = un job ; plus de boucle switch par host.

### Phase 2 — Qualité des candidats (2 jours)

- **T204** : warm pool frais (preuves < 6 h) et public récent avant embedded.
- **T205** : qualification live avant classement (tunnel + DNS), pas seulement
  un score historique.
- **T206** : exclusion des IP partagées et des preuves expirées ; jamais de
  re-score par le fork.
- **T207** : budget de candidats borné (`max_candidates=20`, `max_attempts=10`)
  et abandon rapide d'un candidat mort.

**Sortie** : les réparations ne brûlent plus leur budget sur des serveurs morts.

### Phase 3 — Authentification et niveau conteneur (2 jours)

- **T208** : politique `AUTH_FAILED` : refresh des identifiants de service,
  cooldown global borné, repli WG, pas de boucle.
- **T209** : détection d'identifiants/image périmés ⇒ recreate Compose une fois
  (déjà commencé en T117) ; reprise du chemin chaud après recreate.
- **T210** : conteneur mort/crashé/API down ⇒ recreate avec l'image et les
  secrets courants ; vérifier `vpn_status` et `egress` après recreate.
- **T211** : DNS transitoire : egress retenté (fait), health DNS tolérant,
  aucun teardown d'un tunnel sain sur une erreur DNS passagère.

**Sortie** : les pannes de credentials, d'image et de conteneur sont réparées
par le code.

### Phase 4 — Acceptance et SLO (3 jours)

- **T212** : test « station saine jamais recréée » sur 24 h.
- **T213** : test de réparation autonome de bout en bout sur une station cassée.
- **T214** : SLO : détection, durée de réparation, p95 switch, taux de commit,
  `AUTH_FAILED`/min, recreates.
- **T215** : benchmark 20 switches ; p95 ≤ 45 s sur le premier transport.
- **T216** : acceptance 6/6 `client_usable`, IP distinctes, proxies HTTP/SOCKS5,
  soak 1 h puis 24 h.

**Sortie** : 6/6 stable, preuves chiffrées, zéro intervention manuelle.

## Matrice de réparation

| Panne | Acteur | Action |
|---|---|---|
| Endpoint mort | fork | candidat suivant dans le rotate |
| Transport bloqué | fork | transport suivant (WG→UDP→TCP) |
| Egress non prouvé | fork | retry borné puis rollback |
| Quota 429 | superviseur | nouvelle liste de candidats |
| `AUTH_FAILED` | superviseur | refresh + cooldown + WG |
| Identifiants/image périmés | superviseur | recreate Compose |
| Conteneur crashé | superviseur | recreate Compose |
| API down | superviseur | repli Compose |
| Station saine | personne | aucune action |

## Risques et mitigations

| Risque | Mitigation |
|---|---|
| Limite de sessions NordVPN | cooldown, refresh, WG, pas de martèlement |
| Endpoints instables | qualification live, warm pool frais |
| DNS transitoire | egress retry, health DNS, pas de teardown |
| Registre périmé | preuves < 6 h, public récent |
| Recreate d'une station saine | garde `_needs_chain` + test 24 h |
| Job bloqué | timeout, cancel, rollback, candidat suivant |

## Critères de sortie

- 6/6 `client_usable` en free, IP distinctes, proxies OK.
- Réparation autonome p95 ≤ 180 s, détection ≤ 30 s.
- 0 recreate d'une station saine sur 24 h.
- Trois transports visibles et conservés.
- Aucune commande manuelle dans les logs de réparation.
