# Implementation Plan: Trois modes d'egress — NordVPN, WARP, Direct

**Branch**: `006-three-egress-modes` | **Date**: 2026-11-06 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/006-three-egress-modes/spec.md`

**Note**: Implémenté le 2026-11-06. Ce document conserve l'analyse et les
décisions de conception ; les tâches exécutées sont dans [tasks.md](./tasks.md).

## Summary

Renommer les trois modes visibles en **NordVPN**, **WARP**, **Direct** sans
changer les valeurs internes `vpn`/`socks5`/`direct`, puis séparer la GUI en
trois espaces :

1. les réglages **NordVPN-only** (stack, sélection serveur, identifiants,
   serveurs, nombre de tunnels) ;
2. les réglages **WARP-only** (liste des endpoints, conteneurs, ports, test,
   rotation) ;
3. une section **Routage free commun** visible en NordVPN et WARP, qui pilote
   réellement les deux backends depuis `ip_rotation`.

Le backend consomme déjà la majorité des clés communes dans les deux modes
(`free_parallel`, `quality_rotation`, `latency_rotation`, `strict_free`,
`on_429_action`, `station_bad_ttl_s`, tentatives, fallback exceptionnel,
retry). Le plan verrouille cette matrice, corrige les libellés et l'isolation
des sections, et ajoute les tests manquants. Aucune migration de données.

## Technical Context

**Language/Version**: Python 3.12+ (backend), TypeScript/Svelte 5 (dashboard)

**Primary Dependencies**: FastAPI, uvicorn, curl_cffi, SQLite/WAL ; Svelte 5 +
Vite pour `dashboard-ui` ; aucune nouvelle dépendance.

**Storage**: `config.yaml` (`ip_rotation`), `registry.db`/`telemetry.db` pour
les décisions et observations. Aucun changement de schéma.

**Testing**: `pytest` (backend), `npm run build` + `svelte-check` (frontend),
validation navigateur intégré pour les libellés et les sections.

**Target Platform**: Windows (proxy `pythonw.exe`, Docker Desktop, WARP
containers) ; comportement Linux conservé.

**Project Type**: application web unique (backend FastAPI + dashboard Svelte
dans le même dépôt).

**Performance Goals**: aucun appel d'inférence supplémentaire ; le changement
de mode ne fait que persister `proxy_mode` et démarrer/arrêter les managers
nécessaires. Le rendu de la page ne doit pas déclencher de probe lourde.

**Constraints**:
- valeurs internes `vpn`/`socks5`/`direct` inchangées (compatibilité API,
  config, tests, base) ;
- une option GUI sans consommateur runtime est interdite (Constitution XII) ;
- aucun fallback implicite entre NordVPN, WARP et Direct ;
- le changement de mode ne doit pas interrompre une requête en vol.

**Scale/Scope**: 1 page Free Models, 4 composants Svelte, 1 endpoint de mode,
1 matrice de paramètres communs, 3 modes, 7 endpoints WARP et N stations
NordVPN.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principe | Verdict | Justification |
|---|---|---|
| I. Disponibilité mesurée | PASS | La capacité et le statut sont calculés par backend ; WARP compte ses endpoints, NordVPN ses stations. |
| II. Trois ressources, trois cycles de vie | PASS | Le mode ne change ni l'identité de station, ni la session, ni la clé IP. Les cooldowns restent keyés sur l'IP. |
| III. Une source de vérité | PASS | Les paramètres communs restent dans `ip_rotation` ; aucun doublon NordVPN/WARP. |
| IV. Reset fournisseur = barrière dure | PASS | Aucun paramètre commun ne contourne `reset_at` ni le ledger. |
| V. Une IP n'appartient qu'à une station | PASS | WARP et NordVPN gardent leurs pools séparés ; le mode ne fusionne pas les egress. |
| VI. Indépendance des stations | PASS | Aucun quota/pays imposé par le mode ; les paramètres communs ne créent pas de contrainte géographique. |
| VII. Aucune preuve, aucun succès | PASS | Les probes et preuves de contenu restent inchangées ; le mode ne déclare pas un succès. |
| VIII. Autonomie, un seul réparateur | PASS | Le changement de mode n'est pas une réparation ; le superviseur reste seul propriétaire. |
| IX. Score ordonne, portes dures décident | PASS | Les paramètres communs ne contournent pas les portes dures. |
| X. Dégradation explicite, free only | PASS | Un mode indisponible expose `degraded` + raison ; pas de bascule silencieuse payante. |
| XI. Fallbacks multiples | PASS | Les fallbacks existants (`socks5_fallback_enabled`, `https_fallback_enabled`, `free_exception_fallback`) restent explicites et ordonnés. |
| XII. Toute option GUI a un effet réel | PASS | Le plan produit une matrice `option → clé → consommateur → effet → test` ; les contrôles sans consommateur sont retirés ou marqués. |
| XIII. Données traçables/migrables | PASS | Aucune migration destructive ; les valeurs existantes restent lisibles. |
| XIV. Architecture modulaire | PASS | Modification de présentation + réutilisation des consommateurs existants ; aucun nouveau monolithe. |

Aucune violation à justifier. Pas de `Complexity Tracking` nécessaire.

## Project Structure

### Documentation (this feature)

```text
specs/006-three-egress-modes/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
└── contracts/
    ├── mode-ui.md
    ├── shared-routing.md
    └── status-capacity.md
