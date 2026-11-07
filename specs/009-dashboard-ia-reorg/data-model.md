# Data Model — Réorganisation dashboard

**Date**: 2026-11-07
**But**: décrire la navigation, les alias et la propriété des réglages. Aucune
donnée serveur n'est modifiée.

## NavigationGroup

| Champ | Type | Contrainte |
|---|---|---|
| `id` | string | `free-models`, `config` |
| `label_fr` | string | « Modèles Gratuits », « Configuration » |
| `label_en` | string | « Free Models », « Configuration » |
| `default_tab` | string | id du sous-onglet par défaut |
| `tabs` | list[NavigationTab] | ordre stable |

## NavigationTab

| Champ | Type | Contrainte |
|---|---|---|
| `id` | string | unique dans le groupe |
| `label_fr` | string | traduit |
| `label_en` | string | traduit |
| `route` | string | `#free-models/egress`, `#config/server` |
| `component` | string | chemin du composant |
| `lazy` | bool | true pour les composants lourds |
| `order` | int | 0..n |
| `testid` | string | stable, `tab-<id>` |

## RouteAlias

| Champ | Type | Contrainte |
|---|---|---|
| `old` | string | `#config`, `#free-models`, `#vpn` |
| `new` | string | route cible |
| `permanent` | bool | redirection stable |
| `created_at` | float | timestamp |

**Règles**:
- un alias ne pointe jamais vers un sous-onglet supprimé.
- un hash inconnu tombe sur `default_tab`.
- les alias sont testés un par un.

## SettingOwnership

| Champ | Type | Contrainte |
|---|---|---|
| `key` | string | clé de config/API |
| `group` | string | `free-models` ou `config` |
| `tab` | string | un seul sous-onglet |
| `component` | string | composant qui l'affiche |
| `consumer` | string | consommateur runtime |
| `test` | string | test associé |
| `display_only` | bool | true si aucun consommateur |

**Règles**:
- `key` unique ; un réglage ne peut pas apparaître dans deux tabs.
- `display_only=true` doit être affiché comme tel.
- la matrice est la référence de la revue.

## DirtyForm

| Champ | Type | Contrainte |
|---|---|---|
| `group` | string | page |
| `tab` | string | sous-onglet |
| `dirty` | bool | formulaire modifié |
| `actions` | list[str] | `save`, `discard`, `stay` |

**Règles**:
- `dirty=true` → confirmation avant changement de sous-onglet.
- `save` sauvegarde puis navigue ; `discard` navigue sans sauver ; `stay`
  reste.
- Le guard ne s'applique pas aux formulaires non modifiés.

## NavigationState

| Champ | Type | Contrainte |
|---|---|---|
| `group` | string | page active |
| `tab` | string | sous-onglet actif |
| `source` | enum | `url`, `localStorage`, `default` |
| `updated_at` | float | timestamp |

**Règles**:
- URL prioritaire ; `localStorage` en fallback ; défaut sinon.
- Le changement de tab met à jour l'URL sans recharger la page.

## Relations

```text
NavigationGroup 1 ─── * NavigationTab
NavigationTab   1 ─── * SettingOwnership
RouteAlias      * ─── 1 NavigationTab
DirtyForm       * ─── 1 NavigationTab
```

## Migration

- Aucune table serveur.
- Les anciens hashes sont mappés par `RouteAlias`.
- Les `data-testid` existants sont conservés ou remplacés avec un alias de
  test.
- Aucune clé de config renommée.
