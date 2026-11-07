# Feature Specification: WARP primaire + replis d'egress free

**Feature Branch**: `007-warp-egress-fallback`

**Created**: 2026-11-07

**Status**: Draft

**Input**: Plan d'amélioration WARP du 2026-11-06
(`PLAN_ALTERNATIVE_WARP_2026-11-06.md`) — garder WARP comme source
principale, ajouter des replis gratuits ordonnés, valider la granularité du
quota, et ne jamais basculer silencieusement vers le payant ou le direct.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - WARP reste primaire malgré les blocs épuisés (Priority: P1)

L'opérateur utilise WARP comme source d'egress principale. Quand un bloc
`/64` est épuisé, le système le parke jusqu'à son reset ou tourne vers un
autre `/64` WARP ; WARP reste le chemin majoritaire et les requêtes free
continuent d'obtenir un `200`.

**Why this priority**: WARP est gratuit, sans cap de données, et déjà en
production. Le casser ou le remplacer par un fallback au premier `429`
réduirait la capacité et la prévisibilité.

**Independent Test**: injecter un `429 FreeUsageLimitError` sur une station,
vérifier que le bloc est parké ou tourné, qu'aucune requête n'est servie avant
`reset_at`, et qu'une requête suivante obtient `200` sur un bloc frais.

**Acceptance Scenarios**:

1. **Given** un bloc WARP épuisé avec un `reset_at` proche, **When** une
   requête arrive, **Then** le système parke le bloc et sert via une autre
   station WARP ou un fallback autorisé, sans utiliser le bloc avant reset.
2. **Given** un bloc WARP épuisé avec un `reset_at` lointain et aucun autre
   bloc WARP disponible, **When** la rotation est possible, **Then** le
   système recrée le conteneur et obtient un `/64` distinct.
3. **Given** deux endpoints WARP qui retombent sur le même `/64`, **When**
   le doublon est détecté, **Then** un seul est utilisé et l'autre tourne.
4. **Given** une rotation WARP qui échoue ou reste throttlée, **When** le
   seuil est atteint, **Then** la station passe au fallback sans interrompre
   les autres stations WARP.

---

### User Story 2 - Repli gratuit automatique puis retour à WARP (Priority: P1)

Quand WARP est épuisé ou indisponible au-delà des rotations, une à deux
stations basculent sur un repli **gratuit** dans un ordre configuré
(MicroWARP/usque, IPv6 box/ISP, Oracle Always Free, Proton Free), puis
reviennent à WARP après deux succès consécutifs.

**Why this priority**: WARP ne fournit que 4–5 blocs utiles. Sans repli de
capacité, le pool reste sous-dimensionné pour 3 000–10 000 requêtes/jour.

**Independent Test**: désactiver WARP ou épuiser tous ses blocs, vérifier
qu'une station de fallback sert 10 requêtes sur 10, que WARP reste majoritaire
pour les autres, puis que la station revient à WARP après deux probes `200`.

**Acceptance Scenarios**:

1. **Given** WARP épuisé et un fallback gratuit configuré, **When** le
   déclencheur de bascule est atteint, **Then** au plus deux stations passent
   sur le fallback et servent des `200`.
2. **Given** une station sur fallback, **When** WARP réussit deux probes
   consécutifs, **Then** la station revient à WARP après le cooldown.
3. **Given** tous les replis gratuits indisponibles, **When** aucune capacité
   n'est disponible, **Then** le système expose `degraded` avec la raison et
   ne bascule pas silencieusement vers le payant ou le direct.
4. **Given** un `403 FreeTierError`, **When** il est reçu, **Then** aucune
   rotation ni bascule de fallback n'est déclenchée ; l'erreur est classée
   comme défaut de forme/auth/geo/version.

---

### User Story 3 - Valider la granularité réelle du quota (Priority: P2)

L'opérateur peut exécuter un protocole de validation qui détermine si le
quota est keyé par adresse, par `/64`, par ASN/compte/global, ou par modèle,
avec un contrôle `403` vs `429` et un contrôle direct vs WARP.

**Why this priority**: tout le plan de capacité (box/ISP, Oracle, VPS) dépend
de cette réponse. La financer sans mesure serait une erreur.

**Independent Test**: exécuter le protocole sur un `/64` avec deux adresses,
puis deux `/64` distincts, puis un contrôle `403`, puis répéter sur deux
modèles free ; produire `docs/QUOTA_GRANULARITY_2026-11-07.md` avec les
résultats bruts et un go/no-go.

