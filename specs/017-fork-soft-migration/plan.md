# Implementation Plan: Migration douce vers le fork Gluetun 016

**Branch**: `017-fork-soft-migration` | **Date**: 2026-11-14 | **Spec**: [spec.md](./spec.md)

**Input**: `specs/017-fork-soft-migration/spec.md`, code fork
`gluetun@016-control-plane`, superviseur `scripts/fresh_chain_supervisor.py`,
binder `free/ip_registry/adapters/`.

## Summary

La flotte doit migrer vers `gluetun-016:local` **sans jamais recréer une station
qui sert**. Le superviseur reste le propriétaire unique des réparations ; le
chemin chaud passe par l'API de contrôle du fork, avec un repli Compose quand
la station est trop cassée pour un switch in-place.

Trois verrous techniques ont été identifiés et corrigés dans le fork :

1. le préflight du ladder résolvait le hostname par le DNS du tunnel, qui est
   fail-closed avant le tunnel ; il utilise désormais l'IP du catalogue
   provider ;
2. le job de switch attendait 30 s, moins qu'un repli WireGuard → UDP ; les
   budgets couvrent maintenant le ladder complet ;
3. le switch executor limitait le ladder au `VPN_TYPE` par défaut ; il utilise
   les trois transports quand `VPN_TYPE` n'est pas explicite.

Côté superviseur :

1. le binder v2 existait mais n'était pas câblé (`control_plane.mode=legacy`,
   attribut `is_active` mal nommé) ;
2. le port 8000 n'était pas publié sur l'hôte, donc le processus Windows ne
   pouvait pas joindre l'API ;
3. la clé d'idempotence était réutilisée pour chaque hostname, ce qui produisait
   `idempotency_conflict` ;
4. un switch sur une station crashée échouait sans recreate Compose.

## Contexte technique

- **Fork** : Go, image `gluetun-016:local`, routes `/v1/control/*`,
  `/v1/vpn/{switch,rotate,jobs,transport}`, `/v1/egress/*`.
- **Superviseur** : Python host, `scripts/fresh_chain_supervisor.py` +
  `scripts/fresh_chain.py`, pas d'accès direct au réseau Docker.
- **Binder** : `GluetunControlV2Binder` (API), `GluetunControlBinder` (legacy
  `docker exec`), `ComposeHostnameBinder` (recreate), `HybridBinder` (route).
- **Compose** : `docker-compose.yml` + `logs/compose-hostname-N.yml` +
  `logs/compose-control-plane-N.yml` (image, ports, contrôle).
- **Registre** : `registry.db` reste l'autorité IP/leases/quota/SLO.

## Contraintes

- Six stations, free uniquement ; quota par IP ; aucune attente, aucun payant,
  aucun second compte, aucune septième session.
- Le fork n'écrit jamais dans `registry.db` et ne re-score jamais un candidat.
- Une station saine n'est jamais recréée pour une migration de version.
- Les trois transports restent dans le ladder ; un transport peut être sauté
  par breaker, jamais supprimé de la politique.
- Chaque réparation doit être prouvée par consensus d'egress avant commit.

## Phases

### Phase 0 — Baseline et invariants (fait)

- Figer l'image fork et son digest.
- Vérifier `capabilities` : `switch`, `rotate`, `transport_ladder`.
- Vérifier que le superviseur ne répare que `station_usable=false`,
  `model_usable=false` ou sans IP (`_needs_chain`).
- Interdire toute action de migration sur une station saine.

### Phase 1 — Fork : ladder avant tunnel (fait)

- `internal/vpn/ladder.go` : `targetFor` résout l'IP via
  `provider.GetConnection`, avec `ServerSelection.VPN` et `Mode` par défaut.
- `internal/vpn/transport/preflight.go` : une `EndpointIP` explicite
  court-circuite la résolution DNS.
- `internal/control/jobs/executor.go` : `VPNExplicit=false` ⇒ ladder complet ;
  budgets `switch=180s`, `await=120s`, `verify=60s`.
- `internal/control/jobs/rollback.go` : budgets `apply=60s`, `await=120s`,
  `verify=60s`.
- Tests : `internal/vpn/ladder_target_test.go`,
  `internal/vpn/transport/preflight_test.go`,
  `internal/control/jobs/executor_test.go`,
  `internal/control/jobs/transport_policy_test.go`.

### Phase 2 — Superviseur : chemin chaud v2 (fait)

