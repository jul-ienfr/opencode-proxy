# Feature Specification: Egress utilisable par les clients — NordVPN, WARP, proxys, direct

**Feature Branch**: `023-client-usable-egress`
**Created**: 2026-11-17
**Status**: Draft (plan d'implémentation)
**Input**: « Je veux que le VPN NordVPN fonctionne sur toutes ces stations, mais
ce n'est pas fonctionnel pour moi, c'est-à-dire utilisable par mes clients. Si
ce n'est pas utilisable, ça ne fonctionne pas. Je veux également que ça
fonctionne sur le WARP, ainsi que sur les proxys et directement. Chaque option
peut être désactivée ou activée selon les configurations. Je veux un plan
complet qui analysera tout pour que tout fonctionne de façon parfaite, fiable,
qualitative, performante, optimale, maintenable et modulable. »

Références : constitution v1.3.0 (principes I, IV, V, VII, VIII, X, XIV, XVII,
**XVIII**), specs 006 (trois modes), 008 (auto-egress), 011 (fleet usability),
015 (SLO), 022 (réserve client zéro 429), 003 (parité wire).

## Clarifications

### Session 2026-11-17

- Q : Qu'est-ce qu'un endpoint « utilisable » ?
  R : Il a un transport connecté, un egress lu par deux sources concordantes,
  une preuve `200` avec contenu **du même `wire_fp`**, un bucket ni `spent` ni
  `burned`, et aucune sonde depuis `probe_cooldown_s`.
- Q : Comment tester un format sans détruire d'IP ?
  R : Une IP neuve par variante, jamais deux formats sur la même IP ; une IP
  brûlée reste exclue ≥ 24 h (principe XVIII).
- Q : Quelle est l'unité de quota ?
  R : L'IP. `/32` pour NordVPN, `/64` IPv6 pour WARP ; l'IPv4 WARP est une NAT
  partagée et ne crée aucune capacité.
- Q : Le direct peut-il servir de réserve ?
  R : Non. C'est un provider honnête, à bucket partagé, sans rotation ; il
  renvoie un 429 avec `reset_at` exact et n'est jamais compté en réserve.
- Q : Comment ajouter un client ?
  R : Une entrée dans `free/clients/registry.json` ; un client inconnu tombe
  dans `custom` (OpenAI chat) et ne casse jamais le proxy.
- Q : Les providers sont-ils exclusifs ?
  R : Non. Quatre toggles indépendants (NordVPN, WARP, proxys, direct), avec
  une stratégie et des poids ; le wire reste identique sur les quatre chemins.
- Q : Que signifie « impersonation » dans cette feature ?
  R : L'émulation réseau (TLS, en-têtes, corps, identité) et la couche de
  compatibilité client (protocole, outils, capacités) sont deux objets
  distincts, spécifiés séparément.

## 2. Vocabulaire exact

Ce que l'opérateur appelle « impersonation » couvre en réalité six couches
distinctes. Le plan les traite séparément, car elles ont des tests et des
responsables différents :

| Couche | Nom exact | Contenu |
|---|---|---|
| L0 | **Empreinte transport/TLS** | ClientHello (JA3/JA4), ALPN, HTTP/2, ordre des en-têtes ; c'est `curl_cffi` + `impersonation.json` |
| L1 | **En-têtes HTTP** | UA, `x-opencode-*`, `b3`/`traceparent`, quartet de session, positions et casse |
| L2 | **Parité de corps (wire)** | clés, ordre, types, 13 outils, `max_completion_tokens`, `stream_options` |
| L3 | **Sémantique d'identité** | session/trace/`project`/`prompt_cache_key` stables par conversation et par IP |
| L4 | **Adaptation de protocole** | conversion Anthropic Messages ↔ OpenAI Chat ↔ OpenAI Responses, dans les deux sens |
| L5 | **Politique d'outils** | outils autorisés, interdits, mappés, shims, `tool_choice`, retour des appels |
| L6 | **Négociation de capacités** | streaming, tools, raisonnement, vision, cache, `count_tokens`, contexte |

Termes à utiliser en revue : **émulation client** (L0-L3),
**couche de compatibilité client** (L4-L6), **parité wire** (L2), **politique
d'outils** (L5). « Impersonation » seul ne désigne que L0-L2 et ne doit pas
servir à désigner la conversion de protocole.

## 3. Problème mesuré

L'infrastructure « tourne » mais les clients reçoivent des 429. Les preuves du
2026-11-17 montrent trois causes distinctes, souvent confondues :

1. **Brûlure d'IP par format** (principe XVIII) : un corps non conforme fait
   passer l'IP en `429 FreeUsageLimitError` pour ≥ 24 h. Le même IP est ensuite
   inutilisable pour tout autre format.
2. **Le superviseur consomme le quota qu'il veut réserver** : chaque rotation
   envoie deux sondes au corps officiel complet (43 453 octets, 32 000 tokens)
   sur l'IP neuve. Le journal du 2026-11-17 montre la station 1 re-gagnée à
   21:23:11, relâchée à 21:25:39, re-gagnée à 21:25:56, relâchée à 21:26:16,
   re-gagnée à 21:28:52 — soit ~2 requêtes de quota détruites toutes les
   90-150 s, par station, avec jusqu'à six chaînes en parallèle.
3. **Décisions de libération non prouvées** : une IP fraîchement prouvée est
   relâchée sur `live_unusable`/`live_mismatch` à partir d'une lecture d'IP
   douteuse (`live 1.2.3.4`) ou d'un seul 429 client, sans vérifier que le
   format émetteur était conforme.

Conséquence : la réserve client oscille entre 0 et 6, la flotte re-sonde en
boucle, et le client reçoit des 429 que le proxy attribue au quota alors qu'ils
peuvent être des brûlures de forme.

## 4. Objectifs

- **O1** — Un client reçoit un 200 avec contenu tant qu'au moins une IP de
  réserve est prouvée, quel que soit le provider actif.
- **O2** — Une IP prouvée n'est jamais brûlée par le proxy : aucun corps non
  conforme n'atteint une IP de réserve ; les corps clients sont convertis vers
  le profil officiel ou refusés localement, avant l'egress.
- **O3** — Une IP n'est jamais relâchée sans preuve dure : accord de deux
  lecteurs d'IP indépendants, ou `429` classé `quota` avec `reset_at`
  fournisseur ; jamais sur un sentinel, un timeout ou un unique échec ambigu.
- **O4** — Le budget de sondes est séparé du budget client et plafonné
  (`probe_budget_per_ip`, `min_interval_s`) ; une IP de réserve n'est jamais
  sondée.
- **O5** — Les quatre chemins (NordVPN, WARP, proxys, direct) sont des
  providers de première classe, activables/désactivables indépendamment, avec
  une matrice de capacités explicite et un routage qui ne sert que du prouvé.
- **O6** — Chaque 429 est classé (`format`, `quota`, `session`, `upstream`) avec
  le corps amont conservé, l'empreinte du wire (`wire_fp`), l'IP et le
  `Retry-After` source. Zéro 429 non classé.
- **O7** — Rotation sous contrôle : un seul réparateur (principe VIII), au plus
  une rotation par station par `rotation.min_interval_s`, coalescence globale,
  et un changement de format ne rejoue jamais sur une IP déjà utilisée
  (principe XVIII).

## 5. Non-objectifs

- Jambe payante, second compte, résidentiel, attente comme solution.
- Plus de six stations NordVPN (`station_count` reste la borne dure).
- Réécriture big-bang des monolithes (`opencode.py` reste stranglé).
- Contourner le quota par un autre moyen que la parité de forme et la rotation
  d'IP.

## 6. Définition de « utilisable »

Un endpoint est **client-utilisable** si et seulement si :

1. son transport est `connected` et son egress est lu par **deux sources
   indépendantes** qui concordent ;
2. il possède une preuve `200` avec contenu, **du même `wire_fp`** que le corps
   que le client va envoyer, et vieille de moins de `proof_max_age_s` ;
3. son bucket n'est ni `spent` (reset futur) ni `burned` (≥ 24 h) ;
4. il n'a pas été sondé depuis `probe_cooldown_s` et son quota restant est
   au-dessus du seuil de réserve.

`connected` n'est jamais synonyme d'utilisable (principe I).

## 7. Cas d'usage

### User Story 1 - Client servi sans 429 (Priority: P1)
Given une IP de réserve prouvée, When un client envoie une requête, Then le
proxy convertit le corps vers le profil officiel, vérifie `wire_fp`, route vers
l'IP de réserve, et renvoie 200 (SSE compris). Si aucun corps conforme n'est
constructible, il refuse localement en 400/422 explicite **sans** toucher
l'egress.

**Why this priority**: sans 200 client, le reste de la flotte n'a aucune
valeur opérationnelle.

**Independent Test**: réserve ≥ 1, requête `/v1/messages` en stream, 200 avec
contenu et aucun 429.

**Acceptance Scenarios**:

1. **Given** une réserve ≥ 1, **When** un client appelle, **Then** la requête
   part sur une IP `proven` du même `wire_fp` et renvoie 200.
2. **Given** aucun corps conforme constructible, **When** le client appelle,
   **Then** 422 local, aucun egress.
3. **Given** aucune capacité, **Then** 503 `capacity_exhausted` avec le reset
   exact, jamais un 429 synthétique.

### User Story 2 - Quatre providers activables (Priority: P1)
Given la configuration, When l'opérateur active NordVPN seul, WARP seul, proxys
seuls, direct seul, ou une combinaison, Then le pool ne sert que les providers
activés, la capacité est recomputée par bucket, et un provider désactivé n'est
plus utilisé en moins de 60 s.

**Why this priority**: l'opérateur doit isoler un provider défaillant sans
redéploiement ni redémarrage.

**Independent Test**: activer chaque provider seul puis combiné ; désactiver et
vérifier l'arrêt en moins de 60 s.

**Acceptance Scenarios**:

1. **Given** NordVPN seul, **When** un client appelle, **Then** aucun egress
   WARP, proxy ou direct.
2. **Given** un provider désactivé, **When** 60 s s'écoulent, **Then** plus
   aucune sélection et l'état `disabled` est exposé.
3. **Given** deux providers actifs, **Then** la capacité est la somme des
   buckets distincts, sans double comptage.

### User Story 3 - IP jamais brûlée par le proxy (Priority: P1)
Given une IP prouvée, When le proxy prépare une requête, Then le corps émis est
byte-compatible avec la capture officielle (mêmes clés, ordre, outils, UA,
en-têtes) ; un `wire_fp` non conforme est rejeté en amont par le garde.

**Why this priority**: une IP brûlée ne revient pas avant 24 h ; un seul
corps non conforme détruit une IP saine.

**Independent Test**: convertir un corps Claude Code et comparer à la capture ;
un corps incompatible doit produire un 422 local sans egress.

**Acceptance Scenarios**:

1. **Given** un corps client, **When** il est converti, **Then** il porte les
   6 clés officielles, les 13 outils et un `wire_fp` stable.
2. **Given** le mode `compat`, **Then** il n'est jamais routé vers un bucket
   `proven`/`serving`.
3. **Given** une nouvelle capture, **Then** `wire_fp` change et les preuves
   de l'ancien format ne sont pas réutilisées.

### User Story 4 - Libération prouvée (Priority: P1)
Given une IP prouvée, When un 429 survient, Then le proxy conserve le corps
amont, classe le 429, et ne libère l'IP que si `quota` avec `reset_at`, ou si
deux lecteurs concordent sur un changement d'IP réel. Un sentinel (`1.2.3.4`,
`0.0.0.0`, documentation) est rejeté et journalisé.

