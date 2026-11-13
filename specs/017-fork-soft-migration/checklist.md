# Checklist — Migration douce vers le fork Gluetun 016

**Feature** : `017-fork-soft-migration`
**Date** : 2026-11-14

## Fork

- [x] T101 — IP du serveur résolue depuis le catalogue provider.
- [x] T102 — endpoint explicite ⇒ pas de DNS dans le préflight.
- [x] T103 — ladder complet quand `VPN_TYPE` n'est pas explicite.
- [x] T104 — budgets switch/rollback compatibles avec le ladder.
- [x] Image `gluetun-016:local` reconstruite et déployée.
- [x] Tests Go `internal/vpn`, `internal/control`, `internal/server` verts.

## Superviseur

- [x] T105 — ports loopback 8001-8006 publiés.
- [x] T105 — `control_plane.mode=active`, base URL loopback, clé dédiée.
- [x] T106 — `GluetunControlV2Binder` câblé dans `fresh_chain`.
- [x] T107 — clé d'idempotence par hostname et hash rotate.
- [x] T108 — repli Compose sur `resolution_failed`/`crashed`/`awaiting tunnel`.
- [x] T109 — override `VPN_TYPE=` vide en mode auto.
- [x] T117 — rotation d'identifiants détectée et recreate forcé.
- [x] T118 — identifiants NordVPN rafraîchis via le token existant.
- [x] Tests Python binder/contrat/config verts.
- [ ] T110 — test de non-régression « station saine jamais recréée ».
- [ ] T111 — réparation superviseur validée de bout en bout en live.
- [ ] T112 — runbook sans gel du superviseur ni réparation manuelle normale.

## Qualité et performance

- [ ] T113 — SLO de réparation mesurés (détection, durée, p95 switch).
- [ ] T115 — benchmark 20 switches, p95 ≤ 45 s sur le premier transport.
- [ ] Aucun `docker exec`/`docker cp` sur le chemin chaud quand l'API répond.
- [ ] Aucun `idempotency_conflict` sur 20 cibles successives.
- [ ] Breakers/backoffs bornés et journalisés.
- [ ] Logs sans secret.

## Acceptance

- [ ] 6/6 stations `client_usable` en free.
- [ ] Six IP d'egress distinctes.
- [ ] Trois transports visibles dans `/v1/vpn/transport` et conservés.
- [ ] 0 recreate d'une station saine sur 24 h.
- [ ] Aucun payant, second compte, attente, septième station ou session.

## Definition of Done

La feature est terminée quand :

1. une station saine traverse 24 h sans recreate ni changement d'image ;
2. une station cassée est réparée par le superviseur seul, avec
   `egress.verified=true`, en p95 ≤ 180 s ;
3. les trois transports restent dans la politique et le transport commité est
   visible ;
4. les tests Go/Python et le benchmark passent ;
5. 6/6 stations sont `client_usable` en free, avec des IP distinctes.
