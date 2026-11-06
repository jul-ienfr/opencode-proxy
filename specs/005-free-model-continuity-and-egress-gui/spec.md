# Feature Specification: Continuité du modèle free, pilotage GUI des egress et profils vérifiés

**Feature Branch**: `005-free-model-continuity-and-egress-gui`  
**Created**: 2026-11-06  
**Status**: **Draft — P0/P0.5, deux dernières versions, six stations WARP, 6×200 et rotation automatique**  
**Input**: demande opérateur « ne plus hardcoder le modèle free ; utiliser le
système de découverte/probe existant ; dans la GUI, choisir VPN NordVPN ou
WARP et le nombre de stations, puis laisser le système créer les stations ».

## Contexte

Trois problèmes distincts sont mélangés dans l'exploitation actuelle.

### 1. Le modèle free par défaut est encore une valeur statique

Le proxy possède déjà une **découverte vivante** des modèles gratuits :

- `config/discovery.py` récupère le catalogue, applique les ids dans
  `FREE_MODELS` / `MODELS` / `FREE_MODEL_MAP`, et enregistre les verdicts
  dans `config/free_models_probed.json` (`alive`, `limited`, `dead`) ;
- `config/settings.py::ensure_free_models_on_boot()` lance cette découverte
  au démarrage et en arrière-plan ;
- `GET /api/free-models` expose l'inventaire, le statut et le `route_target`.

Mais le **default target** reste `free_discovery.default_target`
(`mimo-v2.6-flash-free` dans `config.yaml`). Quand il disparaît du catalogue,
`apply_discovered_free_models` se contente d'un `log.warning` et d'un fallback
local (`new_pool[0]`) : **il ne persiste pas** le nouveau target et ne met pas
à jour `FREE_DISCOVERY_DEFAULT_TARGET`. D'autres ids restent hardcodés
(`jev-1.13-free` dans `opencode.py`, `deepseek-v4.1-flash-free` en défaut de
`scripts/fresh_chain.py`).

### 2. La GUI ne pilote pas réellement le mode d'egress

`VpnStatus.svelte` propose `vpn | socks5 | direct` ; `VpnRouting.svelte`
propose `station_count` et appelle `/api/vpn-config`. Le backend sait
hot-reload `station_count` via `_apply_station_count` pour les stations
NordVPN/gluetun, mais :

- il n'existe pas de mode **WARP** dans la GUI ;
- les conteneurs WARP actuels (quatre `caomingjun/warp`) ont été créés à la
  main, hors GUI et hors config managée ;
- aucun composant ne converge « mode + N stations » de façon idempotente,
  avec plan, vérification et rollback.

### 3. Les identités envoyées au provider ne sont pas isolées par station

Le provider ne doit pas pouvoir corréler deux stations comme la même
installation. Or l'état actuel fuit :

