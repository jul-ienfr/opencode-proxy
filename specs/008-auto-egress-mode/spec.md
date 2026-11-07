# Feature Specification: Auto-Egress — mode multi-provider automatique

**Feature Branch**: `008-auto-egress-mode`

**Created**: 2026-11-07

**Status**: Draft

**Input**: Demande opérateur : ajouter un mode qui utilise en parallèle tout ce
qui est disponible et utilisable — NordVPN, WARP, proxys SOCKS5/HTTPS — avec
sélection automatique, sans achat supplémentaire et sans bascule silencieuse
vers le direct.

## Nom retenu

**Auto-Egress** (interne : `auto_egress`, libellé UI : « Auto-Egress »).

Pourquoi ce nom :

- **Auto** : le mode choisit seul le meilleur egress disponible, sans que
  l'opérateur choisisse NordVPN, WARP ou un proxy.
- **Egress** : le mot décrit exactement ce qui est mutualisé (les sorties
  réseau), pas le transport ni le compte.
- « Hybride » a été écarté car il évoque le split-tunneling ; « Multi-Egress »
  est descriptif mais moins clair sur l'automatisation ; « Pool » entre en
  collision avec le pool d'IP existant.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Activer Auto-Egress depuis le GUI (Priority: P1)

L'opérateur voit un quatrième mode à côté de NordVPN, WARP et Direct. Il
sélectionne **Auto-Egress**, le mode persiste après redémarrage, et le proxy
utilise automatiquement tous les providers activés.

**Why this priority**: sans un mode visible et persistant, la fonctionnalité
n'existe pas pour l'opérateur.

**Independent Test**: sélectionner Auto-Egress, recharger le dashboard,
redémarrer le proxy, vérifier que le mode est toujours actif et que les
providers sont listés.

**Acceptance Scenarios**:

1. **Given** les modes NordVPN/WARP/Direct existants, **When** le GUI est
   ouvert, **Then** Auto-Egress est visible et sélectionnable.
2. **Given** Auto-Egress sélectionné, **When** le proxy redémarre, **Then** le
   mode est restauré et les providers activés sont utilisés.
3. **Given** Auto-Egress actif, **When** un provider est désactivé dans le
   GUI, **Then** il sort du pool sans interrompre les requêtes en vol.

---

### User Story 2 - Utiliser tous les providers en parallèle (Priority: P1)

Le proxy répartit les requêtes et les tentatives de secours sur tous les
egress disponibles : stations NordVPN, endpoints WARP, proxys SOCKS5/HTTPS.
Chaque requête utilise **un** egress ; le parallélisme est inter-requêtes et
par hedge, pas un bonding multi-lien d'une même requête.

**Why this priority**: c'est le cœur du mode : ne pas dépendre d'un seul
provider et utiliser la capacité disponible.

**Independent Test**: activer NordVPN + WARP + un proxy, envoyer 20 requêtes
free, vérifier qu'au moins deux providers ont servi et que le taux de succès
est > 99 %.

**Acceptance Scenarios**:

1. **Given** NordVPN, WARP et un proxy sains, **When** 20 requêtes arrivent,
   **Then** elles sont réparties sur au moins deux providers.
2. **Given** un provider lent ou proche du quota, **When** la sélection
   s'exécute, **Then** il reçoit moins de trafic sans être totalement exclu
   tant qu'il reste utilisable.
3. **Given** une requête échoue sur un provider, **When** le retry/hedge
   s'exécute, **Then** un autre provider est tenté sans bloquer la requête.
4. **Given** une même conversation, **When** aucun échec n'intervient,
   **Then** elle reste sticky sur le même egress pour préserver le cache.

---

### User Story 3 - Isoler les pannes et les quotas par provider (Priority: P1)

Un `403`, un `429`, un provider down ou un quota épuisé ne doit pas casser les
autres providers. Chaque provider garde sa propre santé, son quota et son
cycle de rotation.

**Why this priority**: un mode multi-provider qui tombe avec un provider est
pire que le mode actuel.

**Independent Test**: injecter un `403` sur NordVPN, un `429` sur WARP et un
timeout sur le proxy ; vérifier que les requêtes continuent via les providers
restants et qu'aucun `403` ne déclenche de rotation.

**Acceptance Scenarios**:

1. **Given** NordVPN down, **When** Auto-Egress sert, **Then** WARP et le proxy
   continuent et la disponibilité reste > 99 %.
2. **Given** un `403` sur un provider, **When** il est classé, **Then** aucune
   rotation ni pénalité de bloc n'est appliquée.
