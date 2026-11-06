# Feature Specification: Parité wire officielle de la jambe free

**Feature Branch**: `003-official-client-parity`  
**Created**: 2026-11-05  
**Status**: Draft  
**Input**: Audit `docs/AUDIT_IMPERSONATION_FORMAT_2026-11-05.md` + demande opérateur « auditer l'impersonation et le format des requêtes, puis fournir un plan de divergence ».  

## Contexte et problème

La jambe free doit se présenter à la passerelle comme **une installation OpenCode officielle**. Aujourd'hui, elle combine plusieurs clients :

- le corps et l'UA du client **2.2.5** (`free/official_payload.json`, `free/impersonation.json`) ;
- les identités et les TLS des clients **1.18.31 / 1.19.0 / 1.19.3 / 1.19.5** (`free/identities.py`) ;
- un ordre d'en-têtes 1.19.x sur un jeu d'en-têtes 2.2.5 ;
- des outils « placeholder » pour les clients non officiels.

Le résultat n'est ni un client 2.2.5, ni un client 1.19.5. C'est un hybride. L'audit du 2026-11-05 a mesuré :

- 2 lignes officielles incompatibles (npm 1.19.5 = 13 en-têtes / 11 outils / `max_tokens` ; desktop-zip 2.2.5 = 16 en-têtes / 13 outils / `max_completion_tokens`) ;
- 4 en-têtes de tracing/session émis **après `Content-Length`** au lieu des positions 4, 5, 10, 11 ;
- le project `global` remplacé par le hash du dépôt réel ;
- le chemin absolu du workspace de l'opérateur dans le prompt système ;
- un trace ID régénéré à chaque requête ;
- une session tournante toutes les 30 minutes ;
- des placeholders `properties: {}` / « DO NOT CALL » que les mesures internes du projet décrivent comme refusés en 403.

Le TLS, en revanche, est exact pour 1.18.31 et 1.19.5. Le problème est la **cohérence**, pas la capacité à rejouer une empreinte.

## Incident en cours (2026-11-05)

La jambe free est signalée **down** : l'opérateur ne voit que des 429 « quota dépassé », alors que le quota n'est pas prouvé épuisé. L'hypothèse historique est qu'une impersonation incomplète produit aussi un faux 429.

Les traces locales de 24 h montrent que les 429 récents sont des **rate-limits courts** (`Retry after 42–58s`) sur `jev-1.13-free`, pas le bucket journalier (`FreeUsageLimitError`, `Retry-After` de plusieurs heures). Une partie de ces 429 est générée **par le proxy lui-même** (`model_global_remaining`), sans requête amont : c'est un cooldown local, pas un quota fournisseur. Le trafic client réel n'apparaît plus depuis le 2026-11-04 20:28 UTC. Le 2026-11-06, le token NordVPN a été accepté et les credentials régénérés ; l'`AUTH_FAILED` restant est une **limite de sessions simultanées** (six stations + un test = refus, un seul test = auth OK), et l'IP testée `87.120.102.86` est en quota. Le runbook `docs/PLAN_RETABLISSEMENT_FREE_2026-11-05.md` reste prioritaire : il classe le 429 et la limite de sessions avant toute rotation ou tout changement de wire.

## Objectifs

1. Chaque requête free doit être une **installation officielle cohérente** : un seul canal, une seule version, un seul UA/client, un seul TLS, un seul template de corps, un seul ordre d'en-têtes.
2. Le wire émis doit être **vérifiable par diff** contre une capture du client officiel, pas par inspection du code.
3. Aucune requête sortante ne doit contenir le chemin réel, le project réel du dépôt, ou un identifiant partagé entre IP.
4. L'outil de synchronisation doit détecter une nouvelle release et échouer si le wire diverge.
5. La compatibilité avec les clients tiers (Claude Code, Hermes, etc.) doit rester explicite et mesurée, pas mélangée à l'impersonation.

## Non-objectifs

- Contourner le quota ou le gate par un autre moyen que la parité de forme.
- Modifier la jambe payante.
- Supporter `/responses` 2.2.5 dans cette itération (non capturé ; à traiter séparément).
- Imiter plusieurs versions simultanément tant que les profils complets ne sont pas disponibles.
- Changer le moteur de rotation d'IP ou l'allocateur.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Une requête = une installation officielle (Priority: P1)

Un client appelle la jambe free. Le proxy choisit une identité (IP, version, canal, distribution) et émet **un wire entier** cohérent : UA, client, TLS, ordre des en-têtes, template de corps et IDs appartiennent au même profil. Un diff normalisé contre la capture officielle ne montre aucune différence structurelle.

**Why this priority**: c'est la promesse de l'impersonation. Un hybride est détectable même si le gate actuel tolère certaines combinaisons.

**Independent Test**: capturer le wire réel du proxy sur le chemin per-IP (`curl_cffi` → serveur local), le normaliser (IDs/trace/longueur variables), le comparer à `2.2.5-1.json`. Le diff doit être vide sur les noms, l'ordre, les clés de corps, les outils et les schémas.

**Acceptance Scenarios**:

1. **Given** un profil 2.2.5 `cli`, **When** une requête part, **Then** l'ordre est `Authorization, Content-Type, User-Agent, b3, traceparent, x-opencode-client, x-opencode-project, x-opencode-session, x-opencode-session-id, x-session-affinity, x-session-id, Connection, Accept, Host, Accept-Encoding, Content-Length`.
2. **Given** un profil 2.2.5, **When** le corps est construit, **Then** les clés sont `model, messages, tools, stream, stream_options, max_completion_tokens`, 13 outils officiels, pas de `tool_choice`.
3. **Given** un profil 1.19.5 (si activé), **When** une requête part, **Then** l'ordre et le corps sont ceux de 1.19.5 (13 en-têtes, 11 outils, `max_tokens`, `tool_choice: auto`), pas un mélange avec 2.2.5.
4. **Given** un changement de version dans le vault, **When** le profil est résolu, **Then** UA, client, TLS, ordre, corps et IDs changent ensemble.

### User Story 2 — Aucune fuite d'identité réelle (Priority: P1)

Un opérateur inspecte une requête free sortante. Il n'y trouve ni le chemin absolu de son workspace, ni le hash de son dépôt, ni un project ID partagé entre plusieurs IP.

**Why this priority**: la séparation par IP est un invariant du projet ; une fuite annule le bénéfice de la rotation.

**Independent Test**: lancer une requête avec `base_project=global` et vérifier que `x-opencode-project` vaut `global` (ou un dérivé par IP), et que le prompt système ne contient aucun chemin réel.

**Acceptance Scenarios**:

1. **Given** une identité hors dépôt git, **When** la requête part, **Then** `x-opencode-project` est `global` ou un dérivé `sha256("global|ip")[:40]`, jamais le hash du dépôt de l'opérateur.
2. **Given** un payload 2.2.5, **When** le prompt est injecté, **Then** `Today's date` est la date UTC courante et `Working directory` est un chemin synthétique propre au profil.
3. **Given** deux IP différentes, **When** elles envoient la même conversation, **Then** aucun `ses_`, `msg_`, `project`, trace ID ou chemin de prompt n'est partagé.

### User Story 3 — Le sync détecte les releases et les dérives (Priority: P2)

L'opérateur lance `python scripts/opencode_client_sync.py --check`. L'outil compare le client installé (desktop et npm) au manifeste, capture les requêtes template (titre + principal), et échoue si l'ordre, les clés, les outils, l'UA ou le TLS ont changé.

**Why this priority**: sans outil fiable, la dérive recommence à la prochaine release.

**Independent Test**: modifier artificiellement un champ du manifeste et vérifier que `--check` échoue avec un diff lisible.

**Acceptance Scenarios**:

1. **Given** le client desktop 2.2.5, **When** `--check` tourne, **Then** il ne passe pas `--standalone` au CLI npm et capte les deux canaux séparément.
2. **Given** une nouvelle release, **When** `--check` détecte un changement, **Then** il écrit un diff (en-têtes, ordre, corps, outils, TLS) et retourne un code non nul.
3. **Given** un manifeste incomplet (client/project/header_order absents), **When** `--check` tourne, **Then** il échoue avant d'appliquer.

### User Story 4 — Les clients tiers restent supportés sans polluer l'impersonation (Priority: P2)

Un client Hermes/Claude Code envoie ses propres outils et son propre prompt. Le proxy peut soit (a) envoyer un wire officiel exact et mapper les outils, soit (b) envoyer un wire de compatibilité explicitement marqué, avec des définitions réalistes et une mesure de passage.

**Why this priority**: la majorité du trafic n'est pas le client officiel ; il faut un chemin clair, pas un hybride silencieux.

**Independent Test**: envoyer un corps Hermes et vérifier que le mode choisi est visible dans les logs, que les outils officiels ont des schémas réalistes, et que le corps n'est pas un mélange non documenté.

**Acceptance Scenarios**:

1. **Given** un client sans prompt officiel, **When** le mode `compat` est actif, **Then** les 13 outils officiels ont leurs descriptions/schémas complets (pas de `properties: {}`) et les outils client sont ajoutés après.
2. **Given** un client sans outil, **When** le mode `compat` est actif, **Then** `tool_choice` est `auto` et la note « no tools » est présente, comme documenté.
3. **Given** un client officiel (prompt + `read`), **When** le corps passe, **Then** il n'est pas modifié.

## Requirements

### Functional