**Why this priority**: une libération non prouvée détruit une IP saine et
déclenche une rotation qui consomme le quota.

**Independent Test**: 429 ambigu → IP conservée ; sentinel `1.2.3.4` → rejeté ;
deux lecteurs concordants → `mismatch`.

**Acceptance Scenarios**:

1. **Given** un 429 classé `quota`, **Then** l'IP passe `spent` avec `reset_at`
   et reste exclue jusqu'au reset.
2. **Given** un 429 ambigu, **Then** l'IP prouvée est conservée.
3. **Given** une lecture sentinel, **Then** elle est rejetée et journalisée.
4. **Given** deux lecteurs concordants, **Then** `mismatch` avec `evidence_ref`.

### User Story 5 - Rotation bornée (Priority: P2)
Given une flotte saine, When une station a besoin d'une rotation, Then une seule
rotation démarre, les autres demandes sont coalescées, et le budget de sondes
de l'IP neuve est de 2 au plus.

**Why this priority**: le farming d'IP consomme le quota et stérilise la
flotte ; une rotation non bornée est auto-destructrice.

**Independent Test**: 10 demandes en rafale → 1 chaîne, 9 coalescées ;
≤ 6 rotations/heure.

**Acceptance Scenarios**:

1. **Given** 10 demandes simultanées, **Then** une seule rotation démarre.
2. **Given** une rotation récente, **Then** une nouvelle est refusée sauf
   `burned`/`mismatch` prouvé.