- le prompt système officiel (`free/official_payload.json`,
  `free/captures/2.2.5.json`) contient le **chemin réel du workspace**
  (nom d'utilisateur et arborescence locale), la date de capture, et des
  champs `Workspace root folder` / `Platform` / `Is directory a git repo` /
  `Prefer ...` propres à la machine ;
- `free/identities.py` dérive `project` de l'**IP** ; deux stations sur le
  même bloc IPv4 (WARP IPv4 partagé) partageraient le même project ;
- le pool `socks5` fait un **round-robin par requête** : une conversation
  multi-tours peut être servie par plusieurs stations, et le même historique
  apparaît alors sous plusieurs `ses_`/project — corrélation directe ;
- certains IDs globaux (`_proxy_session_id`, fallback `jev-1.13-free`)
  ne sont pas station-scopés ;
- le profil TLS/UA est partagé par toutes les stations (un seul profil
  vérifié 1.19.5) : c'est un fingerprint d'installation commun, à défaut de
  profils multiples vérifiés.

## Objectifs

1. **Continuité du modèle free** : utiliser la découverte/probe existante
   comme source de vérité, et ajouter uniquement une **politique de
   réconciliation du default target** (lecture de l'état existant, choix d'un
   remplaçant, persistance, propagation).
2. **Zéro duplication** : ne pas ajouter un second fetcher de catalogue, ni
   une seconde sonde de modèles. Réutiliser `FREE_MODELS`,
   `free_models_probed.json`, `FREE_MODEL_MAP` et `ensure_free_models_on_boot`.
3. **Pilotage GUI** : un panneau unique permet de choisir le mode
   (`vpn` ou `warp`, avancé `socks5`/`direct`) et le nombre de stations ; le
   système crée, vérifie et bascule les stations dans le mode choisi.
4. **Idempotence** : rejouer « appliquer 5 stations WARP » ne crée pas de
   doublons, ne coupe pas les stations saines et produit le même état final.
5. **Rollback** : si la création échoue, l'ancien ensemble reste actif.
6. **Observabilité** : la GUI montre l'état désiré, l'état réel, l'IP/bloc de
   chaque station, la dernière erreur et la raison du dernier changement de
   modèle.
7. **Isolation d'identité par station** : chaque station possède un tuple
   d'identité indépendant — `project`, `ses_`, `msg_`, trace, `<env>`, prompt
   — et aucun de ces identifiants n'est partagé entre deux stations.
8. **Affinité de conversation** : une conversation multi-tours reste sur la
   même station (donc la même identité) ; le round-robin ne s'applique qu'aux
   conversations nouvelles ou aux requêtes sans historique.
9. **Audit anti-corrélation** : au démarrage et après chaque apply, vérifier
   que les tuples sont disjoints et que le prompt ne contient aucun chemin,
   nom d'utilisateur, hostname, hash de dépôt ou autre marqueur de la machine
   réelle.
10. **Profils vérifiés** : ne générer que des profils ayant réellement répondu
    200 avec contenu sur des blocs distincts, avec preuve, TTL et rejet des
    tuples mixtes.

## Non-objectifs

- Implémenter quoi que ce soit dans cette itération.
- Garantir un quota « illimité » : le quota reste par bloc d'egress.
- Remplacer la découverte/probe existante par un nouveau service.
- Choisir un modèle payant en fallback.
- Inventer ou mélanger des profils UA/TLS non vérifiés.
- Provisionner des comptes ou contourner les limites de sessions NordVPN.
- Modifier la jambe payante.

> **Règle P0** : aucune activation de plusieurs stations avant que
> l'isolation d'identité (FR-019…FR-030) ne soit en place. Le round-robin
> actuel envoie le même historique sous plusieurs identités.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Le modèle free survit à une disparition (Priority: P1)

Le modèle free configuré disparaît du catalogue ou passe `dead`. Au prochain
cycle de découverte (ou au démarrage suivant), le système choisit un modèle
free **vivant** dans l'état déjà probe, met à jour le default target, et la
GUI montre l'ancien et le nouveau.

**Why this priority**: un modèle hardcodé qui disparaît rend la jambe free
inutilisable sans intervention manuelle.

**Independent Test**: injecter un état `FREE_MODELS` sans le target configuré
et un `free_models_probed.json` avec un candidat `alive` ; vérifier que la
politique choisit ce candidat, met à jour `FREE_DISCOVERY_DEFAULT_TARGET` et
persiste `free_discovery.default_target`.

**Acceptance Scenarios**:

1. **Given** un target configuré présent dans `FREE_MODELS` avec verdict
   `alive`, **When** la réconciliation tourne, **Then** le target est conservé
   et aucune écriture disque n'a lieu.
2. **Given** un target absent de `FREE_MODELS`, **When** la réconciliation
   tourne, **Then** un remplaçant `alive` est choisi, persisté et propagé.
3. **Given** un target présent mais `dead`, **When** un autre modèle est
   `alive`, **Then** le remplaçant est choisi.
4. **Given** aucun modèle `alive`, **When** la réconciliation tourne,
   **Then** le système ne choisit pas un modèle payant ; il conserve le
   target et expose un état `no_free_model` clair.

### User Story 2 — La GUI choisit VPN ou WARP et le nombre de stations (Priority: P1)

Un opérateur ouvre la page « Modèles gratuits / Egress », choisit
`VPN (NordVPN)` ou `WARP`, saisit 5 stations, clique Appliquer. Le système
planifie, crée les stations, vérifie qu'elles répondent, puis bascule le pool.

**Why this priority**: c'est le levier de capacité ; aujourd'hui il faut lancer
des conteneurs à la main.

**Independent Test**: avec un `DockerRunner` factice, appliquer `warp × 3`
doit produire un plan de 3 conteneurs, 3 entrées `socks5_proxies` labellisées
et 3 vérifications ; rejouer le même apply ne doit rien recréer.

**Acceptance Scenarios**:

1. **Given** le mode `warp` et 4 stations, **When** l'opérateur applique,
   **Then** quatre stations WARP distinctes sont créées et vérifiées, et la
   GUI affiche leurs blocs.
2. **Given** le mode `vpn` et 5 stations, **When** l'opérateur applique,
   **Then** le système converge via `_apply_station_count(5)` et vérifie les
   cinq egress.
3. **Given** une création qui échoue à la 3ᵉ station, **When** le rollback
   s'exécute, **Then** les stations saines précédentes restent actives.
4. **Given** le même état désiré déjà atteint, **When** l'opérateur réapplique,
   **Then** aucun conteneur n'est recréé.

### User Story 3 — La GUI explique l'état (Priority: P2)

L'opérateur voit, pour chaque station, le mode, le label, l'IP/bloc, l'état
(`healthy`, `provisioning`, `cooling`, `failed`), la dernière erreur et le
temps depuis le dernier succès.

**Independent Test**: un état simulé avec 4 stations WARP et 1 en échec
affiche 4 lignes, dont une `failed` avec la raison.

### User Story 4 — Le système ne casse pas le service en basculant (Priority: P2)

Un changement de mode ou de nombre de stations ne coupe pas les requêtes en
cours : les nouvelles stations sont vérifiées avant que les anciennes soient
retirées.

**Independent Test**: un plan `warp 4 → 6` doit créer 2 nouvelles stations,
les vérifier, puis seulement ensuite drainer les éventuelles surnuméraires.

### User Story 5 — Le provider ne peut pas corréler les stations (Priority: P1)

Deux requêtes servies par deux stations différentes ne doivent partager aucun
identifiant : ni `project`, ni `ses_`, ni `msg_`, ni trace, ni chemin
d'environnement. Une même conversation, elle, reste sur une seule station.

**Why this priority**: si le provider voit le même project/session/trace ou le
même chemin réel sur deux egress, il peut reconstituer une installation unique
et relier les quotas.

**Independent Test**: construire les en-têtes/corps de deux stations et
vérifier que les ensembles `project`, `ses_`, `msg_`, `trace` sont disjoints ;
envoyer cinq tours d'une même conversation et vérifier qu'ils restent sur la
même station.

**Acceptance Scenarios**:

1. **Given** deux stations actives, **When** on compare leurs tuples,
   **Then** `project`, `ses_`, `msg_` et trace sont tous différents.
2. **Given** une conversation de cinq tours, **When** le pool choisit,
   **Then** les cinq tours utilisent la même station et le même `ses_`.
3. **Given** le prompt système envoyé, **When** on le scanne, **Then** il ne
   contient ni `C:\Users\`, ni le nom d'utilisateur, ni le hostname, ni le hash
   du dépôt, ni le vrai workspace ; le `<env>` est minimal et synthétique.
4. **Given** deux stations qui produiraient le même project, **When** l'audit
   tourne, **Then** la station en doublon est exclue ou l'apply échoue avec un
   message explicite.

### User Story 6 — Seuls des profils vérifiés sont utilisés (Priority: P1)

Le générateur existant (`free/identity_generator.py`) produit des profils
complets, mais son drapeau `verified` est statique. Le système doit vérifier
chaque profil contre le gate, sur plusieurs blocs d'egress, puis n'émettre que
les profils réellement vérifiés.

**Why this priority**: un profil « vérifié » sur le papier mais refusé en
live, ou un tuple mixte (UA 2.2.5 + TLS 1.3.14), est détectable et peut
corréler ou bloquer les stations.

**Independent Test**: générer deux profils, en vérifier un sur deux blocs
distincts, vérifier que seul celui-là est émis, que l'autre reste `candidate`,
et qu'un tuple mixte est rejeté par l'audit.

**Acceptance Scenarios**:

1. **Given** un profil qui répond 200 avec contenu sur deux blocs distincts,
   **When** le registre est reconstruit, **Then** il passe `verified` et peut
   être assigné.
2. **Given** un profil qui répond 200 sur un bloc puis 429 sur un autre,
   **When** le vérificateur conclut, **Then** le 429 est classé `quota`, pas
   `reject`, et le profil reste `candidate`.
3. **Given** quatre stations et quatre profils vérifiés, **When** l'audit
   tourne, **Then** chaque station a un profil distinct.
4. **Given** six stations et deux profils vérifiés, **When** l'audit tourne,
   **Then** la réutilisation est visible en `shared_profile` et documentée.
5. **Given** un UA 2.2.5 associé à un JA3 Bun 1.3.14, **When** l'audit
   tourne, **Then** le tuple est rejeté comme impossible.

### Edge Cases

- **Modèle** : le target disparaît **pendant** un cycle de probe ; la requête
  en cours garde le modèle déjà résolu, la réconciliation ne s'applique
  qu'après le cycle.
- **Modèle** : `free_models_probed.json` est absent, corrompu ou plus vieux
  que le TTL ; aucun remplacement n'est écrit, l'état est exposé comme
  `catalog_unavailable` / `probe_stale`.
- **Modèle** : tous les candidats sont `limited` ou `unproven` ; le système
  expose `no_free_model` et ne bascule ni vers un payant ni vers un modèle non
  prouvé.
- **Modèle** : un `pinned_target` devient `dead` ; le pin reste souverain et
  l'état `pinned_unavailable` est affiché, sans substitution silencieuse.
- **Egress** : deux stations obtiennent le même `/64` ou la même IPv4 ; la
  seconde est exclue et l'apply échoue si le nombre demandé n'est plus
  atteignable.
- **Egress** : l'enregistrement WARP est throttlé par Cloudflare ; les
  nouvelles stations sont abandonnées, l'ancien ensemble reste actif, le plan
  indique `registration_throttled`.
- **Egress** : Docker est indisponible, un port est occupé, ou la limite de
  sessions VPN est dépassée ; préflight en échec, aucune station modifiée,
  aucun clamp silencieux.
- **Egress** : deux apply sont lancés en parallèle ; le second reçoit
  `409 provisioning_busy` et ne modifie rien.
- **Egress** : un proxy `socks5` manuel existe déjà sur un port du plan ; il
  est conservé et le plan choisit un autre port ou échoue explicitement.
- **Identité** : le secret local est absent ou illisible ; le système refuse
  d'activer plus d'une station plutôt que de générer des identités instables.
- **Identité** : deux labels produisent le même `project` (collision HMAC ou
  label dupliqué) ; l'audit exclut la station et journalise la collision.
- **Identité** : le TTL d'affinité expire pendant une conversation ; la
  station existante est réutilisée si elle est saine, sinon le failover est
  journalisé et l'historique n'est envoyé qu'à une seule nouvelle station.
- **Identité** : le prompt templaté est refusé par le gate ; le système
  échoue fermé (pas de retour au prompt contenant le chemin réel) et expose
  la raison.
- **Identité** : IPv6/WARP IPv6 tombe ; la station est marquée `failed`, le
  pool ne bascule pas silencieusement vers `direct` si celui-ci est désactivé.
- **Conversation** : une requête sans historique (premier tour) peut être
  routée librement ; dès qu'un historique existe, l'affinité est obligatoire.
- **Conversation** : une station meurt entre deux tours ; le tour suivant peut
  basculer, mais jamais deux copies du même historique ne partent en
  parallèle.
- **Profils** : un profil passe sur un bloc puis échoue sur un autre ; le
  second verdict n'est pas un rejet tant que l'échec peut être un quota.
- **Profils** : le registre est absent, corrompu ou vide ; le système
  n'active qu'un seul profil canonical vérifié, pas une flotte multi-profils.
- **Profils** : un profil est `stale` après une release client ; il est
  exclu jusqu'à re-vérification.
- **Profils** : le nombre de stations dépasse le nombre de profils vérifiés ;
  la réutilisation est marquée `shared_profile` et la limite TLS est
  exposée.
- **Profils** : un tuple mélange UA, Bun, JA3, ordre d'en-têtes ou corps de
  versions différentes ; l'audit le rejette avant envoi.
- **Egress épuisé** : une station WARP managée est à quota zéro et aucune
  requête ne peut plus l'atteindre ; le watchdog la tourne quand même.
- **Rotation** : le redémarrage rend le même `/64` ou un `/64` déjà utilisé ;
  le système recrée ou échoue proprement, sans appliquer le doublon.

## Functional Requirements

### Continuité du modèle

- **FR-001** La réconciliation DOIT lire `FREE_MODELS`, `FREE_MODEL_MAP`,
  `FREE_DISCOVERY_DEFAULT_TARGET` et `config/free_models_probed.json` ; elle
  NE DOIT PAS fetcher le catalogue elle-même.
- **FR-002** Si le target est présent dans `FREE_MODELS` et n'est pas `dead`,
  il DOIT être conservé sans écriture.
- **FR-003** Si le target est absent ou `dead`, un remplaçant DOIT être choisi
  parmi les modèles `alive`, avec priorité : même famille, puis liste de
  préférence configurable, puis premier `alive`.
- **FR-004** Le remplaçant DOIT être persisté dans
  `free_discovery.default_target` (si `auto_default_target: true`) et propagé
  à `FREE_DISCOVERY_DEFAULT_TARGET`.
- **FR-005** Une entrée **explicite** de `FREE_MODEL_MAP` NE DOIT JAMAIS être
  réécrite par la politique.
- **FR-006** Aucun modèle payant NE DOIT être choisi en fallback.
- **FR-007** Si aucun modèle `alive` n'existe, l'état `no_free_model` DOIT être
  exposé (GUI/health), sans écriture destructive.
- **FR-008** La réconciliation DOIT être déclenchée après
  `ensure_free_models_on_boot()` et après chaque cycle de probe, avec un
  verrou pour éviter les courses.
- **FR-009** Un mode « pinned » (GUI) DOIT permettre à l'opérateur de figer
  un target ; la politique ne le remplace alors pas.

### Egress GUI et provisioning

- **FR-010** La GUI DOIT proposer les modes `vpn` (NordVPN) et `warp`, avec
  `socks5`/`direct` en avancé.
- **FR-011** La GUI DOIT proposer un nombre de stations 1–10, avec les limites
  réelles affichées (sessions NordVPN, registrations WARP).
- **FR-012** Le backend DOIT exposer `GET /api/free-egress` (état désiré,
  état réel, stations, blocs, erreurs) et `PUT /api/free-egress` (persister
  puis converger).
- **FR-013** Le provisioning DOIT être idempotent : un plan `plan()` décrit
  les créations/suppressions, `apply()` ne touche que le delta.
- **FR-014** Le provisioning DOIT vérifier chaque station (egress + probe
  free) avant de la déclarer active.
- **FR-015** En cas d'échec, l'ancien ensemble DOIT rester actif ; les
  nouvelles stations en échec sont retirées.
- **FR-016** Le provisioning NE DOIT PAS écraser les proxies `socks5`
  saisis manuellement ; il gère un bloc `managed` séparé.
- **FR-017** Un verrou DOIT sérialiser les apply ; deux clics ne doivent pas
  lancer deux provisioning concurrents.
- **FR-018** La GUI DOIT afficher le plan avant application (dry-run) et le
  progrès station par station.

### Isolation d'identité et anti-corrélation

- **FR-019** Chaque station DOIT avoir une `StationIdentity` dérivée de son
  label et d'un secret local persistant, jamais de l'IP seule ni du workspace
  réel.
- **FR-020** Lorsque le profil émet `x-opencode-project` (2.2.5/1.19.5), il
  DOIT être unique par station (40 hex, dérivé du label/secret), jamais
  `global`, jamais le hash du dépôt réel. Les profils legacy 1.18.31/1.19.3
  n'émettent pas ce header : leur isolation repose sur les session headers,
  le TLS et l'UA.
- **FR-021** `x-opencode-session` (et le quartet) DOIT être minté par
  `(station, conversation)`, canonique `…ffe`, jamais partagé entre stations.
  Les profils legacy utilisent `x-opencode-session-id`/`x-session-affinity`/
  `x-session-id` avec la même règle.
- **FR-022** Lorsque le profil émet `x-opencode-request` / `msg_`, il DOIT être
  frais par requête, jamais partagé entre stations.
- **FR-023** Lorsque le profil émet `b3` / `traceparent`, la trace DOIT être
  station-scopée (trace par station, span par requête) ; aucun trace ID
  commun. Les profils legacy n'émettent pas de tracing.
- **FR-024** Le bloc `<env>` DOIT être minimal et synthétique par station
  (`Working directory: /workspace/<label>`), sans chemin réel, nom
  d'utilisateur, hostname, état git, plateforme réelle ni chemin temporaire.
- **FR-025** Le prompt officiel DOIT être templaté : marqueur officiel
  conservé (signature du gate), date UTC courante, `<env>` synthétique ; aucun
  chemin réel ni date de capture. Le bloc `Instructions from:` (chemin +
  contenu `AGENTS.md`) DOIT être supprimé, pas seulement son chemin.
- **FR-026** Une conversation DOIT être **épinglée** à une station (affinité
  par `conversation`/`prompt_cache_key`/empreinte system+first-user) pour sa
  durée de vie ou un TTL défini ; le round-robin ne s'applique qu'aux
  conversations nouvelles ou sans historique.
- **FR-027** Le failover de conversation NE DOIT se produire que si la station
  est inutilisable ; la nouvelle station reçoit une nouvelle identité, et le
  même historique ne doit pas être envoyé en parallèle à deux stations.
- **FR-028** Un audit DOIT vérifier la disjonction des tuples
  (`project`, `ses_`, `msg_`, trace) et l'absence de sous-strings interdites
  dans le prompt ; une station dupliquée/fuyante est exclue ou l'apply échoue.
- **FR-029** Les sessions curl/connexions/cookies NE DOIVENT PAS être
  partagés entre stations ; chaque station a sa clé de session TLS.
- **FR-030** Les vecteurs partagés par conception (JA3/TLS Bun, version UA si
  un seul profil vérifié, `Authorization: Bearer public`) DOIVENT être
  documentés ; aucun autre identifiant ne doit être commun. Le header
  `x-opencode-client` DOIT être canonicalisé sur la valeur du profil vérifié
  (capture 2.2.5 : `cli`) ; un vault hérité divergent (`desktop`) est migré ou
  exclu.

### Profils vérifiés

- **FR-031** Le générateur NE DOIT émettre que des profils dont le statut est
  `verified` et dont la preuve n'est pas `stale` ; le drapeau statique de
  `VERSION_SPECS` ne suffit pas. La capture ne DOIT jamais fournir
  `project`/`ses_`/`msg_`/trace : ces identifiants sont mintés par station.
- **FR-032** Le registre DOIT conserver, par profil, la provenance, l'empreinte
  de capture, les preuves (bloc, statut, contenu, date), la famille TLS et la
  date d'expiration ; l'écriture DOIT être atomique.
- **FR-033** La vérification DOIT exiger au moins deux `200` avec contenu sur
  des blocs d'egress distincts ; un `429` DOIT être classé `quota`, jamais
  `reject`.
- **FR-034** Un profil DOIT être cohérent de bout en bout : UA, suffixe Bun,
  JA3, ordre des en-têtes, corps et en-têtes de session issus de la même
  version/client ; aucun tuple mixte.
- **FR-035** L'assignation DOIT être déterministe par station
  (`HMAC(secret, "profile:" + label)`) et stable pendant la vie de la station
  et de la conversation.
- **FR-036** Si le nombre de profils vérifiés est inférieur au nombre de
  stations, la réutilisation DOIT être autorisée mais marquée
  `shared_profile` dans l'audit ; la diversité TLS limitée DOIT être exposée.
- **FR-037** Un profil `rejected` ou `stale` DOIT être exclu ; le fallback
  DOIT être le profil canonical vérifié, jamais un modèle ou un egress payant.
- **FR-038** L'audit DOIT exposer `profile_id`, `tls_family`, `shared_profile`,
  `stale`, `rejected` et vérifier la cohérence du tuple avant envoi.

### Rotation automatique des egress épuisés

- **FR-039** Une station WARP **managée** dont le bucket quota est épuisé DOIT
  être tournée automatiquement : redémarrage ou recréation du conteneur,
  résolution du nouvel egress, vérification d'un bloc distinct, puis mise à
  jour de l'endpoint et de la config.
- **FR-040** La rotation DOIT être déclenchée même sans requête en cours
  (watchdog périodique) et sur chaque `429` de quota ; elle NE DOIT PAS toucher
  un proxy non `managed`.
- **FR-041** Une rotation NE DOIT PAS appliquer un bloc déjà utilisé par une
  autre station ; elle réessaie ou échoue proprement.
- **FR-042** La rotation DOIT être single-flight par label et effacer le
  bad-mark de la station après succès ; l'ancien conteneur ne doit pas être
  supprimé avant que le nouveau soit sain.
- **FR-043** Le runtime DOIT sélectionner uniquement les **deux dernières
  versions vérifiées** (aujourd'hui `2.2.5` et `1.19.5`) et les versions
  futures `>= 1.19.5` ; les versions antérieures (1.19.3/1.18.31) sont
  retirées du registre actif et conservées seulement comme archive.
- **FR-044** Un endpoint SOCKS5 statique NE DOIT PAS être soumis au contrat
  santé des stations Docker (auth/tunnel) ; sa sélection dépend de `enabled`,
  `bad_until`, du quota et de l'egress, sinon le pool le déclare inutilisable
  à tort.
- **FR-045** Le cooldown d'un endpoint SOCKS5 DOIT être keyé sur son egress
  réel normalisé (`/64` IPv6, IP exacte IPv4), pas sur son label stable, afin
  qu'une rotation WARP débloque immédiatement le nouveau bloc.
- **FR-046** Le round-robin des nouvelles conversations NE DOIT s'appliquer
  que si la GUI l'a explicitement demandé : toggle SOCKS5 de rotation actif,
  `free_parallel.enabled=true` et `free_parallel.routing=round-robin`. En
  `failover`, le primaire reste sticky et ne bascule que sur panne/quota.
- **FR-047** Les modes GUI DOIVENT être appliqués en SOCKS5 : `strict` =
  compteur 1,2,3… ; `load-balance` = station la moins chargée ; `hedge` =
  course de deux stations au premier tour, puis la gagnante est épinglée à la
  conversation (jamais deux copies du même historique en parallèle).
- **FR-048** `free_parallel.enabled=false` ou le toggle SOCKS5 de rotation
  désactivé DOIT rendre la sélection sticky, sans round-robin implicite.
- **FR-049** `quality_rotation` DOIT pouvoir tourner un endpoint WARP managé
  (même rotateur single-flight que la rotation quota) et ignorer un proxy
  statique non managé ; `latency_rotation` hard-cooled NE DOIT jamais rendre
  zéro candidat SOCKS5 (dernier passage d'admission).
- **FR-050** `post_rotation_probe` DOIT envoyer une complétion minuscule
  (`max_tokens`) via le nouvel egress après une rotation WARP managée, jusqu'à
  `max_attempts`. Un `200` ou un `429` prouve que l'egress atteint l'upstream ;
  les autres échecs bad-markent temporairement l'endpoint.
- **FR-051** Le watchdog de rotation DOIT tourner un endpoint WARP managé dont
  un couple `(modèle, IP)` est bloqué après `429`, même si le ledger
  journalier n'est pas épuisé, afin de rendre une station fraîche avant que
  tous les modèles ne soient bloqués.
- **FR-052** Les timeouts SOCKS5 DOIVENT être corrélés par couples
  `(station, IP)` distincts : au-delà du seuil dans la fenêtre, la cause est
  un événement amont/réseau et aucun bad-mark supplémentaire ne doit être
  posé, pour ne pas vider le pool de tous ses candidats.
- **FR-053** Au redémarrage, les bad-marks transitoires NE DOIVENT PAS être
  restaurés : les compteurs et les quotas journaliers persistent, mais les
  tunnels doivent être immédiatement retentés (sinon 2-3 stations restent
  inutilisables pendant le TTL de timeout).
- **FR-054** Un endpoint WARP managé dont l'IP courante est `trial-dead` pour
  un modèle DOIT être tourné par le watchdog, même si le bloc
  `(modèle, IP)` n'est plus dans la fenêtre courte : un trial-dead de 6 h ne
  doit pas immobiliser une station qui peut obtenir une IP fraîche.
- **FR-055** L'invariant de blocs distincts (EP-06) DOIT être maintenu sous
  rotations concurrentes : l'application de l'egress se fait sous le verrou
  global, et deux endpoints managés qui partagent un `/64` déclenchent la
  rotation du second.
- **FR-056** Toute commande externe lancée par le runtime Windows DOIT utiliser
  `CREATE_NO_WINDOW` : aucune fenêtre de console ne doit apparaître, y compris
  pendant les rotations WARP et les sondes.
- **FR-057** La GUI SOCKS5 DOIT afficher le `label`, l'egress et le bloc de
  chaque endpoint ; le bouton Test DOIT cibler le label exact et forcer IPv6
  pour un endpoint IPv6, afin de distinguer deux proxies partageant host:port.
- **FR-058** Un timeout SOCKS5 NE DOIT PAS poser de bad-mark s'il ferait
  tomber le nombre d'endpoints sains sous le plancher de redondance
  `min(3, n-1)` : le pool conserve toujours plusieurs candidats.
- **FR-059** En mode SOCKS5, `capacity_snapshot()` DOIT compter les endpoints
  statiques (configurés/utilisables/IPs distincts), pas les stations Docker
  vides ; `/health` ne doit pas rester `degraded` avec « 0/1 stations ».

## Key Entities

- **FreeModelCatalog** : `FREE_MODELS`, `MODELS`, `FREE_MODEL_MAP`.
- **ModelProbe** : verdict `alive/limited/dead`, statut, raison, date.
- **ModelSelection** : target, source (config/auto/mapping), reason,
  previous, changed_at.
- **EgressDesiredState** : mode, station_count, provider, options.
- **Station** : id, mode, label, host/port, egress IP/block, state, last_error.
- **ProvisioningPlan** : create/keep/remove/verify actions.
- **ProvisioningRun** : id, plan, status, logs, rollback state.
- **StationIdentity** : label, project, session seed, trace seed, UA/profil,
  `<env>` synthétique.
- **IdentityTuple** : project, `ses_`, `msg_`, trace, UA, env.
- **ConversationAffinity** : clé de conversation → station, TTL, raison.
- **IdentityAudit** : tuples disjoints, sous-strings interdites, résultat.
- **VerifiedProfile** : id, version, client, famille TLS, JA3, corps, statut,
  preuves, expiration.
- **ProfileEvidence** : bloc, statut, contenu, latence, hash, date.
- **ProfileRegistry** : ensemble versionné de profils vérifiés/stale/rejected.
- **ProfileAssignment** : station → profil, date, raison, partage éventuel.

## Acceptance Criteria

1. **AC-001** La spec distingue clairement « utiliser la découverte
   existante » de « ajouter une politique de target ».
2. **AC-002** Aucun nouveau fetcher de catalogue n'est requis.
3. **AC-003** Le target est conservé si `alive`, remplacé si absent/`dead`,
   jamais remplacé par un payant.
4. **AC-004** La GUI expose mode + nombre de stations + plan + état réel.
5. **AC-005** Un apply idempotent ne recrée pas les stations existantes.
6. **AC-006** Un échec de provisioning ne coupe pas l'ancien ensemble.
7. **AC-007** Les proxies `socks5` manuels ne sont pas écrasés.
8. **AC-008** Le plan couvre les deux providers (VPN/WARP) et leurs limites.
9. **AC-009** Deux stations actives ont des `project`, `ses_`, `msg_` et trace
   disjoints.
10. **AC-010** Une conversation de cinq tours reste sur la même station et le
    même `ses_`.
11. **AC-011** Le prompt envoyé ne contient aucun chemin réel, nom
    d'utilisateur, hostname, hash de dépôt ou date de capture ; le `<env>` est
    minimal et synthétique.
12. **AC-012** L'audit détecte un project dupliqué ou une sous-string
    interdite et exclut la station (ou bloque l'apply).
13. **AC-013** Aucune session curl, connexion ou cookie n'est partagé entre
    stations.
14. **AC-014** Les vecteurs partagés par conception (TLS/JA3, UA si un seul
    profil, `Bearer public`) sont documentés et limités à ceux-là.
15. **AC-015** Aucun profil non vérifié n'est émis ; un profil `stale` ou
    `rejected` est exclu.
16. **AC-016** Un `429` n'est jamais compté comme un rejet de profil ; la
    vérification exige deux `200` sur des blocs distincts.
17. **AC-017** Aucun tuple mixte (UA/Bun/JA3/ordre/corps) n'est assigné.
18. **AC-018** Les profils partagés et la limite de diversité TLS sont visibles
    dans l'audit.
19. **AC-019** Une station WARP managée épuisée est tournée automatiquement,
    avec un nouvel egress et un bloc distinct.
20. **AC-020** Un proxy non managé n'est jamais recréé par la rotation.
21. **AC-021** Le runtime n'assigne que les deux dernières versions et les
    futures ; une version antérieure est exclue.
22. **AC-022** Un endpoint SOCKS5 statique est `usable` sans contrat Docker
    quand il est activé, sans bad-mark ni quota épuisé.
23. **AC-023** Une rotation WARP change la clé de cooldown et le nouveau bloc
    est immédiatement sélectionnable.
24. **AC-024** Avec `routing=failover`, trois nouvelles conversations
    sortent sur la même station ; avec `routing=round-robin`, elles se
    répartissent selon le mode (`strict` → compteur, `load-balance` →
    moins chargée).
25. **AC-025** `socks5_auto_rotate=false` ou `free_parallel.enabled=false`
    rend la sélection sticky.
26. **AC-026** En `hedge`, le premier tour incrémente deux stations ; le
    tour suivant de la même conversation n'en incrémente qu'une (gagnante
    épinglée).
27. **AC-027** `quality_rotation` déclenche une rotation WARP sur un endpoint
    managé et ignore un proxy non managé ; une flotte hard-cooled par
    `latency_rotation` reste sélectionnable via le dernier passage.
28. **AC-028** `post_rotation_probe` s'exécute après rotation : `200`/`429`
    acceptés comme egress joignable, échec réseau bad-marké après
    `max_attempts`.
29. **AC-029** Un endpoint managé bloqué pour un couple `(modèle, IP)` est
    tourné par le watchdog même si son ledger journalier n'est pas épuisé.
30. **AC-030** Trois timeouts sur des couples `(station, IP)` distincts dans
    la fenêtre ne bad-markent pas le troisième endpoint ; le pool conserve
    ses candidats.
31. **AC-031** Un redémarrage avec un état contenant `bad_remaining` restaure
    les compteurs mais rend les stations immédiatement sélectionnables.
32. **AC-032** Un endpoint managé `trial-dead` est tourné avec la cause
    `trial_dead`, sans attendre l'expiration du cooldown de 6 h.
33. **AC-033** Après deux rotations concurrentes, les six endpoints managés
    ont six blocs `/64` distincts ; un doublon déclenche la rotation du
    second endpoint avec la cause `duplicate_block`.
34. **AC-034** Les commandes `docker`/`curl` du rotateur WARP passent
    `creationflags=CREATE_NO_WINDOW` sous Windows.
35. **AC-035** La liste SOCKS5 de la GUI affiche label/egress/bloc ; le Test
    d'un endpoint labellisé utilise sa ligne et force IPv6 si nécessaire.
36. **AC-036** Un timeout ne bad-mark pas un endpoint si le nombre d'autres
    endpoints sains est inférieur au plancher `min(3, n-1)`.
37. **AC-037** En SOCKS5, `/health` expose `configured=7`, `usable=7`,
    `capacity_degraded=false` quand les sept endpoints répondent.

## Success Criteria

Les cibles ci-dessous sont mesurables sur un jeu de tests déterministe
(`DockerRunner` factice, état de probe injecté, horloge contrôlée) et, pour
les scénarios live, sur des egress sacrifiables.

- **SC-001** Sur 20 réconciliations avec un target absent ou `dead` et au
  moins un `alive`, 20/20 choisissent un `alive`, 0/20 choisissent un payant,
  et 0/20 écrivent la config quand le target courant est sain.
- **SC-002** Sur 6 stations actives, l'audit retourne 0 collision de
  `project`, `ses_`, `msg_` et trace.
- **SC-003** Sur 10 conversations de 5 tours, 10/10 restent sur une seule
  station et un seul `ses_` ; 0 tour n'est envoyé à deux stations.
- **SC-004** Le scan du prompt sur 6 stations retourne 0 occurrence de chemin
  réel, nom d'utilisateur, hostname, hash de dépôt ou date de capture.
- **SC-005** Trois apply successifs du même état désiré recréent 0 conteneur
  après le premier apply.
- **SC-006** Un échec provoqué à la 3ᵉ création laisse le nombre de stations
  saines identique à l'état initial et ne draine aucune requête en cours.
- **SC-007** Le plan dry-run est visible avant tout apply, et un apply de
  5 stations converge en ≤ 10 min en WARP et ≤ 5 min en VPN (cible
  d'exploitation, mesurée et journalisée).
- **SC-008** Aucun scénario de test ne produit de fallback payant ; l'état
  `no_free_model` est explicite et testé.
- **SC-009** Une conversation dont la station tombe est reprise sur une seule
  nouvelle station, avec exactement un événement `conversation_failover` et
  aucune double émission d'historique.
- **SC-010** `GET /api/free-egress` reflète l'état réel (stations, blocs,
  santé, audit) en ≤ 2 s après la fin d'un apply.
- **SC-011** Sur 10 profils candidats, seuls ceux qui ont deux `200` avec
  contenu sur des blocs distincts sont émis ; 0 profil vérifié sur un seul
  bloc.
- **SC-012** Sur 6 stations et 6 profils vérifiés, 6/6 profils sont distincts ;
  avec 2 profils, 6/6 stations sont servies et 100 % des partages sont marqués
  `shared_profile`.
- **SC-013** Sur 50 requêtes d'audit, 0 tuple mixte n'est envoyé et 0 profil
  `stale`/`rejected` n'est sélectionné.
- **SC-014** Une rotation forcée de 2 stations managées retourne `started:2`,
  change les deux egress et persiste deux blocs `/64` distincts.
- **SC-015** Aucun conteneur non `managed` n'est recréé par la rotation.
- **SC-016** Avec quatre profils vérifiés (1.18.31, 1.19.3, 1.19.5, 2.2.5),
  le runtime n'assigne que 2.2.5 et 1.19.5 ; 1.18.31/1.19.3 ne sont jamais
  sélectionnés.
- **SC-017** Six requêtes proxy consécutives sur les six stations WARP
  managées rendent `200`, sans 429 de sélection interne.
- **SC-018** Deux tours d'une même conversation restent sur la même station ;
  une nouvelle conversation peut aller sur une autre station.
- **SC-019** Les paramètres GUI (`free_parallel.enabled/routing/mode`,
  `hedge_delay_ms`, toggle SOCKS5) changent réellement la sélection live,
  sans round-robin implicite.
- **SC-020** `quality_rotation` et `latency_rotation` restent effectifs en
  SOCKS5 : rotation WARP managée et jamais zéro candidat hard-cooled.
- **SC-021** `post_rotation_probe` valide le nouvel egress après rotation
  (200/429 = joignable) et ne laisse pas une station morte être servie.
- **SC-022** Un bloc `(modèle, IP)` bloqué déclenche la rotation WARP du
  managed concerné sans attendre l'épuisement du ledger.
- **SC-023** Une vague de timeouts corrélés ne vide pas le pool : la garde
  `(station, IP)` distincte empêche le bad-mark du dernier candidat.
- **SC-024** Après redémarrage, les stations sont immédiatement disponibles
  (pas de bad-mark transitoire restauré).
- **SC-025** Un trial-dead de 6 h sur un endpoint managé déclenche une
  rotation WARP au lieu d'immobiliser la station.
- **SC-026** Six endpoints managés ont six blocs `/64` distincts, y compris
  après des rotations concurrentes.
- **SC-027** Aucune fenêtre de console n'apparaît pendant les rotations et
  les sondes du runtime Windows.
- **SC-028** Les six stations WARP sont distinguables dans la GUI
  (label/egress/bloc) et le Test cible la bonne ligne.
- **SC-029** Une vague de timeouts ne réduit jamais la flotte sous le
  plancher de redondance `min(3, n-1)`.
- **SC-030** La capacité affichée par `/health` et la GUI reflète les
  endpoints SOCKS5 réels, pas les stations Docker vides.

## Assumptions

- La découverte/probe existante continue de tourner et de mettre à jour
  `free_models_probed.json`.
- Le backend a accès au CLI Docker (comme aujourd'hui).
- Les conteneurs WARP `caomingjun/warp` restent le provider par défaut ; un
  provider `warp-minimal` pourra être ajouté derrière la même interface.
- Le quota reste par bloc ; la GUI ne promet pas l'infini.

## Open Questions

Voir [open-questions.md](./open-questions.md) : persistance auto du target,
pin, provider WARP par défaut, politique de bascule (blue/green), plafond de
stations, emplacement GUI, conservation des modes avancés, valeurs
synthétiques du `<env>`, profils UA/TLS par station, politique de failover
d'une conversation, nombre/TTL des profils vérifiés et réutilisation quand les
stations dépassent les profils.
