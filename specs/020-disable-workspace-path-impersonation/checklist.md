# Checklist — Désactiver l'impersonation des chemins d'accès

**Feature** : `020-disable-workspace-path-impersonation`
**Date** : 2026-11-14

## Bascule

- [x] T100 — `free.workspace_path_masking: false` appliqué sur le live.
- [x] T100 — hot reload pris sans restart (mode effectif `False`).
- [ ] T101 — défaut `false` dans le code.
- [ ] T102 — parsing strict des chaînes `"false"`/`"0"`/`"no"`/`"off"`.
- [ ] T103 — clé documentée (config, `.env.example`, README).

## Corps free

- [ ] T104 — tous les appelants free explicites.
- [ ] T105 — sonde post-rotation en mode off.
- [ ] T106 — corps de capture en mode off.
- [ ] T107 — anciens `/workspace/oc-*` normalisés.
- [ ] T108 — aucun `<env>` inventé sans cwd.
- [x] T119 — le modèle voit le vrai `cwd` (écho live).
- [x] T119 — tool_call `read` avec le chemin réel.

## Jonctions et streaming

- [ ] T109 — aucune nouvelle jonction en mode off.
- [ ] T110 — script de nettoyage dry-run/reparse-only.
- [ ] T113 — map vide, schéma `filePath` → `path` conservé.

## Observabilité et identité

- [ ] T111 — mode effectif dans `GET /api/config`.
- [ ] T112 — log de la source (env/yaml/défaut).
- [ ] T120 — en-têtes d'identité inchangés.
- [ ] T120 — 6/6 `client_usable` conservé.

## Tests

- [ ] T114 — interrupteur (yaml, chaîne, env, défaut).
- [ ] T115 — corps off (sanitize/render/template).
- [ ] T116 — intégration `_station_free_body` + map vide.
- [ ] T117 — sonde `mask_paths=False`.
- [ ] T118 — historique avec racine factice.

## Limitation connue

- [ ] T107 — une conversation déjà ouverte qui contient encore
  `/workspace/oc-*` peut faire réutiliser l'ancien chemin par le modèle ; le
  remap étant inactif, le client Windows échouerait. Une nouvelle session est
  saine ; le correctif d'historique nécessite un restart.

## Definition of Done

1. Mode off immédiat et durable, sans restart.
2. Zéro `/workspace/oc-` sortant, y compris historique/capture.
3. Tool_calls Windows exécutables du premier coup.
4. Aucune jonction nouvelle ; nettoyage sûr disponible.
5. Identité de station et 6/6 `client_usable` intacts.
6. Tests ciblés verts et preuve live consignée.
