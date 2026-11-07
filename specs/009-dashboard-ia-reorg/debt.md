# Debt — 009 dashboard IA reorg

**Date**: 2026-11-07
**Statut**: aucune P0 ouverte.

| ID | Priorité | Dette | Impact | Plan |
|---|---|---|---|---|
| ~~D-009-1~~ | ~~P2~~ | ~~Recherche de réglage~~ | **Résolu** : index `search.ts` + `SettingsSearch.svelte` | — |
| D-009-2 | P2 | Sous-onglets non lazy-loaded individuellement | Le chunk FreeModels/Config contient toutes les sections | Extraire des composants dynamiques si le budget +20 % est dépassé |
| D-009-3 | P2 | Dirty guard limité aux sous-onglets | Un clic sur un onglet principal peut perdre un brouillon | Store dirty global + guard dans `navigate` |
| D-009-4 | P3 | Sections Config/FreeModels rendues conditionnellement dans les pages | Fichiers encore gros (Config 44 Ko) | Extraction progressive en composants avec props/handlers |
| D-009-5 | P3 | Tests UI manuels (pas de runner unitaire) | Régression possible sans `npm run check` | Ajouter un runner si le projet en adopte un |
| D-009-6 | P3 | Capture 375 px et mesure du premier rendu non faites | Budget +20 % non vérifié | Lighthouse/trace et capture mobile |

## Règles

- Aucune P0 ne doit rester ouverte avant un nouveau lot.
- Chaque nouvelle dette doit être datée et liée à une tâche.
