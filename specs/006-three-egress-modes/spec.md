# Feature Specification: Trois modes d'egress — NordVPN, WARP, Direct

**Feature Branch**: `006-three-egress-modes`

**Created**: 2026-11-06

**Status**: Implemented — 2026-11-06

**Input**: Remplacer les modes VPN/SOCKS5/Direct par NordVPN/WARP/Direct et partager les paramètres de routage communs entre NordVPN et WARP.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Choisir explicitement NordVPN, WARP ou Direct (Priority: P1)

L'opérateur ouvre la page Free Models et voit exactement trois modes d'egress :
**NordVPN**, **WARP**, **Direct**. Le mode actif est mémorisé. Les panneaux
spécifiques au mode actif sont visibles ; les panneaux des autres modes ne
polluent pas l'écran.

**Why this priority**: Aujourd'hui les libellés « VPN (OpenVPN) » et
« SOCKS5 » ne disent pas quel fournisseur est utilisé. L'opérateur ne peut pas
savoir s'il pilote NordVPN, WARP ou le direct, ni quels réglages s'appliquent.

**Independent Test**: Depuis la GUI, cliquer successivement sur les trois
modes ; vérifier le libellé, la persistance après rechargement, et l'affichage
des seules sections pertinentes.

**Acceptance Scenarios**:

1. **Given** la page Free Models, **When** elle est chargée, **Then** les trois
   boutons affichent « NordVPN », « WARP », « Direct » et un seul est actif.
2. **Given** le mode NordVPN actif, **When** l'opérateur sélectionne WARP,
   **Then** le mode est persisté, la section NordVPN disparaît, la section WARP
   apparaît, sans redémarrage du proxy.
3. **Given** le mode WARP actif, **When** l'opérateur recharge la page,
   **Then** WARP est toujours actif.
4. **Given** le mode Direct actif, **When** la page est affichée, **Then** les
   réglages spécifiques NordVPN/WARP ne sont pas présentés comme actifs.

---

### User Story 2 - Partager les paramètres communs NordVPN/WARP (Priority: P1)

Les paramètres qui décrivent la politique de la jambe free — tentatives,
strict/fallback, action sur 429, TTL de bad-mark, fallback exceptionnel,
routage parallèle, retry, qualité et latence — sont regroupés dans une section
**commune** et s'appliquent de la même façon aux stations NordVPN et aux
stations WARP.

**Why this priority**: Ces paramètres sont actuellement visuellement rattachés
à « Parallel VPN stations » et donc perçus comme NordVPN-only. Pourtant ils
sont déjà stockés dans `ip_rotation` et doivent piloter les deux backends.

**Independent Test**: Modifier chaque paramètre commun en mode NordVPN puis en
mode WARP ; vérifier que la valeur persiste et que le backend la lit dans les
deux modes.

**Acceptance Scenarios**:

1. **Given** le mode NordVPN, **When** l'opérateur change `free_parallel` ou
   `on_429_action`, **Then** la valeur est persistée dans `ip_rotation` et
   reste identique après passage en WARP.
2. **Given** le mode WARP, **When** l'opérateur change un paramètre commun,
   **Then** le pool WARP l'applique à chaud sans redémarrage.
3. **Given** un paramètre commun modifié, **When** une requête part en
   NordVPN, **Then** la politique commune est appliquée ; **When** une requête
   part en WARP, **Then** la même politique est appliquée.
4. **Given** le mode Direct, **When** la section commune est affichée,
   **Then** son statut indique qu'elle ne s'applique pas au trafic direct.

---

### User Story 3 - Statut et capacité spécifiques au mode (Priority: P2)

Le tableau de bord affiche les stations et la capacité du mode actif :
stations Docker/gluetun pour NordVPN, endpoints WARP pour WARP, aucune station
pour Direct. Les diagnostics ne mélangent plus les deux familles.

**Why this priority**: En mode WARP, la capacité affichait « 0/1 stations » à
cause des stations Docker vides, ce qui faisait croire à une panne.

**Independent Test**: Basculer entre les modes et vérifier que la capacité, la
liste des stations et les compteurs correspondent au backend actif.

**Acceptance Scenarios**:

1. **Given** le mode WARP avec 7 endpoints, **When** la page est chargée,
   **Then** la capacité affiche 7/7 endpoints, pas 0/1 station Docker.
2. **Given** le mode NordVPN, **When** la page est chargée, **Then** la
   capacité affiche les stations Docker/gluetun réelles.
3. **Given** le mode Direct, **When** la page est chargée, **Then** aucune
   capacité de station n'est présentée comme active.

---

### User Story 4 - Paramètres spécifiques clairement isolés (Priority: P2)

Les paramètres réellement propres à un fournisseur restent dans leur section :
stack WireGuard/OpenVPN, sélection de serveur, identifiants, serveurs et
nombre de tunnels pour NordVPN ; liste des stations WARP, ports, test,
rotation et conteneurs managés pour WARP.

