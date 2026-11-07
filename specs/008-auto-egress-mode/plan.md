# Implementation Plan: Auto-Egress — mode multi-provider automatique

**Branch**: `008-auto-egress-mode` | **Date**: 2026-11-07 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/008-auto-egress-mode/spec.md`.

**Note**: Ce plan réutilise l'abstraction `EgressProvider` et la taxonomie
d'erreurs de la feature 007. Aucun nouvel abonnement payant n'est créé : le
mode utilise uniquement les providers déjà configurés (NordVPN, WARP, proxys).

## Summary

Remplacer le choix exclusif du GUI par une section **« Egress providers »**
avec des cases à cocher : NordVPN, WARP, Proxys SOCKS5/HTTPS, Direct.
L'opérateur coche un ou plusieurs providers ; le moteur **Auto-Egress**
(`auto_egress`) agrège tous les providers cochés et sélectionne automatiquement
le meilleur egress pour chaque requête.

- Une seule case cochée → comportement legacy exact (`vpn`, `socks5`,
  `direct`).
- Plusieurs cases cochées → pool unifié Auto-Egress.
- Aucune case cochée → refus explicite, pas de fallback silencieux.

Le parallélisme est **inter-requêtes** et **par hedge** : une requête utilise
un seul egress, mais plusieurs requêtes/tentatives utilisent des providers
différents. Chaque provider garde sa santé, son quota et sa rotation ; un
`403` ne tourne pas, un `429` parke/tourne le provider concerné, et un provider
down ne casse pas les autres. Le mode est fail-closed : pas de direct
silencieux.

## Technical Context

**Language/Version**: Python 3.12+, TypeScript/Svelte 5, PowerShell 5.1/7

**Primary Dependencies**: FastAPI/uvicorn, httpx, curl_cffi, SQLite WAL,
Docker (NordVPN/WARP), pytest. Aucune nouvelle dépendance.

**Storage**: `registry.db` pour les décisions/attributions ; `config.yaml`
pour `auto_egress` ; le ledger 007 pour les blocs/quotas.

**Testing**: `pytest -k "not docker"` offline ; tests live contrôlés avec
providers réels. Les tests ne doivent pas consommer le quota de production
sans marquage.

**Target Platform**: Windows (proxy + dashboard), providers Docker/local.

**Project Type**: Backend Python + dashboard Svelte + providers.

**Performance Goals**: disponibilité > 99 % avec un provider down ; sélection
< 5 ms hors I/O ; changement de mode sans interruption ; caps de concurrence
respectés.

**Constraints**: un egress par requête ; pas de bonding ; free-first par
défaut ; aucun nouvel abonnement ; pas de direct silencieux ; `403` ≠ `429` ;
`reset_at` par provider ; dédup d'egress physique ; seuils dans `config.yaml`.

**Scale/Scope**: 6 stations WARP, N stations NordVPN, M proxys ; 3 000–10 000
requêtes free/jour ; 2–3 providers simultanés typiques.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principe | Verdict | Justification |
|---|---|---|
| I. Disponibilité mesurée | PASS | Attributions par requête + probes par provider ; `degraded` explicite. |
| II. Trois ressources, trois cycles | PASS | Station, session, IP restent séparées ; le provider ajoute un axe de santé, pas un doublon. |
| III. Une source de vérité | PASS | `registry.db` pour les décisions/attributions ; `config.yaml` pour les poids. |
| IV. Reset fournisseur | PASS | Chaque provider garde son `reset_at` ; le mode ne contourne pas le park. |
| V. Une IP n'appartient qu'à une station | PASS | Dédup par egress physique (IP/bloc), même entre providers. |
| VI. Indépendance des stations | PASS | Aucune contrainte pays ; la diversité reste optionnelle. |
| VII. Aucune preuve, aucun succès | PASS | Un `200` valide seulement ; le mode ne déclare pas un succès sur un `403`. |
| VIII. Autonomie, un seul réparateur | PASS | Le mode est un sélecteur, pas un réparateur ; le superviseur reste propriétaire. |
| IX. Score ordonne, portes dures décident | PASS | Le score ne contourne ni quota, ni reset, ni unicité. |
| X. Dégradation explicite, free only | PASS | Free-first, aucun nouvel abonnement, fail-closed, pas de direct silencieux. NordVPN déjà configuré est un provider existant, pas un substitut payant silencieux. |
| XI. Fallbacks multiples | PASS | Le mode agrège les providers et réutilise la politique de fallback 007. |
| XII. Toute option GUI a un effet | PASS | Poids, caps, providers, direct et sticky ont chacun un consommateur + test. |
| XIII. Données traçables/migrables | PASS | Attribution append-only ; migration additives ; backup. |
| XIV. Architecture modulaire | PASS | `auto_egress.py` + `provider_selector.py` < 1 500 lignes ; ports/adapters. |

Aucune violation à justifier. Point sensible **X** : le mode utilise NordVPN
s'il est déjà configuré, mais ne crée aucun abonnement et ne remplace jamais
WARP par défaut ; `allow_direct=false` et `fail_closed=true` par défaut.

## Project Structure

### Documentation (this feature)

```text
specs/008-auto-egress-mode/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── auto-egress-policy.md
│   ├── provider-selection.md
│   ├── mode-ui.md
│   └── status-api.md
└── tasks.md
```

### Source Code (repository root)

```text
free/
├── auto_egress.py             # NOUVEAU : pool unifié, config, mode
├── provider_selector.py       # NOUVEAU : score, poids, caps, sticky
├── egress_provider.py         # feature 007 : protocol + result
├── errors.py                  # feature 007 : taxonomie
├── fallback.py                # feature 007 : politique de repli
├── pool.py                    # intégration mode/selection
├── warp_rotation.py           # provider WARP
└── quota_ledger.py            # quota par provider