**Acceptance Scenarios**:

1. **Given** une adresse `A` épuisée, **When** une adresse `B` du même `/64`
   est testée, **Then** le document note `429` (bucket `/64`) ou `200`
   (bucket adresse) avec la preuve.
2. **Given** un `/64` épuisé, **When** un `/64` distinct est testé, **Then**
   le document note si le quota est par bloc ou agrégé.
3. **Given** un `403` de forme, **When** il est reçu, **Then** il n'est jamais
   compté comme un bloc épuisé.
4. **Given** deux modèles free, **When** le protocole est répété, **Then** le
   document indique si l'axe est le même pour les deux.

---

### User Story 4 - Voir et piloter provider, bloc et quota (Priority: P2)

L'opérateur voit dans le dashboard le provider actif, le bloc, le dernier
`429`, le `reset_at`, l'état de santé et le fallback en cours. Chaque option
affichée a un effet runtime vérifiable.

**Why this priority**: sans visibilité, une bascule de fallback ou un bloc
parké est indiscernable d'une panne.

**Independent Test**: configurer un fallback, provoquer une bascule, vérifier
que le dashboard affiche provider/bloc/état, que le toggle a un effet et que
la valeur effective est celle utilisée par le runtime.

**Acceptance Scenarios**:

1. **Given** une station sur fallback, **When** le dashboard est ouvert,
   **Then** le provider, le bloc et la raison de bascule sont visibles.
2. **Given** un endpoint avec un bloc épuisé, **When** l'état est affiché,
   **Then** `reset_at` et le dernier `429` sont visibles.
3. **Given** une option GUI sans consommateur, **When** la spec est validée,
   **Then** l'option est retirée ou marquée « affichage seul ».

---

### User Story 5 - Gateway IPv6 VPS optionnel (Priority: P3 — différé, payant)

Si les replis gratuits ne suffisent pas, l'opérateur peut activer un gateway
IPv6 sur un VPS avec un `/48` routé. Le gateway alloue un `/64` par station,
tourne en quelques secondes, persiste les allocations et reste en localhost
avec token.

**Why this priority**: c'est le seul repli de capacité à grande échelle, mais
il est payant et optionnel ; il ne doit jamais être activé par défaut.

**Independent Test**: lancer le gateway, effectuer trois rotations, vérifier
trois `/64` distincts, trois `200`, aucune collision, et la persistance après
redémarrage.

**Acceptance Scenarios**:

1. **Given** un gateway `/48` actif et une station VPS, **When** une rotation
   est demandée, **Then** un `/64` frais est alloué et le port est conservé.
2. **Given** deux stations VPS, **When** elles tournent, **Then** elles
   n'obtiennent jamais le même `/64`.
3. **Given** un gateway down, **When** une station tente de servir, **Then**
   elle est bad-markée et WARP reprend le relais ; aucun direct silencieux.

---

### Edge Cases

- WARP retourne le même `/64` plusieurs fois de suite.
- Le throttle d'enregistrement WARP dépasse 10 min.
- Un conteneur WARP est down plus de 2 min.
- Un `403 FreeTierError` arrive sur une IP fraîche.
- Un `429 RateLimitError` court arrive au lieu d'un quota journalier.
- `Retry-After` annonce plusieurs heures et la recovery réelle est plus tôt.
- Tous les blocs WARP et tous les fallbacks sont épuisés.
- Deux providers retournent le même bloc.
- Un endpoint `ipv6: true` sort en IPv4.
- Le fallback payant est désactivé et la capacité gratuite est insuffisante.
- Le préfixe IPv6 de la box change après reboot.
- Un compte/clé unique est la vraie limite, pas le bloc.

## Scope — Free only (décision opérateur, 2026-11-07)

**Principe** : on implémente uniquement ce qui est **gratuit** et **à notre
portée**. Aucun achat, aucun abonnement, aucun second compte payant, aucune
attente comme solution.

### In scope

- Optimisations et fiabilisation WARP (park/reset, rotation, dédup, probes).
- Abstraction provider, taxonomie 403/429, observabilité, tests.
- Repli de continuité **MicroWARP/usque** (gratuit, même pool).
- IPv6 de la box/ISP **si la box actuelle le délègue déjà** (pas de
  remplacement matériel).
- **Oracle Always Free** et **Proton Free** si un compte gratuit est
  disponible ou créable sans achat.
