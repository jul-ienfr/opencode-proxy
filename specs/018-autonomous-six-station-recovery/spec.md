# Feature Specification: Récupération autonome des six stations

**Feature Branch**: `018-autonomous-six-station-recovery`

**Created**: 2026-11-14

**Status**: Active

**Input**: « Fais-moi un plan avec SpecKit pour corriger tous les problèmes
restants et que, suite à ces corrections, le code puisse enfin tout seul faire
fonctionner toutes les stations, que toutes les stations soient utilisables. »

## Objectif

Le code (fork Gluetun 016 + superviseur Python) doit ramener la flotte à
**6/6 stations `client_usable`** sans intervention humaine, en free, en ne
réparant que les stations inutilisables.

La cible n'est pas « le superviseur arrive parfois à reconnecter une station ».
La cible est : **une station cassée est détectée, réparée et prouvée par le code
seul ; une station saine n'est pas touchée ; la flotte revient à 6/6 et y reste.**

## Périmètre

1. **Primitive de réparation** : un seul `POST /v1/vpn/rotate` par chaîne, avec
   la liste ordonnée du registre ; le fork exécute les candidats et le ladder
   complet.
2. **Qualification des candidats** : warm pool frais, serveurs publics récents,
   exclusion des preuves périmées, jamais de re-score par le fork.
3. **Authentification** : détection `AUTH_FAILED`, rafraîchissement des
   identifiants de service, pause bornée, repli WireGuard, pas de martèlement.
4. **Niveau conteneur** : détection d'un conteneur mort/crashé/API injoignable,
   recreate Compose avec l'image et les secrets courants.
5. **DNS/routage** : preuve d'egress retentée, DNS tolérant aux transitions,
   pas de démontage d'un tunnel sain sur une erreur DNS transitoire.
6. **Observabilité** : événements, métriques, SLO de réparation, preuve 6/6.
7. **Non-régression** : zéro recreate d'une station saine pour une migration.

**Hors périmètre** : jambe payante, second compte, attente comme solution,
septième station ou septième session, réécriture big-bang des monolithes.

## Contraintes dures

- Six stations, pas une de plus ; `station_count=6`, `target_slots=6`.
- Free uniquement ; le quota est par IP. Aucune conclusion « plus d'IP » sans
  test de l'IP concernée.
- Le fork ne lit pas `registry.db`, ne score pas, ne claim pas.
- `registry.db` reste l'autorité IP/leases/quota/SLO.
- WireGuard, OpenVPN UDP et OpenVPN TCP restent tous dans la politique ; un
  transport peut être sauté par breaker, jamais supprimé.
- Une station saine n'est jamais recréée pour une raison de version.
- Aucun gel du superviseur : il est le seul propriétaire des réparations.

## Classes de panne à couvrir

| Classe | Détection | Réparation attendue |
|---|---|---|
| Tunnel mort | healthcheck, egress non vérifié | rotate candidat suivant / ladder |
| Endpoint mort | préflight, handshake, no_egress | candidat suivant, sans disable |
| `AUTH_FAILED` | logs OpenVPN, reason auth | refresh identifiants, cooldown, repli WG |
| DNS transitoire | échec de résolution, `no_quorum` | egress retenté, DNS health, pas de teardown |
| Conteneur crashé | `vpn_status=crashed`, API absente | recreate Compose, image courante |
| Image/secrets périmés | comparaison image/env | recreate une fois sur station cassée |
| Registre périmé | `last_ok_at` ancien | warm pool frais, public récent |
| Quota 429 | probe modèle | rotate vers une nouvelle IP |
| API de contrôle down | `control-unavailable` | repli Compose |
| Job bloqué | état non terminal, timeout | annulation/rollback, candidat suivant |

## Scénarios utilisateurs

### User Story 1 — Le code répare seul une station cassée (P1)

Une station perd son tunnel. Le superviseur la détecte et envoie un `rotate`
avec plusieurs candidats ; le fork essaie les transports, prouve l'egress et
commit. Aucune commande manuelle.

**Test indépendant** : casser une station de test (pas une station qui sert) et
vérifier qu'elle revient `healthy` + `egress.verified=true` sans intervention.

**Critères d'acceptation** :

1. **Given** une station sans egress, **When** le superviseur la voit,
   **Then** il lance une chaîne dans les 30 s.
2. **Given** une liste de candidats, **When** le fork exécute le rotate,
   **Then** il essaie WG, UDP, TCP par candidat et s'arrête au premier egress
   prouvé.
3. **Given** un candidat mort, **When** le rotate l'épuise, **Then** il passe au
   suivant sans désactiver la station.

---

### User Story 2 — Une station saine n'est jamais touchée (P1)

