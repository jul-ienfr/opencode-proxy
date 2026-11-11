# Feature Specification: Fiabilité des requêtes free

**Feature Branch**: `012-request-reliability`

**Created**: 2026-11-08

**Status**: Draft

**Input**: Audit `docs/AUDIT_REQUETES_2026-11-08.md` — réduire les échecs
constatés le 2026-11-08 (2 647 faux `503`, 1 496 `429` finaux, 217 IP sans
succès, 6 052 `AUTH_FAILED`).

## Contexte

Le 2026-11-08, 7 767 tentatives ont été journalisées : 3 618 succès (46,6 %)
et 4 149 échecs. Trois causes dominent :

1. le smoke test SystemOne (`jev-1.13-free`, non-stream) reçoit un `503`
   « pool indisponible (proxy_mode=auto_egress) » alors que le pool WARP est
   disponible ;
2. les six stations NordVPN sont en `AUTH_FAILED`, donc tout le trafic free
   repose sur WARP ;
3. les endpoints WARP IPv4 partagent des adresses NAT déjà épuisées, et le
   quota journalier WARP déclenche des rotations en rafale.

La présente feature ne change ni le fournisseur, ni le compte, ni le modèle
payant : elle rend la jambe free honnête, économe en essais et observable.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Un client non-stream/SystemOne obtient une vraie réponse (Priority: P1)

Un client envoie une requête SystemOne non-stream avec
`proxy_mode=auto_egress`. Le proxy doit utiliser le pool WARP/NordVPN disponible
au lieu de refuser immédiatement en `503`.

**Why this priority**: 2 647 tentatives (34 % du trafic) échouent à cause de ce
seul chemin ; c'est le gain le plus immédiat.

**Independent Test**: appeler `/v1/systemone` avec `jev-1.13-free` et un pool
WARP actif ; la réponse ne doit jamais être le `503` « pool indisponible » et
doit être `200` ou un vrai `429` amont.

**Acceptance Scenarios**:

1. **Given** `proxy_mode=auto_egress` et un pool avec au moins un endpoint
   actif, **When** une requête SystemOne free arrive, **Then** elle est routée
   via le pool et ne renvoie pas `503 pool indisponible`.
2. **Given** le pool vide, **When** la requête arrive, **Then** le `503`
   fail-closed reste honnête et explicite.

---

### User Story 2 - Le pool ne gaspille plus d'essais sur des IP mortes (Priority: P1)

Un endpoint WARP IPv4 dont l'adresse NAT partagée est déjà à quota nul ne doit
plus être sélectionné en boucle. Un seul endpoint IPv4 reste actif comme canari.

**Why this priority**: 4 591 tentatives (33,7 %) ont visé 217 IP sans aucun
succès ; `104.28.230.202` a consommé 622 essais à elle seule.

**Independent Test**: rejouer une journée de trafic et vérifier que les
tentatives sur les IP mortes tombent sous 500, sans dégrader le succès IPv6.

**Acceptance Scenarios**:

1. **Given** les endpoints WARP IPv4 `.200/.202/.204` à quota nul, **When** le
   pool sélectionne, **Then** un seul canari IPv4 reste éligible et les autres
   sont désactivés.
2. **Given** un endpoint IPv4 canari qui prend un `429`, **When** le cycle de
   rotation s'exécute, **Then** il ne déclenche pas de rotation du conteneur.

---

### User Story 3 - Les stations NordVPN ne martèlent plus en `AUTH_FAILED` (Priority: P1)

Quand les credentials NordVPN sont invalides ou que la limite de sessions est
atteinte, les stations Docker sont parquées et n'émettent plus des milliers de
tentatives d'authentification.

**Why this priority**: 6 052 lignes `AUTH_FAILED` aujourd'hui ; la flotte
Docker est inutilisable et brouille les métriques.

**Independent Test**: appliquer `disabled_stations`, vérifier `AUTH_FAILED=0`,
puis valider une seule station avec des credentials sains avant réactivation.

**Acceptance Scenarios**:

1. **Given** des credentials invalides, **When** le proxy démarre, **Then** les
   stations sont parkées et aucune rafale `AUTH_FAILED` n'est émise.
2. **Given** des credentials validés sur une station, **When** l'opérateur
   réactive, **Then** la station obtient un egress public et redevient
   sélectionnable.

---

### User Story 4 - Les rotations suivent le quota, pas le bruit (Priority: P2)

Les rotations WARP ne se déclenchent que pour un quota journalier réel, avec un
budget horaire bas ; les 429 transitoires ne recréent pas les conteneurs.