- Windscribe/Privado Free (10 Go) en urgence, sans achat.
- Validation Phase 0 sur l'infrastructure existante.
- GUI, métriques, runbooks et preuves live.

### Out of scope (cette itération)

- VPS payant `/48` (BuyVM, Linode, Hetzner, Vultr, Contabo).
- VPN payant (Mullvad, PIA, Surfshark, AirVPN, IVPN).
- Forfaits 5G payants (Free 5G+, Free Box 5G).
- Abonnement OpenCode Go.
- Empilement de comptes/clés.
- Proxies publics, Tor, VPN Gate.
- Remplacement de la box/routeur pour obtenir un `/56`.

### Conditionnel

- Oracle/Proton : uniquement si le compte gratuit existe déjà ou peut être
  créé sans carte payante ni abonnement.
- IPv6 box/ISP : uniquement avec la box actuelle ; si elle ne délègue qu'un
  `/64`, on l'utilise tel quel (1 bucket), sans achat.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** WARP DOIT rester le provider par défaut et majoritaire ; les
  replis ne doivent pas absorber plus de deux stations à la fois.
- **FR-002** L'ordre des replis DOIT être configurable et **gratuit d'abord** :
  WARP, client WARP alternatif, IPv6 box/ISP, Oracle Always Free, Proton Free,
  puis options payantes explicitement activées.
- **FR-003** Une bascule de fallback DOIT être déclenchée par : quota après
  épuisement des rotations WARP, échec/throttle WARP persistant > 10 min,
  conteneur down > 2 min, bloc qui revient épuisé plusieurs fois, ou override
  manuel.
- **FR-004** Un `403 FreeTierError` NE DOIT JAMAIS déclencher de rotation,
  marquer un bloc épuisé ou changer de provider ; il doit être classé comme
  défaut de forme, auth, geo ou version.
- **FR-005** Un `429` DOIT être classé en quota journalier
  (`FreeUsageLimitError`), rate-limit court (`RateLimitError`), ou
  compte/global ; chaque classe a un traitement distinct.
- **FR-006** Par défaut, aucune IP NE DOIT être utilisée avant son `reset_at`
  fournisseur ; le probe anticipé (`ledger_max_block_s`) reste une option
  explicite, désactivée par défaut pour les nouveaux providers.
- **FR-007** Si le reset est proche (< 30 min) et qu'un autre bloc est
  disponible, le système DOIT parker le bloc au lieu de recréer un conteneur ;
  si le reset est lointain et la capacité manque, il DOIT tourner ou basculer.
- **FR-008** L'egress réel DOIT être mesuré et dédupliqué par famille
  (`/64` IPv6, `/32` IPv4) ; deux stations ne DOIVENT JAMAIS partager un bloc.
- **FR-009** Une station sur fallback DOIT revenir à WARP après **deux succès
  consécutifs**, avec un cooldown anti-flapping de 15 min.
- **FR-010** Chaque endpoint DOIT porter un `provider` et un `label` distincts ;
  un bloc fallback NE DOIT JAMAIS être réutilisé pour une station WARP, et
  inversement.
- **FR-011** Le dashboard DOIT afficher provider, bloc, dernier `429`,
  `reset_at`, état de santé et fallback en cours pour chaque endpoint.
- **FR-012** Le système DOIT être fail-closed : si le pool est vide, il expose
  `degraded` et sa raison ; il NE DOIT PAS sortir en direct sans configuration
  explicite.
- **FR-013** Le protocole Phase 0 DOIT tester adresse vs `/64` vs
  ASN/compte/global, par modèle, avec un contrôle `403` et un contrôle
  direct vs WARP, et produire un document go/no-go.
- **FR-014** Un client WARP alternatif (MicroWARP/usque/warp-minimal) DOIT
  pouvoir servir de repli de continuité sans promettre de quota
  supplémentaire.
- **FR-015** Proton Free DOIT être supporté comme repli de capacité pour **une
  seule station**, avec data illimitée et un seul appareil, sous réserve d'un
  compte gratuit disponible sans achat.
- **FR-016 (différé)** Le gateway IPv6 VPS optionnel DOIT allouer un `/64` par
  station, lier une source stable, persister les allocations et exposer
  `/health`, `/allocate`, `/rotate`, `/release`. **Hors périmètre de cette
  itération (payant).**
- **FR-017 (différé)** Une rotation VPS DOIT prendre moins de 10 s et ne jamais
  changer le port SOCKS5 de la station. **Hors périmètre de cette itération
  (payant).**
