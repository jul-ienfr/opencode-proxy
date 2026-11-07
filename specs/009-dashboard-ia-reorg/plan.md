# Implementation Plan: Réorganisation des onglets Modèles Gratuits et Configuration

**Branch**: `009-dashboard-ia-reorg` | **Date**: 2026-11-07 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/009-dashboard-ia-reorg/spec.md`.

**Note**: Réorganisation **UI uniquement**. Aucune API, aucune clé de config,
aucun comportement runtime ne change. Les composants existants sont réutilisés
et déplacés dans des sous-onglets.

## Summary

Découper les deux pages les plus chargées du dashboard en sous-onglets clairs :

- **Modèles Gratuits** : Vue d'ensemble, Egress & providers, Routage free,
  Qualité & probes, Avancé.
- **Configuration** : Serveur, Modèles & routage, Clés & accès, Outils web,
  Maintenance.

Chaque réglage a **un seul** emplacement. Les anciens liens redirigent, le
sous-onglet actif est dans l'URL, les formulaires modifiés sont protégés, et
les composants lourds sont chargés à la demande.

## Technical Context

**Language/Version**: TypeScript, Svelte 5 (runes), Vite.

**Primary Dependencies**: routeur/hash existant (`$lib/router.svelte`),
`Card`, `Empty`, `i18n`, `query` — aucune nouvelle dépendance.

**Storage**: URL hash pour le sous-onglet ; `localStorage` pour la dernière
position si l'URL n'en contient pas ; aucun stockage serveur.

**Testing**: `npm --prefix dashboard-ui run check`, `npm --prefix dashboard-ui run build`,
tests de composants/routeur existants, tests manuels a11y/responsive.

**Target Platform**: Dashboard web (desktop + mobile 375 px), Chrome/Edge.

**Project Type**: Frontend Svelte dans `dashboard-ui/`, backend inchangé.

**Performance Goals**: premier rendu ≤ +20 % ; composants lourds lazy ;
navigation de sous-onglet < 100 ms.

**Constraints**: aucune modification d'API/config ; aucune perte de réglage ;
aucun doublon ; anciens hashes fonctionnels ; accessibilité clavier ; FR/EN.

**Scale/Scope**: 2 pages principales, 10 sous-onglets, ~8 composants déplacés,
0 changement backend.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principe | Verdict | Justification |
|---|---|---|
| I. Disponibilité mesurée | PASS | Aucun changement de probe/ledger ; l'UI ne déclare rien. |
| II. Trois ressources, trois cycles | PASS | Aucun changement station/session/IP. |
| III. Une source de vérité | PASS | Aucune migration ; les clés de config restent identiques. |
| IV. Reset fournisseur | PASS | Aucun changement runtime. |
| V. Une IP n'appartient qu'à une station | PASS | Aucun changement pool. |
| VI. Indépendance des stations | PASS | Aucun changement. |
| VII. Aucune preuve, aucun succès | PASS | L'UI ne fabrique aucun succès. |
| VIII. Autonomie, un seul réparateur | PASS | Aucun changement de réparation. |
| IX. Score ordonne, portes dures décident | PASS | Aucun changement de score. |
| X. Dégradation explicite, free only | PASS | Aucun fallback ajouté ; l'UI expose les états existants. |
| XI. Fallbacks multiples | PASS | Aucun changement de fallback. |
| XII. Toute option GUI a un effet | PASS | Chaque réglage garde son contrat ; la matrice est mise à jour. |
| XIII. Données traçables/migrables | PASS | Aucune donnée modifiée ; seuls les liens changent. |
| XIV. Architecture modulaire | PASS | Découpage en composants plus petits, pas de monolithe. |

Aucune violation à justifier. Le point sensible est **XII** : la
réorganisation ne doit pas masquer un réglage sans consommateur ; la matrice
`option → clé → consommateur → effet → test` est maintenue.

## Project Structure

### Documentation (this feature)

```text
specs/009-dashboard-ia-reorg/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── navigation.md
│   ├── route-aliases.md
│   └── option-ownership.md
└── tasks.md
```

### Source Code (repository root)

```text
dashboard-ui/src/
├── lib/
│   ├── router.svelte.ts           # hash → route, sous-onglet
│   └── components/
│       ├── SubTabs.svelte         # NOUVEAU : barre de sous-onglets a11y
│       └── Card.svelte            # existant
├── pages/
│   ├── FreeModels.svelte          # coquille + routeur de sous-onglets
│   ├── free/
│   │   ├── Overview.svelte        # inventaire + counts
│   │   ├── Egress.svelte          # VpnStatus + providers + allocator
│   │   ├── Routing.svelte         # VpnRouting + free attempts
│   │   ├── Quality.svelte         # probes/qualité/timeouts
│   │   └── Advanced.svelte        # VpnAdvanced restant
│   ├── Config.svelte              # coquille + routeur de sous-onglets
│   └── config/
│       ├── Server.svelte
│       ├── Models.svelte          # mapping + routes + catalogue
│       ├── Keys.svelte            # API keys
│       ├── Web.svelte             # web search/fetch
│       └── Maintenance.svelte     # debug/logs/updates/backups
└── pages/vpn/
    ├── VpnStatus.svelte           # réutilisé
    ├── VpnRouting.svelte          # réutilisé
    ├── VpnServers.svelte          # réutilisé
    ├── VpnAdvanced.svelte         # réutilisé (peut être découpé)
    └── AllocatorStatus.svelte     # réutilisé