3. **Given** une IP neuve, **Then** au plus 2 sondes sont envoyées.

### User Story 6 - Observabilité actionnable (Priority: P2)
Given une fenêtre de 24 h, When l'opérateur consulte le dashboard, Then il voit
par provider : buckets prouvés, brûlés, épuisés, rotations, sondes, 429 client
classés, et le motif exact de chaque libération.

**Why this priority**: sans classification ni preuve, l'opérateur ne peut
pas distinguer un quota d'une brûlure de format.

**Independent Test**: consulter `/api/egress/state`, `/api/egress/incidents` et
`/api/egress/429` ; chaque ligne porte une classe et une preuve.

**Acceptance Scenarios**:

1. **Given** une fenêtre de 24 h, **Then** les états sont visibles par
   provider et par bucket.
2. **Given** un 429, **Then** sa classe et le corps amont sont conservés.
3. **Given** une libération, **Then** `evidence_ref` est obligatoire.

### User Story 7 - Chaque client parle son protocole (Priority: P1)
Given un client de la matrice (Claude Code, Codex, Kilo Code, Hermes Agent,
DeepSeek, Aider, SDK OpenAI, ou un client inconnu), When il envoie sa requête
dans son propre format, Then le proxy la convertit vers le wire officiel, route
vers un bucket prouvé du même `wire_fp`, et renvoie une réponse dans le format
d'origine (SSE compris), y compris les appels d'outils.