3. **Given** un `429` quota sur WARP, **When** le bloc est parké, **Then**
   NordVPN/proxy peuvent servir sans réutiliser le bloc WARP.
4. **Given** tous les providers épuisés, **When** aucune capacité ne reste,
   **Then** le système expose `degraded` avec la raison, sans direct silencieux.

---

### User Story 4 - Régler les poids et la diversité (Priority: P2)

L'opérateur peut activer/désactiver chaque provider, régler son poids, le
nombre de requêtes concurrentes et la politique de diversité. Le défaut reste
**free-first** : WARP d'abord, puis NordVPN/proxys, sans exclure les autres.

**Why this priority**: le mode doit rester prévisible et ne pas vider un
provider payant ou un quota free en quelques minutes.

**Independent Test**: régler WARP à 60 %, NordVPN à 30 %, proxy à 10 %,
envoyer 100 requêtes, vérifier la répartition approximative et les caps.

**Acceptance Scenarios**:

1. **Given** des poids configurés, **When** 100 requêtes passent, **Then** la
   répartition respecte les poids à ±10 points.
2. **Given** un provider désactivé, **When** la sélection s'exécute, **Then**
   il ne reçoit aucune requête.
3. **Given** un cap de concurrence par provider, **When** la limite est
   atteinte, **Then** les requêtes sont routées vers les autres providers.
4. **Given** `allow_direct=false` (défaut), **When** tous les providers sont
   indisponibles, **Then** le mode échoue explicitement, pas en direct.

---

### User Story 5 - Voir qui sert quoi (Priority: P2)

Le dashboard affiche les providers actifs, leur santé, leur quota, leur poids,
le dernier `403`/`429`, et la répartition des requêtes par provider.

**Why this priority**: sans attribution, un mode multi-provider est une boîte
noire impossible à déboguer.

**Independent Test**: envoyer 20 requêtes, vérifier que le dashboard montre la
répartition et le provider de chaque requête récente.

**Acceptance Scenarios**:

1. **Given** Auto-Egress actif, **When** le dashboard est ouvert, **Then** les
   providers actifs, leur état et leur poids sont visibles.
2. **Given** une requête servie, **When** l'historique est consulté, **Then**
   le provider et le bloc utilisés sont visibles.
3. **Given** un provider en `degraded`, **When** le dashboard est ouvert,
   **Then** la raison et le dernier échec sont affichés.

---

### Edge Cases

- NordVPN est configuré mais son abonnement/session est expiré.
- WARP est épuisé mais un proxy statique reste disponible.
- Deux providers sortent par la même IP/bloc (dédoublonnage physique).
- Un provider renvoie un `403` de forme, pas un quota.
- Un provider renvoie un `429` court (rate-limit) au lieu d'un quota.
- Un provider est down au démarrage.
- Tous les providers sont down : `degraded`, pas de direct.
- Une conversation change de provider en cours de route (cache perdu).
- Le cap de concurrence est atteint sur tous les providers.
- Le mode est changé pendant une requête en vol.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** Le mode `auto_egress` DOIT être visible dans le GUI à côté de
  NordVPN, WARP et Direct, et persister après redémarrage.
- **FR-002** Auto-Egress DOIT inclure tous les providers **activés et
  configurés** : stations NordVPN, endpoints WARP, proxys SOCKS5/HTTPS.
- **FR-003** Chaque requête DOIT utiliser **un seul** egress ; le parallélisme
  est inter-requêtes et par hedge, jamais un bonding multi-lien d'une même
  requête.
- **FR-004** La sélection DOIT être automatique, basée sur la santé, le quota,
  la latence, les succès récents et le poids du provider.
- **FR-005** Le mode DOIT appliquer un plafond de concurrence par provider pour
  éviter de vider un seul egress.
- **FR-006** Chaque provider DOIT garder son propre ledger/quota : IP NordVPN,
  bloc WARP `/64`, IP proxy. Aucun bloc ne DOIT être partagé entre deux
  stations.
- **FR-007** Un `403 FreeTierError` NE DOIT PAS déclencher de rotation, de
  pénalité de bloc ou de changement de provider.
- **FR-008** Un `429` DOIT être classé quota/rate-limit/account ; un quota
  parke ou tourne **dans le provider concerné**, sans condamner les autres.
- **FR-009** Un provider down ou en échec DOIT être retiré de la sélection
  sans interrompre les autres.