```

**Structure Decision**: conserver les composants métier existants et créer
une couche de navigation (`SubTabs`) + des coquilles de page. Les gros
composants sont déplacés, pas réécrits.

### Implementation note (MVP 2026-11-07)

Le MVP livré garde `FreeModels.svelte` et `Config.svelte` comme coquilles et
rend les sections **conditionnellement** (`{#if active === …}`) au lieu de
créer 10 nouveaux fichiers. L'UX, les URLs, les alias et le dirty guard sont
identiques. L'extraction en composants est différée (voir `debt.md`) parce que
l'état et les handlers de `Config.svelte` sont partagés : les extraire d'un
bloc aurait dupliqué la logique de sauvegarde.

## Phase 0 — Audit et cartographie

1. Inventorier chaque section/réglage des pages `FreeModels.svelte` et
   `Config.svelte`.
2. Associer chaque réglage à un sous-onglet **unique**.
3. Lister les anciens hashes et les liens existants.
4. Identifier les composants lourds à lazy-loader.
5. Produire `contracts/option-ownership.md`.

**Sortie**: cartographie complète, 0 doublon, 0 réglage orphelin.

## Phase 1 — Coquille de navigation

1. Créer `SubTabs.svelte` (ARIA tablist, clavier, responsive, i18n).
2. Étendre `router.svelte` pour `#free-models/<tab>` et `#config/<tab>`.
3. Ajouter les alias des anciens hashes.
4. Créer les coquilles `FreeModels.svelte` et `Config.svelte` avec lazy
   loading.
5. Tests de route/alias.

**Critère**: sous-onglets navigables, URL persistante, anciens liens OK.

## Phase 2 — Modèles Gratuits

1. Déplacer l'inventaire dans `free/Overview.svelte`.
2. Déplacer `VpnStatus` + `AllocatorStatus` + checkboxes providers dans
   `free/Egress.svelte`.
3. Déplacer `VpnRouting` + free attempts dans `free/Routing.svelte`.
4. Déplacer qualité/probes/timeouts dans `free/Quality.svelte`.
5. Déplacer le reste de `VpnAdvanced` dans `free/Advanced.svelte`.
6. Vérifier qu'aucun réglage n'est dupliqué.

**Critère**: toutes les sections accessibles en ≤ 2 clics, 0 doublon.

## Phase 3 — Configuration

1. `config/Server.svelte` : serveur, port, bind, restart, debug.
2. `config/Models.svelte` : mapping, routes custom, catalogue.
3. `config/Keys.svelte` : API keys, token.
4. `config/Web.svelte` : web search/fetch.
5. `config/Maintenance.svelte` : updates, logs, backups, debug.
6. Vérifier les sauvegardes par sous-onglet.

**Critère**: chaque réglage dans un seul sous-onglet, sauvegardes OK.

## Phase 4 — Deep links, recherche, dirty guard

1. Alias d'anciens hashes + redirection.
2. Recherche de réglage → sous-onglet + highlight.
3. Guard `dirty` avant changement de sous-onglet.
4. Tests de navigation/reload.

**Critère**: 100 % des anciens liens, recherche fonctionnelle, confirmation
sur formulaire modifié.

## Phase 5 — Accessibilité, responsive, i18n

1. ARIA tablist/tabpanel, focus visible, flèches.
2. Scroll horizontal des sous-onglets à 375 px.
3. Libellés FR/EN.
4. Tests manuels clavier + mobile.

**Critère**: 0 piège de focus, 0 scroll horizontal de page, 100 % traduit.

## Phase 6 — Tests et polish

- `npm run check`, `npm run build`.
- Tests de composants/routes.
- Vérifier le premier rendu ≤ +20 %.
- Mettre à jour la matrice GUI et `debt.md`.
- Preuves dans `docs/evidence/009-dashboard-ia.md`.

## Risks and Mitigations

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Réglage perdu au déplacement | moyenne | élevé | cartographie exhaustive + tests |
| Doublon de réglage | moyenne | moyen | `option-ownership.md` + revue |
| Ancien lien cassé | moyenne | moyen | alias + tests |
| Composant lourd ralentit | moyenne | moyen | lazy loading |
| Test UI cassé | élevée | faible | `data-testid` stables |
| Accessibilité régressée | moyenne | moyen | ARIA/clavier + tests |
| Sauvegarde partielle | moyenne | élevé | tests par sous-onglet |
| URL inconnue | faible | faible | fallback défaut |

## Re-check Constitution (post-design)

Tous les principes restent **PASS**. Points à surveiller :

- **XII** : la matrice option/consommateur doit être mise à jour, pas
  seulement déplacée.
- **XIV** : les nouveaux fichiers restent petits ; pas de méga-composant.
- **III** : aucune clé de config renommée ; seuls les liens changent.

## Next Step

Exécuter `/speckit.tasks` puis démarrer par la Phase 0 (cartographie) et la
Phase 1 (SubTabs + routes). La réorganisation est indépendante de la feature
008 Auto-Egress ; elle peut être livrée avant ou après.
