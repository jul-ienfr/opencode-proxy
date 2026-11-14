# Implementation Plan: Désactiver l'impersonation des chemins d'accès

**Branch**: `020-disable-workspace-path-impersonation` | **Date**: 2026-11-14 | **Spec**: [spec.md](./spec.md)

**Input**: `spec.md`, `opencode.py`, `free/station_identity.py`,
`free/runtime_identity.py`, `free/profile_live.py`, `streaming/tool_paths.py`.

## Summary

Le proxy possède déjà un interrupteur (`free.workspace_path_masking` /
`FREE_WORKSPACE_PATH_MASKING`) mais son défaut est `true` et deux chemins
secondaires (sonde de profil, corps de capture) ne le consultent pas. Le plan
rend le mode off **immédiat** (config hot reload), **durable** (défaut `false`),
**complet** (sondes, captures, historiques) et **observable**, sans toucher à
l'identité de station ni aux six stations.

La bascule live est déjà appliquée : `config.yaml` porte
`free.workspace_path_masking: false`, et l'évaluation hors process donne
`False`. Le reste du plan supprime les angles morts.

## Décision d'architecture

**Un seul interrupteur, un seul interpréteur.**

- `_workspace_path_masking_enabled()` reste l'unique source de vérité ; il lit
  l'env puis `config.yaml` à chaque appel (hot reload safe).
- Le mode off ne supprime pas le code de masking : il le laisse inactif, ce qui
  permet un rollback par config sans livraison.
- L'identité de station (`free.station_identity: true`) est indépendante : UA,
  `x-opencode-*`, project, session, trace et TLS ne changent pas.
- Le remap retour (`_free_path_map`, `translate_paths`) est **désactivé** en
  mode off ; le healing de schéma (`filePath` → `path`) reste actif car il ne
  réécrit pas la valeur du chemin.

**Invariant** : en mode off, aucun corps sortant ne contient
`/workspace/oc-`. Si un ancien tour ou une capture en contient encore, il est
normalisé vers le `cwd` réel avant l'envoi.

## Contexte technique

- Interrupteur : `opencode.py::_workspace_path_masking_enabled()`.
- Construction du corps free : `opencode.py::_station_free_body()` →
  `free/runtime_identity.py::build_body(mask_paths=...)`.
- Substitution : `free/station_identity.py::sanitize_text`,
  `render_system_prompt`, `template_official_body`, `_sanitize_value`.
- Racine factice : `workspace_env_cwd`, `ENV_PREFIX`, `ALIAS_ROOT`.
- Jonction Windows : `ensure_workspace_alias` (`mklink /J C:\workspace\oc-*`).
- Remap retour : `_free_path_map`, `_path_map_from_body`,
  `streaming/tool_paths.py`.
- Sonde hors switch : `free/profile_live.py::build_verification_request` et
  `free/runtime_identity.py::_build_capture_body`.
- Hot reload : `opencode.py::_periodic_config_watch` (5 s + 1 s debounce).

## Phases

### Phase 0 — Bascule immédiate et baseline (fait)

- Ajouter `free.workspace_path_masking: false` à `config.yaml`.
- Attendre le watcher (≤ 6 s), vérifier que la clé survit à une réécriture.
- Évaluer `_workspace_path_masking_enabled()` hors process : `False`.
- Relever la baseline : en-têtes d'identité, statut 6/6, dernières erreurs
  `C:\workspace does not exist`.

**Sortie** : mode off effectif sans restart, baseline conservée.

### Phase 1 — Défaut durable et parsing strict (P0)

- **T101** : passer le défaut de `_workspace_path_masking_enabled()` à `False`.
- **T102** : parsing strict de la valeur YAML : un `"false"` chaîne doit valoir
  `False` (pas `bool("false")`), idem `"0"`, `"no"`, `"off"`.
- **T103** : ajouter la clé commentée dans un exemple de config
  (`.env.example` ou bloc documenté) pour que la valeur survive aux
  régénérations.
- **T104** : vérifier que `_station_free_body` transmet bien le mode à
  `build_body`, et qu'aucun autre appel free ne prend le défaut `True`.

**Sortie** : un redémarrage ou une régénération de config reste en mode off.

### Phase 2 — Angles morts du corps free (P0)

- **T105** : `free/profile_live.py::build_verification_request` reçoit
  `mask_paths` ; `IdentityProfileSender` le porte ; le call site
  `opencode.py::_free_post_rotation_probe` passe le mode effectif.
- **T106** : `free/runtime_identity.py::_build_capture_body` reçoit déjà
  `mask_paths` ; vérifier que tous ses appelants le transmettent.