- `config.yaml` : `control_plane.mode=active`,
  `base_url=http://127.0.0.1:800{station}`,
  `api_key_env=VPN_CONTROL_API_KEY`, `transport_owner=fork`,
  `switch_timeout_s=180`, `rotate_timeout_s=300`.
- `logs/compose-control-plane-N.yml` : ports
  `127.0.0.1:800N:8000`, sources egress, backoffs bornés, cooldowns DNS.
- `scripts/fresh_chain.py` : import et construction de
  `GluetunControlV2Binder` quand `is_active` ; `cycle_id` unique ; clé
  d'idempotence par cible ; `tri_stack` ⇒ une seule tentative `auto`.
- `free/ip_registry/adapters/gluetun_control_v2.py` : clé par hostname,
  hash des candidats pour rotate, repli `control-unavailable` sur
  `resolution_failed`, `crashed`, `awaiting tunnel`.
- `free/ip_registry/adapters/hybrid_binder.py` : le binder v2 est prioritaire ;
  repli Compose sur `control-unavailable:*`.
- `free/ip_registry/adapters/compose_binder.py` : `vpn_type=auto` écrit
  `VPN_TYPE=` vide.

### Phase 3 — Migration douce (en cours)

- Laisser le superviseur tourner en permanence ; ne jamais le geler.
- Ne recréer que les stations cassées, une par une, avec l'image courante.
- Vérifier après chaque réparation : `healthy`, `egress.verified=true`, IP
  différente de l'hôte, proxies HTTP/SOCKS5.
- Si une station saine est détectée, ne pas la toucher, même si son image est
  plus ancienne.
- Critère de sortie : 6/6 `client_usable`, 0 recreate de station saine sur 24 h.

### Phase 4 — Observabilité et qualité (à faire)

- Mesurer : détection, durée de réparation, p95 switch, taux de commit,
  nombre de recreates, `idempotency_conflict`, `docker exec` sur le chaud.
- Ajouter un test de non-régression « station saine jamais recréée » basé sur
  `_needs_chain` et sur l'image/`StartedAt`.
- Vérifier les logs : aucune fuite de clé, aucun `docker cp` sur le chaud.
- Mettre à jour le runbook et `implementation-status.md` du 016.

## Migration matrice

| Station | État courant | Action | Critère |
|---|---|---|---|
| 1 | cassée / réparable | superviseur : switch v2 ou recreate | egress vérifié |
| 2 | saine | **ne pas toucher** | `StartedAt` stable |
| 3 | réparée UDP | **ne pas toucher** si saine | egress vérifié |
| 4 | cassée | superviseur : recreate auto | egress vérifié |
| 5 | cassée | superviseur : recreate auto | egress vérifié |
| 6 | cassée | superviseur : recreate auto | egress vérifié |
| 7 | saine (hors périmètre 016) | **ne pas toucher** | inchangée |

La matrice est réévaluée à chaque cycle ; une station qui redevient saine sort
de la liste des actions.

## Risques et mitigations

| Risque | Mitigation |
|---|---|
| DNS du tunnel mort bloque la résolution | IP du catalogue provider dans `targetFor` |
| Switch in-place sur une boucle crashée | repli Compose sur `crashed`/`awaiting tunnel` |
| Clé d'idempotence réutilisée | clé par hostname + hash rotate |
| Ladder plus long que le job | budgets 180/120/60 s |
| Port de contrôle non publié | ports loopback 8001-8006 |
| Migration qui churn une station saine | `_needs_chain` + critère `StartedAt`/image |
| Perte des transports | `TRANSPORT_LADDER` complet, aucun retrait |

## Critères de qualité

- **Fonctionnel** : 6/6 `client_usable`, trois transports visibles, rotation
  automatique sur 429.
- **Fiable** : aucun recreate d'une station saine ; egress prouvé avant commit ;
  breaker/backoff bornés.
- **Rapide** : switch p95 ≤ 45 s sur le premier transport, ≤ 180 s avec repli ;
  réparation p95 ≤ 180 s.
- **Performant** : chemin chaud sans `docker exec`/`docker cp` ; un seul job
  actif par station ; pas de double tunnel.
- **Qualitatif** : tests Go/Python verts, docs à jour, logs sans secret.

## Dépendances

- Image fork construite et taguée `gluetun-016:local`.
- `credentials.env` avec `VPN_CONTROL_API_KEY` (jamais dans `config.yaml`).
- Ports 8001-8006 libres sur l'hôte.
- `registry.db` accessible au superviseur.
