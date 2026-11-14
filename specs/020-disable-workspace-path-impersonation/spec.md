# Feature Specification: Désactiver l'impersonation des chemins d'accès

**Feature Branch**: `020-disable-workspace-path-impersonation`

**Created**: 2026-11-14

**Status**: Active

**Input**: « J'aimerais que tu me fasses un plan pour désactiver
l'impersonation des chemins d'accès, et je voudrais qu'elle soit actuellement
désactivée. Fais-moi un plan avec SpecKit. »

## Objectif

Le proxy free ne doit plus remplacer le chemin réel du workspace client par une
racine factice `/workspace/oc-<station>-<hash>`. Le provider doit recevoir le
`cwd` réel envoyé par le client, sans substitution de chemin, sans jonction
Windows et sans remap des tool_calls au retour.

La désactivation est **immédiate** (hot reload, sans redémarrage) et **durable**
(valeur par défaut `false`, survit à un redémarrage et à une réécriture de
`config.yaml`). Le reste de l'identité de station (UA, `x-opencode-*`, project,
session, TLS) reste inchangé.

## État actuel constaté

- L'interrupteur existe déjà : `opencode.py::_workspace_path_masking_enabled()`
  lit `FREE_WORKSPACE_PATH_MASKING`, puis `free.workspace_path_masking`
  (`yaml_get`, défaut **true**).
- `config.yaml` ne contenait pas la clé : le mode effectif était donc **activé**.
- Le chemin free applique le masking dans `free/station_identity.py`
  (`sanitize_text`, `template_official_body`, `workspace_env_cwd`,
  `ensure_workspace_alias`, `translate_paths`) via
  `free/runtime_identity.py::build_body`.
- Deux appels restent hors switch : la sonde de profil
  (`free/profile_live.py::build_verification_request`) et le corps de capture
  (`free/runtime_identity.py::_build_capture_body`) utilisent le défaut `True`.
- Le streaming utilise `streaming/tool_paths.py` avec la map
  `_free_path_map` ; quand le masking est off, la map doit rester vide.

**Décision immédiate appliquée** : `free.workspace_path_masking: false` a été
ajouté à `config.yaml` ; le watcher (5 s + debounce 1 s) le prend sans restart.

## Périmètre

1. **Interrupteur** : `free.workspace_path_masking`, défaut `false`, override
   env `FREE_WORKSPACE_PATH_MASKING` conservé.
2. **Corps free** : vrai `cwd`, aucune racine factice, aucun masquage
   `C:\Users\...`.
3. **Jonctions** : plus aucune création de `C:\workspace\oc-*` ; nettoyage
   optionnel des jonctions existantes (uniquement des reparse points).
4. **Retour** : `_free_path_map` vide, `translate_paths` inactif ; le healing
   de schéma (`filePath`/`file_path` → `path`) reste actif car il ne touche pas
   au chemin.
5. **Sondes** : la sonde post-rotation et les corps de capture obéissent au même
   interrupteur.
6. **Artefacts historiques** : lorsqu'un corps contient encore un ancien
   `/workspace/oc-*` (historique de conversation, capture), il est normalisé
   vers le `cwd` réel en mode off — aucun chemin factice ne doit sortir.
7. **Observabilité** : mode effectif visible (`/api/config`) et journalisé une
   fois au boot et au changement.
8. **Tests** : unitaires, intégration chaude, preuve live sur le wire.

**Hors périmètre** : suppression de `free/station_identity.py` ou de l'identité
de station ; changement des en-têtes/UA/TLS ; jambe payante ; refonte du
streaming ; modification des six stations.

## Contraintes dures

- Aucun restart du proxy n'est nécessaire pour basculer : le hot reload
  `config.yaml` doit suffire (≤ 6 s).
- Le mode off ne doit pas casser les tool_calls Windows : le modèle doit
  recevoir le vrai chemin et le renvoyer tel quel.
- L'identité de station reste active (`free.station_identity: true`) ; seule la
  substitution de chemin est coupée.
