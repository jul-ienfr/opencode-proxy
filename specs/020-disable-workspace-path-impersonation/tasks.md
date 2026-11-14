# Tasks — Désactiver l'impersonation des chemins d'accès

**Feature** : `020-disable-workspace-path-impersonation`
**Input** : `spec.md`, `plan.md`
**Date** : 2026-11-14

Légende : `[P]` = parallélisable ; `P0` = bloquant la release ;
`P1` = requis pour la cible ; `P2` = qualité.
Statut : `fait`, `en cours`, `à faire`.

---

## Phase 0 — Bascule immédiate (fait)

### T100 — Désactiver le mode sur le process live (P0) — fait
- **Fichiers** : `config.yaml`.
- **Action** : ajouter `free.workspace_path_masking: false` sous `free:`.
- **Acceptation** : la clé survit au hot reload ; le process live est en mode
  off sans restart.
- **Preuve** : `_workspace_path_masking_enabled()` hors process = `False` ;
  sonde live `deepseek-v4.1-flash-free` : le modèle rend
  `<env>Working directory: C:\Users\julie\Downloads\opencode-proxy-one</env>`,
  zéro `/workspace` ; tool_call `read` avec
  `C:\Users\julie\Downloads\opencode-proxy-one\AGENTS.md` ; aucune jonction
  créée après 21:16:02.

---

## Phase 1 — Défaut durable et parsing strict

### T101 — Défaut `false` de l'interrupteur (P0) — à faire
- **Fichiers** : `opencode.py::_workspace_path_masking_enabled`.
- **Action** : remplacer le défaut `True` par `False` (yaml et fallback
  exception).
- **Acceptation** : sans clé ni env, le mode est off.
- **Test** : unitaire sans clé.

### T102 — Parsing strict de la valeur YAML (P0) — à faire
- **Fichiers** : `opencode.py::_workspace_path_masking_enabled`.
- **Action** : interpréter `"false"`, `"0"`, `"no"`, `"off"` comme `False`,
  `"true"`, `"1"`, `"yes"`, `"on"` comme `True` ; refuser le `bool("false")`.
- **Acceptation** : une valeur chaîne `"false"` désactive le mode.
- **Test** : unitaire paramétré (bool et chaînes).

### T103 — Documenter la clé (P1) — à faire
- **Fichiers** : `config.yaml` (commentaire), `.env.example`, `README.md`.
- **Action** : documenter la clé, l'override env et le trade-off
  (le provider voit le chemin réel).
- **Acceptation** : un opérateur trouve la clé et sait la remettre à `true`.

### T104 — Vérifier tous les appelants free (P0) — à faire
- **Fichiers** : `opencode.py`, `free/runtime_identity.py`.
- **Action** : inventorier les appels `build_body` / `template_official_body`
  et s'assurer qu'aucun chemin free ne prend le défaut `True` silencieusement.
- **Acceptation** : un seul interpréteur, tous les appels free explicites.
- **Test** : grep + test d'intégration.

---

## Phase 2 — Angles morts du corps free

### T105 — Sonde post-rotation (P0) — à faire
- **Fichiers** : `free/profile_live.py`, `opencode.py::_free_post_rotation_probe`.
- **Action** : ajouter `mask_paths` à `build_verification_request` et
  `IdentityProfileSender` ; passer le mode effectif au call site.
- **Acceptation** : la sonde ne masque plus en mode off.
- **Test** : `tests/test_profile_live.py`.

### T106 — Corps de capture (P1) — à faire
- **Fichiers** : `free/runtime_identity.py::_build_capture_body`.
- **Action** : vérifier que tous les appelants transmettent `mask_paths` ;
  aucun défaut `True` résiduel.
- **Acceptation** : capture et corps client suivent le même mode.
- **Test** : `tests/test_runtime_identity.py`.

### T107 — Normaliser les anciens `/workspace/oc-*` (P0) — à faire
- **Fichiers** : `free/station_identity.py`, `free/runtime_identity.py`.
- **Action** : en mode off, remplacer toute racine `/workspace/oc-*` présente
  dans l'historique, le system ou une capture par le `cwd` réel extrait.
- **Acceptation** : le corps sortant ne contient aucun `/workspace/oc-`.
- **Test** : `tests/test_tool_call_path_healing.py`.
- **Constat live 21:21** : avec un historique contenant
  `/workspace/oc-WARP-1-IPv6-deadbeef/AGENTS.md`, le modèle réutilise ce
  chemin et le proxy ne le remappe plus (map vide) ⇒ le client Windows
  échouerait. Le correctif est requis avant de considérer le mode off complet
  pour les conversations déjà ouvertes. Il prendra effet au prochain restart
  (changement de code, pas de hot reload).

### T108 — Aucun `<env>` inventé (P1) — à faire
- **Fichiers** : `free/station_identity.py::render_system_prompt`.
- **Action** : si aucun `cwd` réel n'est extractible, ne pas injecter de bloc
  `<env>` ni de racine de remplacement.