- **FR-001** : un `FreeWireProfile` unique doit porter `channel`, `version`, `distribution`, `client`, `ua`, `tls`, `header_order`, `header_template`, `body_template`, `request_id_header`, `trace_mode`, `session_mode`.
- **FR-002** : `_official_free_headers` et `_free_wire_body` doivent recevoir le profil ; ils ne doivent plus choisir l'UA, le client ou le template indépendamment.
- **FR-003** : l'ordre des en-têtes 2.2.5 doit inclure `b3` et `traceparent` en positions 4–5, et le quartet de session en 8–11.
- **FR-004** : le corps 2.2.5 doit être `model, messages, tools, stream, stream_options, max_completion_tokens`, avec 13 outils officiels et sans `tool_choice`.
- **FR-005** : le corps 1.19.5, si le profil existe, doit être `model, max_tokens, messages, tools, tool_choice, stream, stream_options`, avec 11 outils.
- **FR-006** : le project `global` ne doit jamais être remplacé par le hash du dépôt de l'opérateur.
- **FR-007** : le prompt système doit être templaté : date UTC, cwd synthétique, plateforme et « git repo » cohérents avec le profil.
- **FR-008** : le trace ID doit être stable par session ; le span ID doit être neuf par requête.
- **FR-009** : la session doit rester stable par conversation ; la rotation de 30 min doit être un kill-switch, pas le défaut.
- **FR-010** : les templates `title` et `compaction` doivent être capturés et sélectionnés par type de requête.
- **FR-011** : le mode `compat` doit utiliser des définitions d'outils réalistes ; les placeholders `properties: {}` sont interdits sans mesure A/B qui les valide.
- **FR-012** : `scripts/opencode_client_sync.py` doit supporter les canaux desktop et npm, capturer toutes les requêtes template, persister l'ordre, le client, le project et le TLS, et échouer si le manifeste est incomplet.
- **FR-013** : un test end-to-end doit capturer le wire réel du proxy sur le chemin per-IP et le comparer à la capture officielle.
- **FR-014** : un test de cohérence doit vérifier le tuple `(version, UA, client, TLS, header_order, body_keys, tools)`.
- **FR-015** : un mode `observe` doit pouvoir journaliser le diff sans bloquer le trafic.

### Non-functional

- **NFR-001** : aucun chemin absolu de l'opérateur ne doit apparaître dans une requête sortante.
- **NFR-002** : aucun identifiant (session, project, trace) ne doit être partagé entre deux IP.
- **NFR-003** : le diff de wire doit être déterministe et reproductible hors ligne.
- **NFR-004** : le coût du profil doit rester négligeable (< 1 ms par requête, pas d'I/O sur le chemin chaud).
- **NFR-005** : le rollback doit être possible par configuration (`OPENCODE_FREE_WIRE_STYLE`, `OPENCODE_CLIENT_VERSION`), sans redéploiement.

## Key Entities

- **FreeWireProfile** : version, canal, distribution, client, UA, TLS, ordre, template de corps, sémantique des IDs.
- **Identity** : IP, project, session, message, trace, génération.
- **Capture** : requête officielle brute (headers ordonnés, body brut, hash, type de requête).
- **Manifest** : spec versionnée qui référence les captures et les templates.
- **WireDiff** : résultat normalisé de la comparaison proxy ↔ officiel.

## Success Criteria

- **SC-000** : la jambe free rend un 200 avec contenu sur une IP, puis 10/10, puis les six stations sont client-utilisables ; zéro `FreeTierError` et zéro 502 sur le chemin client.
- **SC-001** : le diff normalisé entre le proxy 2.2.5 `cli` et `2.2.5-1.json` est vide sur les noms, l'ordre, les clés, les outils et les schémas.
- **SC-002** : aucune requête sortante ne contient le chemin réel ou le project réel du dépôt.
- **SC-003** : le trace ID est identique sur deux requêtes d'une même session ; le span diffère.
- **SC-004** : `--check` détecte une modification artificielle de l'UA, de l'ordre ou d'un outil.
- **SC-005** : l'A/B gate ne montre pas de régression de 200 par rapport au baseline (échantillon ≥ 20 par variante, même IP, même fenêtre).
- **SC-006** : les tests de parité existants sont mis à jour et passent ; le test end-to-end per-IP passe.

## Assumptions

- Le free model actuel reste `deepseek-v4.1-flash-free` et utilise `/chat/completions`.
- Le canal canonique est **2.2.5 `cli`** (zip standalone), car le payload et le fixture actuels sont déjà 2.2.5 `cli`. Un profil `desktop` est une option, pas le défaut.
- Le gate peut tolérer l'ordre et le TLS ; l'audit ne le prouve pas. L'A/B est obligatoire avant de déclarer une régression fonctionnelle.
- Le profil 1.19.5 est conservé comme rollback tant que le profil 2.2.5 n'est pas validé en live.
- Les clients tiers sont supportés en mode `compat` explicite, pas en mode « officiel » silencieux.

## Risks

- **R1** : le gate exige le prompt système exact (chemin/date compris). Le templating casserait le 200. Mitigation : A/B, garder le prompt figé en rollback, templater seulement la date d'abord.
- **R2** : les définitions complètes des 13 outils augmentent le corps de ~13 Ko et peuvent dépasser le budget du gate. Mitigation : mesurer, ajuster `free.max_wire_bytes`, garder les placeholders seulement si prouvés.
- **R3** : le profil 2.2.5 `cli` ne correspond pas à l'app desktop installée. Mitigation : `distribution` explicite, deux profils, pas de mélange.
- **R4** : la rotation de version est utilisée pour l'anti-corrélation ; la supprimer réduit la diversité. Mitigation : conserver la rotation d'IP/session, pas la rotation de version, jusqu'à ce que les profils complets existent.