- Un `FREE_WORKSPACE_PATH_MASKING=true` dans l'environnement du process gagne
  sur la config : il doit être absent pour que le mode off soit effectif.
- Aucune suppression de dossier utilisateur : le nettoyage ne touche qu'aux
  jonctions/symlinks créés par le proxy.
- Le quota free reste par IP ; aucun test ne doit conclure sur le pool à partir
  d'une seule IP.

## Scénarios utilisateurs

### User Story 1 — Le free reçoit le vrai chemin (P1)

Un client Windows envoie `<env>Working directory: C:\Users\julie\projet</env>`.
Le proxy transmet ce chemin tel quel au provider. Le modèle appelle `read` avec
`C:\Users\julie\projet\AGENTS.md`, le client l'exécute du premier coup.

**Test indépendant** : requête free avec un prompt contenant un `cwd` réel ;
inspecter le corps sortant et le tool_call retourné.

**Critères d'acceptation** :

1. **Given** `workspace_path_masking=false`, **When** le corps contient
   `C:\Users\julie\projet`, **Then** le corps sortant contient ce chemin et
   aucun `/workspace/oc-`.
2. **Given** le même corps, **When** le modèle émet un tool_call `read`,
   **Then** `path` est le chemin réel, sans remap.
3. **Given** un workspace réel, **When** la requête part, **Then** aucune
   jonction `C:\workspace\oc-*` n'est créée.

---

### User Story 2 — La bascule est immédiate et durable (P1)

L'opérateur écrit `free.workspace_path_masking: false` dans `config.yaml` ;
la requête suivante est en mode off, sans restart. Un redémarrage ou une
réécriture de config conserve le mode off.

**Test indépendant** : modifier la clé, attendre ≤ 6 s, appeler
`_workspace_path_masking_enabled()` dans le process live (ou vérifier
`/api/config`), puis redémarrer et recontrôler.

**Critères d'acceptation** :

1. **Given** la clé à `false`, **When** le watcher recharge, **Then** le mode
   effectif est `false` en ≤ 6 s.
2. **Given** un redémarrage, **When** le proxy boote, **Then** le mode reste
   `false` sans variable d'environnement.
3. **Given** `FREE_WORKSPACE_PATH_MASKING=true`, **When** le process démarre,
   **Then** l'env gagne et le mode est `true` (override documenté).

---

### User Story 3 — Aucun chemin factice ne fuit (P1)

Même avec un historique de conversation ou une capture officielle contenant
`/workspace/oc-*`, le corps sortant en mode off ne contient plus cette racine.

**Test indépendant** : injecter un message historique avec
`/workspace/oc-WARP-1-abcdef12/fichier`, envoyer en mode off, scanner le corps.

**Critères d'acceptation** :

1. **Given** un historique avec `/workspace/oc-*`, **When** le mode est off,
   **Then** le corps sortant ne contient aucun `/workspace/oc-`.
2. **Given** une capture de profil avec un `cwd` factice, **When** la sonde
   part, **Then** elle utilise le vrai `cwd` ou n'injecte aucune racine
   factice.
3. **Given** un corps sans `cwd`, **When** le mode est off, **Then** le proxy
   n'invente ni `<env>` ni racine.

---

### User Story 4 — Le reste de l'identité est intact (P2)

La désactivation ne change ni l'UA, ni les en-têtes `x-opencode-*`, ni le
project/session/trace, ni le TLS. Les six stations restent utilisables.

**Test indépendant** : comparer les en-têtes et le statut free avant/après ;
vérifier 6/6 `client_usable` sur une fenêtre d'observation.

**Critères d'acceptation** :

1. **Given** le mode off, **When** une requête free part, **Then** les en-têtes
   d'identité sont identiques au mode on.
2. **Given** le mode off, **When** la flotte tourne, **Then** aucun statut
   `client_usable` ne régresse à cause du changement.

## Exigences fonctionnelles