Une station `client_usable=true` conserve son image, son uptime et son IP même
si une nouvelle image existe.

**Test indépendant** : laisser tourner 24 h et comparer `StartedAt`, image et IP.

**Critères d'acceptation** :

1. **Given** `station_usable=true` et `model_usable=true`, **When** le
   superviseur cycle, **Then** aucune chaîne n'est créée pour elle.
2. **Given** une nouvelle image, **When** aucune panne n'est détectée,
   **Then** la station n'est pas recréée.

---

### User Story 3 — Les identifiants sont appliqués automatiquement (P1)

Une rotation d'identifiants OpenVPN/NordLynx est détectée ; le superviseur
recrée la station cassée avec les nouveaux secrets, puis reprend le chemin
chaud.

**Critères d'acceptation** :

1. **Given** un conteneur avec d'anciens identifiants, **When** une réparation
   démarre, **Then** un recreate Compose est forcé.
2. **Given** un `AUTH_FAILED` récent, **When** une chaîne démarre,
   **Then** elle attend le cooldown et privilégie WireGuard, sans marteler
   OpenVPN.

---

### User Story 4 — La flotte revient à 6/6 et y reste (P1)

Après réparation, les six stations sont `client_usable`, avec des IP distinctes
et des proxies fonctionnels, pendant au moins une heure puis 24 h de soak.

**Critères d'acceptation** :

1. **Given** une flotte dégradée, **When** le code répare, **Then** 6/6
   `client_usable` en ≤ 30 min.
2. **Given** 6/6, **When** une station tombe, **Then** elle est réparée sans
   faire tomber les cinq autres.

## Exigences fonctionnelles

- **FR-001** : un seul job `rotate` par chaîne, exécuté par le fork.
- **FR-002** : le superviseur envoie une liste ordonnée ; le fork ne re-score
  jamais.
- **FR-003** : le warm pool et les preuves registre sont rafraîchis ; les
  preuves de plus de 6 h ne sont pas classées premières.
- **FR-004** : `AUTH_FAILED` déclenche refresh des identifiants, cooldown borné
  et repli WG ; pas de boucle d'auth.
- **FR-005** : un conteneur mort/crashé/API down déclenche un recreate Compose.
- **FR-006** : des identifiants ou une image périmés déclenchent un recreate
  une fois, uniquement sur une station à réparer.
- **FR-007** : l'egress est retenté avant de démonter un tunnel ; un DNS
  transitoire ne doit pas tuer un tunnel sain.
- **FR-008** : les jobs sont idempotents par cible ; aucun
  `idempotency_conflict`.
- **FR-009** : les candidats morts sont abandonnés rapidement, sans disable.
- **FR-010** : chaque réparation est observable (image, transport, IP, raison,
  durée).
- **FR-011** : aucune station/session supplémentaire ; free uniquement.
- **FR-012** : le superviseur n'est jamais gelé pour une migration.
- **FR-013** : seul `client_usable` compte. Une station `healthy` sans preuve
  réelle de 200 free frais est un **target de réparation**, jamais un succès ;
  elle n'est ni comptée ni protégée.

## Exigences non fonctionnelles

- **NFR-001** : 6/6 `client_usable` en free, IP distinctes.
- **NFR-002** : détection ≤ 30 s ; réparation jusqu'à egress prouvé p95 ≤ 180 s.
- **NFR-003** : switch p95 ≤ 45 s sur le premier transport, ≤ 180 s avec repli.
- **NFR-004** : 0 recreate d'une station saine sur 24 h.
- **NFR-005** : aucun `docker exec`/`docker cp` sur le chemin chaud quand l'API
  répond.
- **NFR-006** : un seul tunnel actif par station, pas de double tunnel.
- **NFR-007** : tests Go/Python, benchmark et soak verts.

## Critères de succès

- **SC-001** : 6/6 `client_usable` pendant 1 h, puis 24 h de soak.
- **SC-002** : réparation autonome d'une station cassée, sans commande manuelle.
- **SC-003** : zéro recreate d'une station saine sur 24 h.
- **SC-004** : trois transports visibles et conservés.
- **SC-005** : proxies HTTP et SOCKS5 fonctionnels sur chaque station.
- **SC-006** : aucune intervention manuelle dans les logs de réparation.

## Risques externes

- Le compte NordVPN peut imposer une limite de sessions ou un throttle
  `AUTH_FAILED`. Le code peut rafraîchir les identifiants, mettre en pause et
  basculer WG ; il ne peut pas créer de capacité payante ou un second compte.
- Les endpoints NordVPN changent souvent ; la qualification live doit rester
  fraîche.
- Le réseau peut bloquer un transport ; le ladder doit basculer, pas supprimer
  le transport.