- **FR-018** Aucun proxy public, Tor ou VPN Gate NE DOIT être utilisé pour des
  credentials ou du trafic API.
- **FR-019** L'empilement de comptes/clés NE DOIT PAS être une stratégie par
  défaut ; l'axe compte/clé doit être prouvé avant toute implémentation.
- **FR-020** Toute option GUI DOIT avoir un contrat
  `option → clé config → consommateur runtime → effet observable → test`.
- **FR-021** Les seuils (cooldowns, probes, max fallback, reset) DOIVENT vivre
  dans `config.yaml`, pas en dur dans le code.
- **FR-022** Chaque exigence DOIT avoir soit un test automatisé, soit une
  preuve live référencée.
- **FR-023** La jambe free NE DOIT JAMAIS dépendre de la jambe payante ; toute
  option payante est opt-in, tracée et réversible.
- **FR-024** Les métriques DOIVENT exposer : blocs distincts, blocs épuisés,
  `429` par modèle/axe, `403` gate, rotations, doublons, temps de reset.
- **FR-025** Un changement de provider NE DOIT PAS interrompre une requête en
  vol ; le drain se fait avant la bascule.
- **FR-026** Aucun composant payant NE DOIT être ajouté dans cette itération ;
  les options payantes restent documentées, désactivées par défaut et hors
  périmètre d'implémentation.

### Key Entities

- **EgressEndpoint** : station logique (label, provider, host, port, famille,
  managed, egress_ip, block, reset_at, santé, état).
- **EgressBlock** : bloc de quota (`/64` ou `/32`), provider, premier/dernier
  usage, `reset_at`, épuisé, source de la preuve.
- **FallbackPolicy** : ordre des providers, max stations en fallback, seuils
  de déclenchement, probes, succès requis, cooldown.
- **QuotaObservation** : modèle, bloc, statut, `Retry-After`, `reset_at`,
  horodatage, classe d'erreur.
- **ProviderHealth** : provider, état, dernier probe, dernier succès, dernier
  échec, raison.
- **GatewayAllocation** (optionnel) : bloc, port, source IPv6, station,
  état, date d'allocation.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** Sur 24 h de trafic, WARP reste le provider de **plus de 80 %**
  des requêtes free.
- **SC-002** Sur 24 h, la disponibilité client est **> 99 %** et les `429`
  représentent **< 5 %** des requêtes.
- **SC-003** Après épuisement de WARP, une station de fallback sert
  **10 requêtes sur 10** en `200`.
- **SC-004** Une station revient à WARP après **deux probes `200` consécutifs**,
  dans les deux cycles de probe suivant la recovery.
- **SC-005** Aucun doublon de bloc entre stations ou providers : **0** sur
  une fenêtre de 24 h.
- **SC-006** Un `403` ne provoque **aucune** rotation ni marquage de bloc :
  **0** faux positif sur le jeu de tests.
- **SC-007** La Phase 0 produit un document go/no-go pour **au moins deux
  modèles free**, avec preuves brutes `403`/`429`.
- **SC-008 (différé)** Une rotation VPS alloue un `/64` distinct en **< 10 s**,
  trois rotations donnent trois blocs et trois `200`. Hors périmètre de cette
  itération (payant).
- **SC-009** Le dashboard affiche provider, bloc et état pour **100 %** des
  endpoints actifs.
- **SC-010** Aucune bascule silencieuse vers le direct ou le payant :
  **0** occurrence sur la période de soak.

## Assumptions

- WARP free reste disponible et fournit environ **4–5 blocs utiles** ; ce
  chiffre doit être re-mesuré, pas supposé.
- La granularité du quota n'est pas confirmée : la Phase 0 est un prérequis
  au choix d'un repli de capacité.
- Les replis gratuits (box/ISP, Oracle, Proton) sont disponibles ou peuvent
  être testés ; chez Red by SFR, la box fournie ne délègue qu'un `/64`.
- Le VPS `/48` est optionnel, payant et désactivé par défaut ; il ne fait pas
  partie du MVP gratuit.
- Les CGU OpenCode/Cloudflare/VPN doivent être vérifiées avant déploiement ;
  ce plan est technique, pas juridique.
- Le plan source `PLAN_ALTERNATIVE_WARP_2026-11-06.md` reste la référence
  détaillée pour les chiffres, les dépôts analysés et les coûts.