**Why this priority**: les clients réels parlent trois protocoles
différents ; un seul format ne peut pas les servir.

**Independent Test**: 3/3 200 par client, en stream et non-stream, sur une IP
neuve prouvée.

**Acceptance Scenarios**:

1. **Given** Claude Code, **Then** `/v1/messages` renvoie la séquence SSE
   Anthropic complète.
2. **Given** Codex, **Then** `/v1/responses` renvoie `response.*`.
3. **Given** un client inconnu, **Then** il tombe dans `custom` ou reçoit un
   422 local, jamais un egress non conforme.
4. **Given** un nouvel outil client, **Then** le mapping se fait par données,
   sans nouveau code.

### User Story 8 - Politique d'outils explicite (Priority: P1)
Given un client qui déclare `Read`, `Bash`, `todowrite`, un outil MCP ou un nom
inconnu, When le corps est converti, Then les outils autorisés par la politique
sont déclarés, les interdits ne partent jamais, les noms sont mappés vers la
grille officielle, et les appels revenus sont re-mappés vers le nom client. Un
appel à un outil non déclaré n'est jamais relayé tel quel.

**Why this priority**: le gate exige la grille officielle et refuse
certaines combinaisons ; un outil mal mappé brûle l'IP.

**Independent Test**: `Read`/`Bash`/`TodoWrite`/outil MCP → wire conforme et
retour au nom d'origine.

**Acceptance Scenarios**:

1. **Given** `Read`, **Then** `read` officiel est déclaré avec un schéma
   exécutable.
2. **Given** un outil interdit, **Then** il ne part jamais vers l'amont.
3. **Given** un appel à un outil inconnu, **Then** il est rendu en texte ou
   refusé, jamais relayé tel quel.

### User Story 9 - Fidélité de flux et d'erreurs (Priority: P1)
Given un flux SSE par famille (Anthropic `message_start`/`content_block_*`,
OpenAI `delta`/`[DONE]`, Responses `response.*`), When le proxy relaie, Then
aucun événement n'est perdu, dupliqué ou réordonné, l'usage final est présent,
et une erreur amont est traduite dans le format du client avec son statut réel.