- **FR-010** Le mode DOIT être **fail-closed** : si aucun provider n'est
  disponible, il expose `degraded` et sa raison ; il NE DOIT PAS sortir en
  direct sans `allow_direct=true`.
- **FR-011** Le défaut DOIT être **free-first** : WARP en premier, puis
  NordVPN/proxys, sans exclure les autres providers tant qu'ils sont sains.
- **FR-012** Aucun nouvel abonnement payant NE DOIT être créé pour ce mode ;
  seuls les providers déjà configurés sont utilisés.
- **FR-013** Un changement de mode DOIT drainer les requêtes en vol avant de
  basculer ; aucune requête ne doit être interrompue.
- **FR-014** Une conversation DOIT rester sticky sur son egress tant qu'il est
  sain ; un changement ne se produit qu'après échec ou quota.
- **FR-015** Le dashboard DOIT afficher les providers actifs, leur santé, leur
  poids, leur quota, le dernier `403`/`429` et la répartition des requêtes.
- **FR-016** Les métriques DOIVENT exposer : requêtes par provider, taux de
  succès, `429`/`403`, latence, switches, hedges gagnés.
- **FR-017** La configuration DOIT vivre dans `config.yaml` :
  `auto_egress.providers`, `weights`, `max_concurrent`, `hedge_delay_ms`,
  `allow_direct`, `diversity`, `sticky`.
- **FR-018** Chaque option GUI DOIT avoir un contrat
  `option → clé → consommateur → effet → test`.
- **FR-019** Auto-Egress DOIT réutiliser l'abstraction `EgressProvider` de la
  feature 007 ; aucun second moteur de rotation.
- **FR-020** Les providers payants déjà configurés (NordVPN) peuvent servir ;
  les providers payants non configurés ne doivent pas être ajoutés.
- **FR-021** Un provider qui partage l'egress physique d'un autre (même IP ou
  même `/64`) DOIT être dédupliqué dans la sélection.
- **FR-022** Un provider `403`/auth/version NE DOIT PAS être compté comme un
  bloc épuisé.
- **FR-023** Le mode DOIT exposer `degraded` si moins de deux providers sont
  sains, avec la liste des providers indisponibles.
- **FR-024** Les tests DOIVENT couvrir : sélection multi-provider, provider
  down, 403, 429 quota, 429 court, direct fail-closed, caps de concurrence,
  sticky, changement de mode sans interruption.

### Key Entities

- **AutoEgressConfig** : providers activés, poids, caps, hedge, direct,
  diversité, sticky.
- **UnifiedEndpoint** : endpoint d'un provider (NordVPN station, WARP endpoint,
  proxy) avec provider, bloc, santé, quota, poids.
- **ProviderState** : santé, dernier probe, dernier succès/échec, quota,
  disponibilité.
- **SelectionDecision** : requête, provider choisi, score, raison, alternatives
  tentées.
- **RequestAttribution** : requête, provider, endpoint, bloc, statut, latence.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** Auto-Egress est visible, sélectionnable et persiste après
  redémarrage : **100 %** des redémarrages testés.
- **SC-002** Avec NordVPN + WARP + proxy sains, 20 requêtes utilisent **au
  moins deux providers** et réussissent à **> 99 %**.
- **SC-003** Avec un provider down, la disponibilité reste **> 99 %** via les
  autres.
- **SC-004** Un `403` provoque **0** rotation et **0** changement de provider.
- **SC-005** Un `429` quota parke/tourne uniquement le provider concerné :
  **0** bloc partagé entre providers.
- **SC-006** `allow_direct=false` → **0** requête directe silencieuse.
- **SC-007** Le dashboard affiche la répartition par provider pour **100 %**
  des requêtes récentes.
- **SC-008** Un changement de mode n'interrompt **aucune** requête en vol
  (0 erreur).
- **SC-009** Les poids configurés sont respectés à **±10 points** sur 100
  requêtes.
- **SC-010** Les caps de concurrence par provider sont respectés à **100 %**.

## Assumptions

- NordVPN, WARP et les proxys sont déjà configurés et utilisables ; le mode
  n'achète rien.
- WARP reste le provider gratuit prioritaire ; NordVPN/proxys sont utilisés
  s'ils sont déjà disponibles.
- Une requête HTTP ne peut pas être "bondée" sur plusieurs egress sans
  protocole spécial ; le parallélisme est donc inter-requêtes + hedge.
- Les quotas restent ceux de chaque provider ; le mode ne les multiplie pas.
- Le direct n'est jamais un fallback silencieux.
- La feature 007 (provider abstraction, erreurs, ledger) est un prérequis.