opencode.py                    # _free_proxy_mode, sélection free, mode switch
dashboard/api.py               # mode endpoint, statut providers, attribution
dashboard-ui/src/pages/vpn/
├── VpnStatus.svelte           # cases à cocher providers + attribution
└── VpnAdvanced.svelte         # poids, caps, hedge, direct, sticky

config.yaml                    # auto_egress.providers/weights/caps
tests/
├── test_auto_egress_mode.py
├── test_provider_selector.py
├── test_auto_egress_failover.py
├── test_auto_egress_direct.py
└── test_auto_egress_ui.py
```

**Structure Decision**: conserver le pool existant et ajouter un **routeur de
providers** au-dessus, pas un second proxy. Le mode est un consommateur de
l'abstraction 007.

## Phase 0 — Research & decisions

1. Contrat UI : section de cases à cocher (NordVPN, WARP, Proxys, Direct),
   pas un quatrième bouton radio. `auto_egress` est le moteur d'agrégation.
2. Définir le mode dérivé : une case = legacy (`vpn`/`socks5`/`direct`),
   plusieurs cases = `auto_egress`; zéro case = refus.
3. Définir le périmètre du parallélisme : inter-requêtes + hedge, pas bonding.
4. Définir les poids par défaut : WARP 60 %, NordVPN 30 %, proxys 10 %.
5. Définir les caps : `max_concurrent` par provider, `hedge_delay_ms`,
   `hedge_max_attempts`.
6. Définir la politique de diversité : douce, jamais une porte dure.
7. Définir le comportement direct : `allow_direct=false` par défaut,
   `fail_closed=true`, case explicite avec avertissement.
8. Vérifier la compatibilité avec `free_parallel` existant.

**Sortie**: `research.md` avec les décisions et alternatives.

## Phase 1 — Foundational

1. Ajouter `auto_egress` à l'enum de mode et à `_free_proxy_mode`.
2. Créer `free/auto_egress.py` : `AutoEgressConfig`, `UnifiedEndpoint`,
   `ProviderState`, `build_pool()`.
3. Créer `free/provider_selector.py` : score, poids, caps, sticky,
   diversité, hedge.
4. Brancher le pool unifié dans `free/pool.py` : sélection, retry, hedge.
5. Ajouter la config `auto_egress` et la validation dans `config/settings.py`.
6. Ajouter l'attribution par requête dans `registry.db` / logs.
7. Tests de sélection, caps, sticky, dédup.

**Critère**: mode sélectionnable en interne, pool unifié construit, sélection
déterministe avec des providers factices.

## Phase 2 — US1 (cases à cocher/persistance)

1. Remplacer le radio group par une section de cases à cocher dans
   `VpnStatus.svelte` + locales (`Egress providers`).
2. Étendre `POST /api/vpn/proxy-mode` pour accepter une liste
   `egress.providers` et dériver `vpn`/`socks5`/`direct`/`auto_egress`.
3. Persister/restaurer la liste dans `config.yaml` ; lire le legacy
   `proxy_mode` si la liste est absente.
4. Refuser zéro provider avec un message explicite.
5. Drain au changement de sélection.
6. Tests UI/API/persistance/backward-compat.

**Critère**: cases visibles, une ou plusieurs cochées, zéro refusée,
persistance et mode dérivé corrects.

## Phase 3 — US2 (sélection multi-provider)

1. Construire le pool unifié : NordVPN managers + WARP endpoints + proxies.
2. Implémenter le score : santé, quota, latence, succès récents, poids.
3. Implémenter les caps de concurrence par provider.
4. Brancher le hedge/retry multi-provider.
5. Sticky par conversation.
6. Tests de répartition, caps, hedge, sticky.

**Critère**: 20 requêtes → ≥2 providers, >99 % de succès.

## Phase 4 — US3 (isolation pannes/quotas)

1. Réutiliser `errors.py` (403/429/5xx/account).
2. Propager le park/rotate au provider concerné uniquement.
3. Retirer un provider down de la sélection sans casser les autres.
4. Fail-closed + `degraded`.
5. Tests 403/429/down/tous down.

**Critère**: un provider down → disponibilité >99 % ; 403 → 0 rotation.

## Phase 5 — US4 (poids/diversité)

1. Exposer `auto_egress.providers`, `weights`, `max_concurrent`,
   `hedge_delay_ms`, `allow_direct`, `diversity`, `sticky`.
2. GUI : activer/désactiver, poids, caps.
3. Validation des poids (somme, bornes), effet runtime.
4. Tests de répartition ±10 points.

**Critère**: poids respectés, options persistantes et effectives.

## Phase 6 — US5 (observabilité)

1. `/api/vpn-status` et `/api/free-health` : providers actifs, santé, poids,
   quota, dernier 403/429, switches.
2. Attribution par requête (provider, endpoint, bloc, statut, latence).
3. Dashboard : répartition, dernier événement, `degraded`.
4. Tests API/UI.

**Critère**: 100 % des requêtes récentes attribuées.

## Phase 7 — Polish

- Tests complets `pytest -k "not docker"`, `ruff`, `npm run check/build`.
- Soak 24 h avec providers réels, 403/429 injectés.
- Docs : mode, config, runbook, rollback.
- Mise à jour de `debt.md`, `CHANGELOG`, `spec.md` si une décision change.

## Risks and Mitigations

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| NordVPN épuisé/down | moyenne | moyen | WARP/proxy continuent ; provider retiré |
| WARP épuisé | élevée | moyen | NordVPN/proxy prennent le relais |
| Provider partage un egress | moyenne | moyen | dédup par IP/bloc |
| 403 pris pour un 429 | moyenne | élevé | taxonomie 007 + tests |
| Direct silencieux | faible | critique | `allow_direct=false`, fail-closed |
| Poids ignorés | moyenne | moyen | tests ±10 points, option effective |
| Cap de concurrence cassé | moyenne | moyen | semaphores par provider + tests |
| Mode switch interrompt une requête | moyenne | élevé | drain + tests |
| Coût NordVPN | faible | moyen | aucun nouvel abonnement ; providers existants |
| Attribution incomplète | moyenne | moyen | tests API + logs |

## Re-check Constitution (post-design)

Tous les principes restent **PASS**. Points à surveiller :

- **X** : le mode est free-first et fail-closed ; NordVPN n'est utilisé que
  s'il est déjà configuré, sans nouvel abonnement.
- **V** : la dédup par egress physique doit couvrir les providers qui
  partagent une IP/bloc.
- **XII** : chaque option (poids, cap, provider, direct, sticky) doit avoir un
  test runtime.

## Next Step

> **Voir aussi** : `specs/009-dashboard-ia-reorg/` pour la réorganisation des
> onglets Modèles Gratuits/Configuration, qui fournit la section de cases à
> cocher des providers.

Exécuter `/speckit.tasks` puis démarrer par la Phase 1 (mode enum + pool
unifié) et la Phase 2 (GUI/persistance). Le mode peut être livré en MVP avec
NordVPN + WARP, puis étendu aux proxys.