**Why this priority**: 616 rotations réussies et 154 échouées aujourd'hui ;
chaque rotation crée un trou de service.

**Independent Test**: injecter des 429 transitoires et un quota journalier ;
seul le second doit déclencher une rotation, dans la limite du budget.

**Acceptance Scenarios**:

1. **Given** un `FreeUsageLimitError`, **When** la station est marquée, **Then**
   une rotation contrôlée est autorisée selon le budget.
2. **Given** un 429/timeout transitoire, **When** la station est marquée,
   **Then** aucune rotation de conteneur n'est lancée.

---

### User Story 5 - La latence d'attente de slot est bornée (Priority: P2)

Une requête ne doit plus perdre 3 s à attendre un slot par IP puis continuer
quand même.

**Why this priority**: 6 359 timeouts de slot aujourd'hui, p95 de succès 50 s.

**Independent Test**: saturer un couple (modèle, IP) et mesurer l'attente :
elle doit être ≤ 0,25 s avant le fail-open.

**Acceptance Scenarios**:

1. **Given** tous les slots occupés, **When** une requête arrive, **Then** elle
   attend au plus 0,25 s puis poursuit, sans bloquer 3 s.

---

### Edge Cases

- Pool vide : le `503` fail-closed reste, avec un message qui distingue
  « aucun endpoint » de « mode non supporté ».
- Tous les endpoints à quota journalier : une rotation contrôlée maximum par
  fenêtre, pas de gel total de 180 s.
- Endpoint IPv4 canari seul : il ne doit jamais être rotationné.
- Credentials NordVPN valides mais session limit : ne pas réactiver les six
  stations simultanément.
- Un client qui rejoue un `429` : le proxy ne doit pas amplifier la boucle
  (pas de faux `200`, pas de retry infini).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Le chemin free non-stream et SystemOne MUST accepter
  `proxy_mode=auto_egress` quand le pool contient au moins un endpoint actif.
- **FR-002**: Un `503 pool indisponible` MUST ne être émis que si aucun
  endpoint n'est utilisable, jamais à cause du seul nom du mode.
- **FR-003**: Les endpoints WARP IPv4 MUST être limités à un canari actif ;
  les autres MUST rester déclarés mais désactivés.
- **FR-004**: Un endpoint WARP IPv4 MUST ne jamais déclencher de rotation de
  conteneur.
- **FR-005**: Les stations NordVPN en `AUTH_FAILED` MUST être parkées avant de
  tenter une réactivation progressive.
- **FR-006**: Un `FreeUsageLimitError` MUST bloquer le couple `(modèle, IP)`
  jusqu'au reset fournisseur ou `Retry-After`, et MUST être distingué d'un 429
  transitoire.
- **FR-007**: La rotation WARP MUST être bornée à 3/heure et 10–15 min
  d'intervalle, et MUST ne se déclencher que sur quota journalier réel.
- **FR-008**: L'attente de slot MUST être ≤ 0,25 s avant le fail-open.
- **FR-009**: Le système MUST journaliser le résultat final des rotations
  (`result`, `finished_at`, `reason`) et permettre de distinguer une tentative
  d'une requête client.
- **FR-010**: Le taux d'échec client MUST rester mesurable sans parser les logs.

### Key Entities

- **Endpoint WARP** : label, conteneur, port, famille IPv4/IPv6, egress, bucket.
- **Station NordVPN** : slot Docker, état d'authentification, egress, santé.
- **Tentative free** : modèle, endpoint, statut, durée, cause, quota.
- **Job de rotation** : endpoint, déclencheur, résultat, raison, horodatage.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: `503 pool indisponible` = 0 sur 24 h après déploiement.
- **SC-002**: Le taux de succès des tentatives hors smoke passe de 70,7 % à
  ≥ 80 % en 72 h.
- **SC-003**: Les tentatives sur IP sans succès (≥10 essais) passent de 4 591 à
  < 500 par jour.
- **SC-004**: Les rotations WARP passent de 616 à < 150 par jour.
- **SC-005**: p95 de latence des succès passe de 50,4 s à < 35 s.
- **SC-006**: `AUTH_FAILED` passe de 6 052 à < 50 par jour.
- **SC-007**: Au moins 4/6 stations client-utilisables en permanence.

## Assumptions

- Le pool WARP reste la source free principale pendant la réparation NordVPN.
- Les credentials NordVPN existants peuvent être revalidés sans second compte.
- Le client SystemOne utilise bien `/v1/systemone` et un payload
  `{state, questions}`.
- Les tests live Docker restent opt-in (`-k "not docker"` pour la gate).
- Aucun fallback payant, résidentiel ou second compte n'est ajouté.