**Why this priority**: un événement SSE manquant casse le client même si
le modèle a répondu correctement.

**Independent Test**: 100 flux par famille, séquences complètes et usage final.

**Acceptance Scenarios**:

1. **Given** un flux Anthropic, **Then** `message_start` → `content_block_*` →
   `message_delta` → `message_stop` est complet.
2. **Given** un flux OpenAI chat, **Then** `[DONE]` et l'usage final sont
   présents.
3. **Given** une erreur mid-stream, **Then** elle est traduite dans le format
   du client et le flux se termine proprement.

### User Story 10 - Providers interchangeables sans changer le wire (Priority: P2)
Given une requête convertie, When elle part via NordVPN, WARP, un proxy ou le
direct, Then le corps, les en-têtes et l'empreinte TLS observés sont identiques
(CONNECT ne modifie pas le ClientHello vers l'origine), et seul le bucket
change.

**Why this priority**: si le provider modifie l'empreinte observée,
changer de provider revient à changer d'identité.

**Independent Test**: même requête via les quatre chemins → même JA3/JA4, même
ordre d'en-têtes, même `wire_fp`.

**Acceptance Scenarios**:

1. **Given** les quatre providers, **Then** `wire_fp` est identique.
2. **Given** un CONNECT de proxy, **Then** le ClientHello vers l'origine est
   inchangé.
3. **Given** un provider désactivé, **Then** aucune requête ne l'utilise.

### Edge Cases

- Un 429 arrive au milieu d'un flux SSE : le flux client reçoit son événement
  d'erreur, l'IP est classée, aucun événement n'est dupliqué.
- Le client se déconnecte pendant le stream : l'egress est annulé, le bucket
  n'est pas rejoué.
- Un corps dépasse `max_wire_bytes` : 413 local, jamais un envoi tronqué.
- Une image est envoyée à un client sans capacité vision : 422 local explicite.
- Un outil inconnu est appelé par le modèle : healing en texte ou refus selon
  la politique, jamais relayé tel quel.
- Deux lecteurs d'IP ne concordent pas : aucun `mismatch`, état inchangé,
  incident journalisé.
- Une lecture renvoie un sentinel (`1.2.3.4`) : rejeté, aucune libération.
- Un provider est désactivé pendant un flux : le flux en cours finit, aucune
  nouvelle sélection.
- Une rotation démarre pendant un stream : l'IP actuelle finit la requête.
- WARP IPv4 est épuisé : capacité 0, bascule sur un `/64` ou un autre provider.
- Un proxy meurt : liveness, denylist, bascule ; jamais de réserve.
- `enforce_vpn_only` est actif : le direct est refusé avant tout egress.
- Un client publie une nouvelle version : `wire_fp` change, l'ancien bucket
  n'est pas réutilisé avec le nouveau format.

## 8. Requirements

### Functional Requirements

- **FR-001** — Un unique constructeur de wire (`wire_profile`) produit le corps
  officiel ; les clés, l'ordre, les 13 outils, l'UA et les 16 en-têtes sont
  dérivés de la capture active. Aucun autre chemin ne peut composer un corps
  envoyé à une IP prouvée.
- **FR-002** — Le chemin client distingue `convert` (corps client → wire
  officiel, outils mappés) et `compat` (corps déclaré non officiel, interdit
  sur IP prouvée). Le mode est visible dans les logs et la réponse.
- **FR-003** — `wire_fp` (sha256 normalisé du corps + UA + ordre d'en-têtes +
  TLS) est calculé avant l'envoi et stocké avec chaque preuve, chaque 429 et
  chaque brûlure.
- **FR-004** — Un registre `ip_state` persiste `unproven|proven|serving|spent|
  burned|mismatch`, avec `proven_at`, `reset_at`, `burned_at`, `wire_fp`,
  `provider`, `bucket_key`, `reason`, `evidence_ref`.
- **FR-005** — Un 429 est classé avant toute décision : `format`
  (`FreeUsageLimitError` sur IP jamais prouvée avec ce `wire_fp`), `quota`
  (`FreeUsageLimitError` avec `reset_at`), `session`, `upstream`. Le corps
  amont brut est conservé (jamais vide).
- **FR-006** — Une IP `burned` est exclue jusqu'à `burned_at + 24 h` et jusqu'au
  reset fournisseur ; aucun test de format ne la réutilise.
- **FR-007** — Une IP prouvée n'est sondée qu'après `probe_cooldown_s` et
  jamais par une sonde cliente ; le budget `probe_budget_per_ip` est débité
  d'un ledger distinct du ledger client.
- **FR-008** — Les providers sont des adapters de première classe
  (`nordvpn`, `warp`, `proxies`, `direct`) avec capacités déclarées :
  `can_rotate`, `bucket_key`, `quota_source`, `wire_profile`, `health_source`.
- **FR-009** — Chaque provider a un `enabled` indépendant, un poids, et un
  contrat de désactivation : arrêt de sélection immédiat, arrêt des sondes
  d'entretien après `drain_s`, état `disabled` exposé.
- **FR-010** — Le direct est un provider honnête : bucket partagé, pas de
  rotation, 429 exact relayé avec son `reset_at` ; il n'est jamais présenté
  comme une capacité de réserve.
- **FR-011** — WARP compte ses buckets par `/64` IPv6 ; l'IPv4 WARP est une NAT
  partagée et ne crée pas de capacité.
- **FR-012** — Un seul réparateur : le superviseur. Les demandes de réparation
  sont coalescées par `(provider, station)` et bornées par
  `rotation.min_interval_s` (sauf `burned`/`mismatch` prouvé).
- **FR-013** — Une lecture d'IP non globale, sentinel, documentation ou
  `unknown` n'est jamais adoptée ; deux lecteurs doivent concorder pour un
  `mismatch` libératoire.
- **FR-014** — `client_reserve` est calculée depuis les IP `proven` non
  sondées ; une IP `serving` qui reçoit un 429 `quota` sort de la réserve sans
  être re-sondée avant `reset_at`.
- **FR-015** — Le proxy ne fabrique jamais de 429 : s'il n'a aucune capacité,
  il renvoie 503 `capacity_exhausted` avec le plus proche `reset_at` exact, ou
  429 seulement si l'amont a réellement répondu 429 (corps conservé).
- **FR-016** — La configuration unifie `egress.providers.*` et conserve une
  migration depuis `proxy_mode`/`egress_providers` (une seule vérité runtime).
- **FR-017** — Toute option (provider, stratégie, poids, budgets, seuils) a un
  consommateur runtime, un effet observable et un test `option_map`.

- **FR-018** — Un registre de clients déclaratif (`free/clients/registry.json`)
  porte pour chaque client : famille de protocole, endpoints, mode d'auth,
  streaming, capacités, politique d'outils, profil wire. Ajouter un client est
  une donnée, pas du code.
- **FR-019** — Trois familles de protocole et une famille `custom` :
  `anthropic_messages`, `openai_chat`, `openai_responses`, `passthrough` ; une
  famille = un adapter unique, pas un chemin par marque.
- **FR-020** — Endpoints servis : `/v1/messages` (+ `/v1/messages/count_tokens`),
  `/v1/chat/completions`, `/v1/responses`, en stream et non-stream.
- **FR-021** — Auth entrante acceptée : `x-api-key` (Anthropic) et
  `Authorization: Bearer` (OpenAI) ; aucune de ces valeurs n'est réémise vers
  l'amont, qui ne voit que le wire officiel.
- **FR-022** — Fidélité SSE par famille : types d'événements Anthropic,
  `delta`/`[DONE]` OpenAI, `response.*` Responses ; pas de buffering, erreur
  mid-stream traduite, bloc d'usage final conservé.
- **FR-023** — Politique d'outils par client : autorisés (grille officielle),
  interdits (`subagent_fork` et liste configurable), mappés
  (client → officiel), shims bornés ; `tool_choice` normalisé (jamais `none`
  vers l'amont).
- **FR-024** — Retour d'outils : `tool_use`/`tool_calls` re-mappés vers le nom
  client d'origine, identifiants préservés ; un appel non déclaré est rendu en
  texte ou refusé selon la politique, jamais relayé tel quel.
- **FR-025** — Raisonnement : `reasoning_content` (DeepSeek/GLM), blocs
  `thinking` Anthropic et `reasoning.effort` Responses sont préservés et
  convertis par famille.
- **FR-026** — Multimodal : images/fichiers (`b64`, URL) acceptés si la
  capacité est déclarée ; sinon 422 local explicite, jamais une troncature
  silencieuse.
- **FR-027** — Contexte : budget `keep_tokens` et compaction par famille ;
  jamais de dépassement du budget du gate ; erreur locale si insuffisant.
- **FR-028** — Erreurs : mapping amont → client par famille (statut, type,
  code, `Retry-After`) ; jamais de 429 synthétique ; 503
  `capacity_exhausted` avec le reset exact si aucune capacité.
- **FR-029** — Identité par client : session, trace, `project` et
  `prompt_cache_key` stables par conversation et par IP, jamais partagés entre
  IP ou entre clients.
- **FR-030** — `wire_fp` du corps converti comparé à l'état du bucket ;
  `compat` ne cible jamais un bucket `proven`/`serving`.
- **FR-031** — Fixtures golden par client (requête, réponse, SSE) et snapshot
  du wire converti ; toute dérive échoue la gate.
- **FR-032** — Les quatre providers respectent le même wire et la même
  admission ; le direct et les proxys sont `quota_source=shared`.
- **FR-033** — WARP : buckets `/64` IPv6, IPv4 NAT capacité 0, provisioning et
  enregistrement, santé par endpoint, rotation bornée.
- **FR-034** — Proxys : qualification liveness/anonymat/latence, denylist,
  bucket par endpoint, isolation, `max_active`, rotation de sources.
- **FR-035** — Direct : 200 et 429 honnêtes, jamais compté en réserve,
  interaction explicite avec `enforce_vpn_only`.
- **FR-036** — Empreinte TLS par provider : le ClientHello observé à l'origine
  ne dépend pas du provider (le CONNECT ne le modifie pas) ; un test compare
  JA3/JA4 et l'ordre des en-têtes sur les quatre chemins.
- **FR-037** — Concurrence : limite par bucket, hedging borné, aucun retry sur
  un 429 (il brûlerait une IP), aucun doublon sur un bucket épuisé.
- **FR-038** — Performance : surcoût proxy p50 < 30 ms hors génération, TTFB
  non dégradé, pool de connexions par `(proxy, impersonate)`.
- **FR-039** — Observabilité par client (requêtes, 200, classes d'erreur,
  latence, `wire_fp`) et par provider (buckets, sondes, rotations).
- **FR-040** — Rollback : `egress.wire.client_mode`, `clients.<id>.enabled` et
  les flags de provider s'appliquent sans redéploiement.
- **FR-041** — Un client inconnu tombe dans `custom` (OpenAI chat par défaut)
  et ne casse jamais le proxy.
- **FR-042** — Documentation `docs/CLIENTS.md` : matrice, capacités, procédure
  d'ajout d'un client, exemples de corps par famille.

### Key Entities

- **IPState** : état d'une IP (`unproven|proven|serving|spent|burned|mismatch`),
  `provider`, `bucket_key`, `wire_fp`, `proven_at`, `serving_at`, `reset_at`,
  `burned_at`, `reason`, `evidence_ref`, `probe_count`.
- **Bucket** : unité de quota (`/32` NordVPN, `/64` WARP IPv6, `host:port`
  proxy, IP hôte direct) ; porte la capacité et le prochain reset.
- **WireProfile** : capture active (corps, en-têtes, outils, TLS) et
  `wire_fp` ; une version par release officielle.
- **ClientProfile** : famille de protocole, endpoints, auth, capacités,
  politique d'outils, sémantique d'identité.
- **ToolPolicy** : grille officielle, interdits, alias, shims, plafond
  d'extras, comportement pour un outil inconnu.
- **ProbeBudget** : compteur de sondes par bucket, séparé du ledger client.
- **Evidence** : preuve horodatée (200 avec contenu, 429 classé, capture A/B)
  référencée par `evidence_ref`.
- **Incident** : libération, rotation, burn, mismatch, avec cause et preuve.
- **ProviderCapabilities** : `can_rotate`, `bucket_key`, `quota_source`,
  `health_source`, `wire_profile`.

### Non-functional Requirements

- **NFR-001** — Aucune I/O réseau dans le chemin chaud hors envoi egress ; les
  lectures d'IP, sondes et réconciliations sont en tâche de fond.
- **NFR-002** — `wire_fp` et la conversion client < 1 ms, sans allocation
  notable (profiling sur 10 000 corps).
- **NFR-003** — Gates obligatoires : `pytest -k "not docker"`,
  `config_audit --strict`, `platform_audit --strict`, `test_option_map.py`,
  matrice Windows/Linux.
- **NFR-004** — Aucun état perdu au redémarrage : `ip_state`, réserves, budgets
  et `reset_at` sont persistés et restitués.
- **NFR-005** — Aucune fenêtre intempestive (principe XVI) ; tout processus
  passe par `run_hidden`.

## Success Criteria

### Measurable Outcomes

- **SC-001** — 0 429 client sur 24 h tant que `client_reserve ≥ 1` (mesuré sur
  le ledger client, pas sur les sondes).
- **SC-002** — 0 IP prouvée relâchée sans preuve dure sur 24 h ; chaque
  libération a `evidence_ref` et un ou deux lecteurs concordants.
- **SC-003** — ≤ 6 rotations/heure au total, ≤ 1/10 min/station hors
  `burned`.
- **SC-004** — 100 % des 429 stockés avec corps amont non vide et classe ;
  0 `response_body` vide.
- **SC-005** — Test A/B de wire : une IP neuve par variante, ≥ 20 échantillons
  par variante, même fenêtre ; la variante conforme ne régresse pas par
  rapport au baseline (principe XVIII).
- **SC-006** — Les quatre providers, activés seuls puis combinés, servent au
  moins une requête client 200 avec contenu ; désactivés, ils ne servent plus
  aucune requête en < 60 s.
- **SC-007** — `direct` activé : 200 client reproductible et 429 honnête avec
  `reset_at` exact quand le bucket partagé est épuisé.

- **SC-008** — Chaque client de la matrice obtient 3/3 `200` avec contenu, en
  stream et non-stream, sur une IP neuve prouvée.
- **SC-009** — Golden wire par client : diff normalisé vide contre la référence
  (seuls IDs, trace et longueur varient), `wire_fp` stable.
- **SC-010** — 0 appel d'outil non déclaré relayé ; 0 `tool_choice: none`
  envoyé à l'amont ; 100 % des appels revenus portent le nom client d'origine.
- **SC-011** — 0 corps client incompatible envoyé à un bucket
  `proven`/`serving` (compteur d'admission = 0).
- **SC-012** — SSE fidèle : 100 flux par famille sans événement manquant,
  dupliqué ou réordonné ; usage final présent dans 100 % des cas.
- **SC-013** — Le `wire_fp` et l'empreinte TLS observés sont identiques sur les
  quatre providers pour une même requête convertie.
- **SC-014** — `docs/CLIENTS.md` et `option-map` complets : chaque client et
  chaque provider ont un contrat testé.

## Assumptions

- Le quota free est par IP (bucket `/32` NordVPN, `/64` WARP IPv6), pas par
  compte ; le `Retry-After` fournisseur est la seule vérité de reset.
- Une IP brûlée par format reste brûlée ≥ 24 h (principe XVIII, preuve du
  2026-11-17).
- La capture 2.3.0 (`free/captures/2.3.0.json`) reste la référence de wire ;
  une nouvelle release invalide `wire_fp` et exige un re-test à IP neuve.
- Les proxys publics sont non fiables par nature ; ils sont un fallback, pas
  une capacité de réserve garantie.

## Risks

- **R1** — Le gate amont change sans préavis : mitigation = `wire_fp` +
  détection de dérive + A/B à IP neuve.
- **R2** — Le direct et certains proxys partagent leur bucket avec d'autres
  utilisateurs : mitigation = classification `quota` et attente honnête, jamais
  de promesse de réserve.
- **R3** — La limitation de sessions NordVPN borne les rotations : mitigation =
  rotation bornée, transports multiples, réutilisation après reset seulement.
- **R4** — Un bug de conversion client pourrait refuser du trafic légitime :
  mitigation = mode `observe` (log du diff, pas de blocage) avant `enforce`.