- **FR-001** : `free.workspace_path_masking` existe dans `config.yaml`, défaut
  `false`.
- **FR-002** : `FREE_WORKSPACE_PATH_MASKING` garde la priorité sur la config
  (`1/true/yes/on` et `0/false/no/off`).
- **FR-003** : en mode off, `template_official_body` conserve le `cwd` réel et
  ne remplace aucun `C:\Users\...`.
- **FR-004** : en mode off, aucune racine `/workspace/oc-` n'est générée et
  `ensure_workspace_alias` n'est jamais appelé sur le chemin chaud.
- **FR-005** : en mode off, `_free_path_map` reste vide et `translate_paths`
  ne remappe aucun tool_call.
- **FR-006** : le healing de schéma des tool_calls (`filePath`, `file_path`,
  `filename` → `path`) reste actif.
- **FR-007** : la sonde post-rotation et `build_verification_request` reçoivent
  le mode effectif.
- **FR-008** : tout `/workspace/oc-*` résiduel d'un historique ou d'une capture
  est normalisé vers le `cwd` réel en mode off.
- **FR-009** : le mode effectif est exposé par `/api/config` et journalisé au
  boot/changement.
- **FR-010** : la bascule config est visible en ≤ 6 s, sans restart.
- **FR-011** : un utilitaire de nettoyage supprime uniquement les jonctions
  `C:\workspace\oc-*` marquées reparse point.
- **FR-012** : le retour en mode on se fait par config/env, sans changement de
  code.

## Exigences non fonctionnelles

- **NFR-001** : zéro ajout de latence mesurable sur le chemin chaud (pas de
  `mklink` par requête).
- **NFR-002** : aucune régression des six stations `client_usable`.
- **NFR-003** : tests unitaires et intégration verts.
- **NFR-004** : la désactivation est un choix explicite : le provider voit le
  nom d'utilisateur Windows réel. Ce trade-off est assumé et documenté.
- **NFR-005** : aucune fuite de secret supplémentaire (les gardes
  `sanitize_free_headers` restent en place).

## Cas limites

- `FREE_WORKSPACE_PATH_MASKING=true` dans l'environnement parental : le mode
  reste on ; le diagnostic doit le signaler.
- Valeur YAML chaîne `"false"` : doit être interprétée comme `false`, pas comme
  booléen truthy.
- Conversation ancienne avec `/workspace/oc-*` : normalisée en mode off.
- `cwd` absent ou non exploitable : aucune racine inventée.
- Workspace sur lecteur réseau ou chemin Unicode : transmis tel quel.
- Deux workspaces ouverts sur la même station : plus de collision de jonction
  puisqu'il n'y a plus de jonction.
- Requête en vol pendant le reload : peut utiliser l'ancien mode ; la suivante
  est correcte.

## Critères de succès

- **SC-001** : le process live expose `workspace_path_masking=false` après
  l'édition de config, sans restart.
- **SC-002** : le corps free sortant contient le vrai `cwd` et zéro
  `/workspace/oc-`.
- **SC-003** : un tool_call `read`/`edit`/`write` revient avec un chemin réel
  et s'exécute du premier coup sous Windows.
- **SC-004** : aucune nouvelle jonction `C:\workspace\oc-*` n'est créée.
- **SC-005** : les en-têtes d'identité de station sont inchangés.
- **SC-006** : 6/6 `client_usable` conservé sur la fenêtre d'observation.
- **SC-007** : le retour au mode on ne demande qu'une clé de config (ou l'env).

## Hypothèses

- Le client envoie toujours un `cwd` réel dans `<env>` ou un indice équivalent.
- Le provider accepte le chemin Windows tel quel dans le prompt et les
  tool_calls (comportement déjà observé sur les modèles free).
- Le hot reload de `config.yaml` reste le mécanisme de bascule ; aucun restart
  n'est requis.
- Le nettoyage des jonctions existantes est optionnel et peut être lancé
  manuellement après acceptance.