**Why this priority**: Le partage ne doit pas mélanger des réglages qui n'ont
aucun sens pour l'autre backend, ni faire croire qu'ils s'appliquent.

**Independent Test**: En mode WARP, vérifier que les réglages WireGuard/OpenVPN,
identifiants NordVPN et serveurs NordVPN ne sont pas visibles ou sont
explicitement inactifs. En mode NordVPN, vérifier que la liste WARP et ses
actions ne sont pas présentées comme actives.

**Acceptance Scenarios**:

1. **Given** le mode WARP, **When** la page est affichée, **Then** les
   réglages NordVPN-only sont absents ou marqués inactifs.
2. **Given** le mode NordVPN, **When** la page est affichée, **Then** les
   actions WARP (test/rotation des endpoints) sont absentes ou inactives.
3. **Given** un changement de mode, **When** la page se recharge, **Then**
   aucun réglage spécifique de l'ancien mode n'est appliqué silencieusement.

---

### Edge Cases

- Changement de mode pendant qu'une requête free est en vol : la requête garde
  son egress et son identité ; le nouveau mode ne s'applique qu'aux requêtes
  suivantes.
- WARP sélectionné sans endpoint activé : échec explicite, pas de bascule
  silencieuse vers Direct ou NordVPN.
- NordVPN sélectionné avec Docker arrêté : état `degraded` actionnable, pas de
  fallback WARP implicite.
- Direct sélectionné avec des paramètres communs non applicables : ils restent
  persistés mais ne sont pas présentés comme actifs.
- Ancienne configuration `proxy_mode: vpn|socks5|direct` : elle reste lisible ;
  les libellés changent sans migration de données.
- Paramètre commun modifié en WARP : la valeur doit survivre à un passage
  NordVPN → WARP → NordVPN.
- Deux endpoints WARP partagent `host:port` : la GUI doit les distinguer par
  label et le test doit cibler le bon endpoint.
- Un paramètre commun n'a pas le même objet selon le mode (ex. rotation
  Docker vs rotation conteneur WARP) : le contrat doit définir l'effet attendu
  dans chaque mode, jamais un no-op silencieux.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** La GUI DOIT présenter exactement trois modes d'egress :
  **NordVPN**, **WARP**, **Direct**.
- **FR-002** Les valeurs internes `vpn`, `socks5`, `direct` DOIVENT rester
  compatibles avec la configuration, l'API et les bases existantes ; seuls les
  libellés et la présentation changent.
- **FR-003** Le changement de mode DOIT être persisté et appliqué à chaud, sans
  redémarrage du proxy.
- **FR-004** Les réglages NordVPN-only (stack WireGuard/OpenVPN, sélection de
  serveur, identifiants, serveurs, nombre de tunnels Docker) DOIVENT être
  affichés uniquement en mode NordVPN et NE DOIVENT PAS être appliqués en WARP
  ou Direct.
- **FR-005** Les réglages WARP-only (liste des stations, ports, conteneurs
  managés, test et rotation des endpoints) DOIVENT être affichés uniquement en
  mode WARP et NE DOIVENT PAS être appliqués en NordVPN ou Direct.
- **FR-006** Une section **Routage free commun** DOIT regrouper au minimum :
  `strict_free`, `on_429_action`, `station_bad_ttl_s`,
  `auto_max_free_attempts`/`max_free_attempts`, `free_exception_fallback`,
  `free_parallel` (enabled/routing/mode/hedge_delay),
  `retry_until_success`, `quality_preselection` (hors `max_load`, qui reste
  NordVPN-only), `quality_rotation` et `latency_rotation`.
- **FR-007** Les paramètres communs DOIVENT être lus depuis `ip_rotation` par
  les deux backends NordVPN (`vpn`) et WARP (`socks5`), sans branche
  NordVPN-only.
- **FR-008** `free_parallel` DOIT piloter la sélection des stations NordVPN et
  des endpoints WARP : round-robin/strict/load-balance/hedge, avec les mêmes
  valeurs et la même sémantique de failover.
- **FR-009** `quality_rotation` DOIT pouvoir tourner une station NordVPN et un
  endpoint WARP managé ; un endpoint WARP non managé est ignoré explicitement.
- **FR-010** `latency_rotation` DOIT filtrer les stations NordVPN et les
  endpoints WARP de la même façon, avec le même plancher de secours.
- **FR-011** `post_rotation_probe` DOIT avoir une portée explicite : WARP-only
  ou commun aux deux rotations ; le libellé et le contrat doivent refléter
  cette portée sans ambiguïté.
- **FR-012** En mode Direct, les paramètres communs restent persistés mais la
  GUI DOIT indiquer qu'ils ne s'appliquent pas au trafic direct.
- **FR-013** Les titres, aides et libellés DOIVENT utiliser « NordVPN »,
  « WARP » et « Direct » ; le mot « SOCKS5 » ne doit plus servir de nom de mode
  visible.