- **Acceptation** : corps sans cwd ⇒ corps inchangé.
- **Test** : unitaire.

---

## Phase 3 — Jonctions, streaming et observabilité

### T109 — Aucune création de jonction (P0) — à faire
- **Fichiers** : `opencode.py::_set_free_path_map`.
- **Action** : garantir le retour avant `ensure_workspace_alias` en mode off ;
  test qui prouve qu'aucun `mklink` n'est appelé.
- **Acceptation** : zéro nouvelle entrée `C:\workspace\oc-*` en mode off.
- **Test** : `tests/test_workspace_alias.py` + mock `subprocess.run`.

### T110 — Nettoyage des jonctions existantes (P2) — à faire
- **Fichiers** : `scripts/cleanup_workspace_aliases.py` (nouveau).
- **Action** : `--dry-run` par défaut ; supprimer uniquement les reparse points
  `C:\workspace\oc-*` ; refuser tout dossier réel.
- **Acceptation** : 410 jonctions historiques listées, aucune suppression
  réelle sans `--apply`.
- **Test** : unitaire sur un faux reparse point.

### T111 — Exposer le mode effectif (P1) — à faire
- **Fichiers** : `dashboard/routers/config_routes.py`.
- **Action** : ajouter `workspace_path_masking` (valeur effective + source) à
  `GET /api/config`.
- **Acceptation** : le dashboard lit `false` en mode off.
- **Test** : route.

### T112 — Journaliser la source (P2) — à faire
- **Fichiers** : `opencode.py` (boot + watcher).
- **Action** : log `workspace_path_masking on|off (source=env|yaml|default)`.
- **Acceptation** : une ligne au boot et à chaque changement.
- **Test** : log.

### T113 — Streaming sans remap, schéma intact (P0) — à faire
- **Fichiers** : `streaming/tool_paths.py`, `opencode.py`.
- **Action** : vérifier map vide ⇒ aucun remap de chemin ; conserver
  `filePath` → `path`.
- **Acceptation** : tool_call réel inchangé, schéma guéri.
- **Test** : `tests/test_tool_call_path_healing.py`.

---

## Phase 4 — Tests et acceptance

### T114 — Tests de l'interrupteur (P0) — à faire
- **Fichiers** : `tests/test_tool_call_path_healing.py`.
- **Action** : yaml `false`, yaml `"false"`, env prioritaire, défaut.
- **Acceptation** : 4 cas verts.

### T115 — Tests du corps off (P0) — à faire
- **Fichiers** : `tests/test_station_identity.py`,
  `tests/test_tool_call_path_healing.py`.
- **Action** : `sanitize_text`, `template_official_body`,
  `render_system_prompt` en mode off.
- **Acceptation** : chemin réel conservé, aucun `/workspace/oc-`.

### T116 — Intégration chaude (P0) — à faire
- **Fichiers** : `tests/test_tool_call_path_healing.py`.
- **Action** : `_station_free_body` + `_set_free_path_map` en mode off.
- **Acceptation** : map vide, aucune jonction.

### T117 — Sonde de vérification (P1) — à faire
- **Fichiers** : `tests/test_profile_live.py`.
- **Action** : `build_verification_request(mask_paths=False)`.
- **Acceptation** : pas de racine factice.

### T118 — Historique avec racine factice (P1) — à faire
- **Fichiers** : `tests/test_tool_call_path_healing.py`.
- **Action** : message contenant `/workspace/oc-*` ⇒ normalisé.
- **Acceptation** : zéro `/workspace/oc-` sortant.

### T119 — Preuve live (P0) — fait
- **Fichiers** : `logs/debug.log`, réponses du proxy.
- **Action** : requête free avec `cwd` réel, vérifier le chemin vu par le
  modèle et le tool_call retourné.
- **Acceptation** : le modèle rend le `cwd` réel, aucun `/workspace` ; tool_call
  `read` avec le chemin réel.
- **Preuve** : `deepseek-v4.1-flash-free` a rendu
  `<env>\n  Working directory: C:\Users\julie\Downloads\opencode-proxy-one\n</env>`
  et un tool_call `read` sur
  `C:\Users\julie\Downloads\opencode-proxy-one\AGENTS.md`. La capture wire
  n'est pas instrumentée ; l'écho du prompt fait foi.

### T120 — Identité et flotte intactes (P0) — à faire
- **Fichiers** : `logs/fresh_chain_supervisor.log`, `logs/registry.db`,
  en-têtes free.
- **Action** : vérifier 6/6 `client_usable`, en-têtes UA/project/session
  inchangés.
- **Acceptation** : aucune régression attribuable au mode off.

---

## Definition of Done

1. Mode off effectif sans restart, durable après redémarrage.
2. Zéro `/workspace/oc-` dans le corps sortant, y compris historique/capture.
3. Tool_calls Windows exécutables du premier coup.
4. Aucune jonction nouvelle ; script de nettoyage disponible.
5. En-têtes d'identité inchangés ; 6/6 `client_usable` conservé.
6. Tests ciblés verts ; preuve live consignée.
