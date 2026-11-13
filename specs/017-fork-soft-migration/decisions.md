# ADR-017-01 — Répartition fork / superviseur

**Date** : 2026-11-14
**Statut** : Accepté
**Feature** : `017-fork-soft-migration`

## Question

La réparation d'une station doit-elle vivre dans le superviseur Python ou être
intégrée directement dans le fork Gluetun ?

## Décision

**Le fork répare la station. Le superviseur répare la flotte.**

Ce n'est pas « tout dans le fork » ni « tout dans le superviseur » :

### Dans le fork (par station)

- Exécuter une liste ordonnée de candidats dans **un seul** job
  `POST /v1/vpn/rotate`.
- Essayer `wireguard`, puis `openvpn-udp`, puis `openvpn-tcp` pour chaque
  candidat.
- Vérifier l'egress par consensus avant de commit.
- Rollback vers le snapshot initial si un candidat échoue.
- DNS, état persistant, jobs idempotents, breakers et backoff bornés.
- Renvoyer `committed`, `exhausted` ou `rolled_back`.

### Dans le superviseur (par flotte)

- `registry.db` : IP, leases, quotas par IP, score, SLO.
- Claims, warm pool, déduplication des IP entre stations.
- Construction de la liste **ordonnée** de candidats ; le fork ne re-score
  jamais.
- Probes des modèles free (200/429).
- Recreate pour changement d'image, de secrets/env, ou conteneur mort.
- Make-before-break inter-stations.

## Pourquoi

- Le fork tourne **dans** le conteneur : si le conteneur est mort ou
  crash-loop, il ne peut pas se recréer lui-même.
- Le fork ne connaît ni le registre, ni les quotas, ni les autres stations ;
  dupliquer cette logique créerait deux autorités.
- Un changement d'image ou de secrets ne s'applique qu'au recreate ; le fork ne
  peut pas modifier son propre environnement.
- Le fork ne doit pas choisir une IP : il exécute une liste ordonnée.

## Conséquences

- Le superviseur arrête de faire un `/v1/vpn/switch` par hostname : il envoie
  un `/v1/vpn/rotate` avec `transport_policy=auto`.
- Moins d'allers-retours, réparation plus rapide, un seul job par station.
- Les pannes de conteneur, d'image ou de secrets restent gérées par le
  superviseur, qui détecte les identifiants périmés et force le recreate.
- Le fork reste l'autorité du tunnel ; `registry.db` reste l'autorité des IP,
  quotas et claims.

## Prochaine étape

Remplacer la boucle `assign` par hostname dans `_run_claims` par un
`GluetunControlV2Binder.rotate(...)` unique, avec la liste ordonnée du registre.