- **T107** : en mode off, normaliser les `/workspace/oc-*` historiques d'un
  corps (messages, system, contenu de capture) vers le `cwd` réel extrait par
  `workspace_root_from_messages`. Aucun chemin factice ne doit sortir.
- **T108** : si aucun `cwd` réel n'est extractible, ne rien injecter : pas de
  `<env>` inventé, pas de racine de remplacement.

**Sortie** : le corps free ne contient plus aucune racine factice, même avec un
historique ancien ou une capture.

### Phase 3 — Jonctions, streaming et observabilité (P1)

- **T109** : confirmer que `_set_free_path_map` sort avant
  `ensure_workspace_alias` en mode off ; ajouter un test qui prouve qu'aucun
  `mklink` n'est appelé.
- **T110** : ajouter `scripts/cleanup_workspace_aliases.py` : liste et supprime
  uniquement les `C:\workspace\oc-*` qui sont des reparse points ; refuse tout
  dossier réel ; `--dry-run` par défaut.
- **T111** : exposer `workspace_path_masking` (valeur effective) dans
  `GET /api/config` via `config.settings.yaml_get`.
- **T112** : journaliser une fois au boot et à chaque changement :
  `workspace_path_masking on|off (source=env|yaml|default)`.
- **T113** : vérifier que `streaming/tool_paths.py` reçoit une map vide et que
  le healing de schéma reste actif ; test de non-régression `filePath` → `path`.

**Sortie** : mode visible, aucune jonction nouvelle, retour arrière par config.

### Phase 4 — Tests et acceptance (P0)

- **T114** : unitaires `_workspace_path_masking_enabled` (yaml false, yaml
  `"false"`, env prioritaire, défaut).
- **T115** : unitaires `sanitize_text` / `template_official_body` /
  `render_system_prompt` en mode off (chemin réel conservé, aucun
  `/workspace/oc-`).
- **T116** : intégration `_station_free_body` + `_set_free_path_map` en mode
  off (map vide, pas de jonction).
- **T117** : test sonde `build_verification_request(mask_paths=False)`.
- **T118** : test d'historique contenant `/workspace/oc-*` → normalisé.
- **T119** : preuve live : requête free, corps sortant avec `cwd` réel, zéro
  `/workspace/oc-`, tool_call `read` avec chemin réel.
- **T120** : vérifier 6/6 `client_usable` et en-têtes d'identité inchangés
  après la bascule.

**Commandes** :

```powershell
python -m pytest tests/test_tool_call_path_healing.py `
  tests/test_profile_live.py tests/test_station_identity.py `
  tests/test_workspace_alias.py -q
```

**Sortie** : SC-001..SC-007 couverts, flotte intacte.

## Fichiers touchés

```text
opencode.py                                  # défaut false, sonde, observabilité
free/station_identity.py                     # normalisation historique (mode off)
free/runtime_identity.py                     # transmission mask_paths
free/profile_live.py                         # mask_paths dans la sonde
dashboard/routers/config_routes.py           # /api/config expose le mode
scripts/cleanup_workspace_aliases.py         # nettoyage sûr des jonctions
tests/test_tool_call_path_healing.py         # mode off + historique
tests/test_profile_live.py                   # sonde mask_paths
tests/test_station_identity.py               # sanitize/render off
tests/test_workspace_alias.py                # aucune création en off
```

## Risques et mitigations

| Risque | Mitigation |
|---|---|
| Env `FREE_WORKSPACE_PATH_MASKING=true` hérité | diagnostic au boot + log de la source |
| Valeur YAML `"false"` interprétée truthy | parsing strict T102 |
| Historique avec `/workspace/oc-*` | normalisation T107 + audit du corps |
| Capture officielle avec `cwd` factice | sonde T105 + normalisation T107 |
| Tool_calls Windows cassés | preuve live T119, chemin réel de bout en bout |
| Jonctions existantes orphelines | script T110, dry-run, reparse-only |
| Régression identité de station | tests d'en-têtes T120 |
| Config réécrite par le dashboard | clé documentée + test de survie |

## Retour arrière

1. `free.workspace_path_masking: true` dans `config.yaml` (hot reload ≤ 6 s).
2. Ou `FREE_WORKSPACE_PATH_MASKING=true` + restart si l'env doit gagner.
3. Aucun code à redéployer : le masking est resté intact.

## Critères de sortie

- Mode off effectif dans le process live, sans restart.
- Corps free avec `cwd` réel, zéro `/workspace/oc-`.
- Tool_call `read`/`edit`/`write` avec chemin réel, exécuté du premier coup.
- Aucune nouvelle jonction ; script de nettoyage disponible.
- En-têtes d'identité inchangés ; 6/6 `client_usable` conservé.
- Tests ciblés verts et preuve live consignée.