```

### Source Code (repository root)

```text
dashboard-ui/src/pages/
├── FreeModels.svelte                  # composition des sections
└── vpn/
    ├── VpnStatus.svelte               # boutons NordVPN/WARP/Direct + statut
    ├── VpnRouting.svelte              # NordVPN-only + routage free commun
    ├── VpnServers.svelte              # NordVPN-only (credentials/serveurs)
    └── VpnAdvanced.svelte             # qualité commune, WARP-only, HTTPS NordVPN

dashboard-ui/src/lib/
└── locales.ts                         # libellés et aides FR/EN

dashboard/
└── api.py                             # /api/vpn/proxy-mode, /api/vpn-config,
                                       # capacité/statut par mode

free/
├── pool.py                            # consommateurs communs + sélection WARP
└── warp_rotation.py                   # rotation WARP

opencode.py                            # tentatives, retry, qualité, statut free
config/settings.py                     # lecture/validation ip_rotation

tests/
├── test_vpn_endpoints.py
├── test_free_attempt_tuning.py
├── test_socks5_affinity.py
├── test_socks5_failover_guard.py
├── test_warp_rotation.py
├── test_free_station_reliability.py
└── test_vpn_alerts_capacity.py
```

**Structure Decision**: application unique existante. La feature touche
principalement la présentation Svelte et la matrice de paramètres ; les
consommateurs Python existent déjà et ne sont pas déplacés.

## Phase 0 — Research

Artefact : [research.md](./research.md).

Décisions à verrouiller :

1. **Mapping des modes** : `vpn` → NordVPN, `socks5` → WARP, `direct` →
   Direct ; aucune migration de valeur interne.
2. **Matrice des paramètres** : inventorier chaque contrôle GUI, sa clé
   `ip_rotation`, son consommateur Python, son effet en NordVPN et en WARP, et
   son test. Les contrôles sans consommateur sont supprimés ou marqués
   « affichage seul ».
3. **Portée de `post_rotation_probe`** : WARP-only par défaut (le probe est
   branché sur la rotation WARP), ou commun si un probe Docker équivalent est
   planifié. Le libellé doit refléter la décision.
4. **Mode Direct** : la section commune reste visible mais indique
   explicitement qu'elle ne s'applique pas au trafic direct ; aucun réglage
   n'est appliqué silencieusement.
5. **Isolation des sections** : NordVPN-only dans `VpnServers` et la partie
   stack/serveur/station_count de `VpnRouting` ; WARP-only dans la liste
   d'endpoints ; commun dans un panneau dédié visible en NordVPN et WARP.
6. **Capacité/statut** : par mode, sans mélange Docker/WARP ; `Direct` n'a pas
   de capacité de station.

## Phase 1 — Design

Artefacts : [data-model.md](./data-model.md),
[contracts/](./contracts/), [quickstart.md](./quickstart.md).

- Modèle : `EgressMode`, `RoutingParameter`, `ModeCapability`,
  `StatusSnapshot`.
- Contrats : sélecteur de mode et sections ; paramètres communs et leur
  sémantique par backend ; statut/capacité par mode.
- Quickstart : scénario manuel de validation des trois modes, du partage des
  paramètres et de la capacité.

## Phase 2 — Tasks

[tasks.md](./tasks.md) contient le découpage exécuté et coché.

## Re-check Constitution (post-design)

Tous les principes restent PASS. Le point sensible est **XII** : la matrice
`research.md` doit couvrir 100 % des contrôles visibles. Aucun contrôle ne sera
conservé sans consommateur ou mention « affichage seul ».

## Complexity Tracking

Aucune violation. Pas de nouveau module, pas de nouvelle dépendance, pas de
migration de données.