- **FR-014** Le statut et la capacité DOIVENT être calculés sur le backend du
  mode actif : stations Docker/gluetun pour NordVPN, endpoints WARP pour WARP,
  aucun pour Direct.
- **FR-015** Un changement de mode NE DOIT PAS réinitialiser les paramètres
  communs ni les réglages de l'autre mode.
- **FR-016** Les anciennes configurations DOIVENT rester lisibles ; aucune
  migration destructive de `proxy_mode` n'est autorisée.
- **FR-017** Chaque application d'un paramètre commun DOIT être traçable :
  mode, clé, valeur effective et backend consommateur.
- **FR-018** Les tests DOIVENT couvrir les trois modes, le partage des
  paramètres, l'isolation des réglages spécifiques, la capacité et la
  persistance.
- **FR-019** En mode SOCKS5, la capacité et le statut DOIVENT compter les
  endpoints WARP réels et le quota par modèle, pas les stations Docker vides.
- **FR-020** La langue choisie DOIT persister côté navigateur et côté serveur,
  et être reprise au démarrage/redémarrage même si le stockage local est absent
  (autre origine `localhost`/`127.0.0.1`, stockage vidé).
- **FR-021** Le sélecteur « Essais gratuits / requête » DOIT toujours afficher
  une valeur sélectionnée : `Auto (selon stations)` ou une valeur manuelle
  effective 1–3, y compris si `config.yaml` contient une ancienne valeur hors
  bornes.
- **FR-022** En mode manuel, `max_free_attempts` NE DOIT PAS être écrasé par le
  budget effectif (élargi par `strict_free`) au démarrage ou lors d'un
  changement de `station_count` ; la valeur opérateur 1–3 doit persister.

### Key Entities

- **EgressMode** : `vpn` (affiché NordVPN), `socks5` (affiché WARP),
  `direct` (affiché Direct), avec libellé, backend et capacités.
- **CommonRoutingPolicy** : paramètres `ip_rotation` partagés par les deux
  backends, avec pour chaque clé les modes où elle s'applique et son effet.
- **NordVpnSettings** : stack, sélection de serveur, identifiants, serveurs,
  nombre de tunnels Docker.
- **WarpSettings** : liste d'endpoints, ports, conteneurs, enabled/managed,
  rotation et test.
- **ModeCapability** : matrice mode × paramètre (applicable, ignoré,
  spécifique) utilisée par la GUI et l'audit.
- **StatusSnapshot** : capacité et stations du mode actif, sans mélange
  Docker/WARP.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** La page Free Models affiche exactement trois modes libellés
  « NordVPN », « WARP », « Direct » ; aucun libellé « VPN (OpenVPN) » ou
  « SOCKS5 » ne subsiste comme nom de mode.
- **SC-002** Un cycle NordVPN → WARP → NordVPN conserve 100 % des paramètres
  communs et des réglages spécifiques.
- **SC-003** Une modification d'un paramètre commun est effective dans les
  deux modes sans redémarrage ; preuve par test backend et vérification GUI.
- **SC-004** En mode WARP, la capacité affiche les endpoints réels (ex. 7/7),
  jamais « 0/1 stations Docker ».
- **SC-005** Les réglages NordVPN-only ne sont ni visibles ni appliqués en
  WARP ; les réglages WARP-only ne sont ni visibles ni appliqués en NordVPN.
- **SC-006** Les suites existantes restent vertes et les nouveaux tests
  couvrent les trois modes et le partage des paramètres.
- **SC-007** En mode WARP, `/health` et `/api/vpn-status` exposent la capacité
  réelle (7/7) et le quota par modèle, sans faux « 0/1 ».
- **SC-008** La langue française reste active après rechargement, vidage du
  localStorage et redémarrage du proxy.
- **SC-009** Le paramètre d'essais gratuits est lisible, sélectionné et
  effectif : `auto` → 7 essais avec 7 endpoints ; manuel 1–3.
- **SC-010** Un mode manuel `3` reste `3` après redémarrage, même si
  `strict_free` porte le budget effectif à 7.

## Assumptions

- « NordVPN » correspond au backend Docker/gluetun existant (`proxy_mode=vpn`).
- « WARP » correspond aux endpoints SOCKS5/WARP existants
  (`proxy_mode=socks5`), managés ou non.
- « Direct » correspond à `proxy_mode=direct`.
- Les paramètres communs vivent déjà dans `ip_rotation` et n'ont pas besoin
  d'un nouveau stockage.
- Les valeurs internes de `proxy_mode` ne changent pas ; la migration est
  présentationnelle et comportementale, pas destructive.
- La portée exacte de `post_rotation_probe` (WARP-only ou commune) sera
  tranchée en Phase 0 ; par défaut, elle reste WARP-only car le probe est déjà
  branché sur la rotation WARP.
- Aucun changement de l'identité de station, des profils vérifiés ou du wire
  officiel n'est inclus dans cette feature.
