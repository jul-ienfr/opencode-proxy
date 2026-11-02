# Plan de correction — performance, fiabilité, qualité, modularité

> 2026-11-02. Suite de l’audit `docs/AUDIT_PRESELECTION_QUOTA_2026-11-02.md`
> et des preuves `docs/evidence/ip-preselection-2026-11-02-*.json`.
> Statut : proposition à valider. Aucun changement runtime n’est inclus ici.

## 0. État d’exécution au 2026-11-02

| Item | État | Preuve |
|---|---|---|
| P0.1 migration `server_host` | **fait** | 13 lignes attribuées / 3 hostnames |
| P0.2 alerte attribution | **fait** | métrique `host_attribution` + warning à 20 manquants |
| P0.3 statut registre | **fait** | `GET /api/free/ip-registry/status` |
| P0.4 audit strict | **fait pour `ip_registry`** | `ip_rotation` reste au niveau feuille (P5.1) |
| P0.5 contrat schéma | **fait** | `tests/test_requests_schema_contract.py` |
| P0.6 isolation des tests | **fait** | reset autouse `_vpn_manager`/pool/attempt/vault |
| P1.1 rebuild qualité | **fait** | 7 entrées / 3 hosts au boot |
| P1.2 index hostname | **fait** | `idx_requests_server_host_time` |
| P1.3 ratio d’attribution | **fait** | `host_attribution.coverage` |
| P1.4 rapport robuste | **fait** | retry + `source_unavailable` + `--output` |
| P1.5 preuve après redémarrage | **partiel** | JSON 1 h / 24 h ; cible 72 h |

Reste avant P2 : laisser tourner 24 h, vérifier la couverture ≥ 95 % et
≥ 10 hostnames `n ≥ 3`.

### Mise à jour 2026-11-02 — allocateur toutes-stations

Le contrat opérateur a été étendu : toutes les stations sont réparées
indépendamment, avec une IP propre et un catalogue partagé. L’allocateur passe
directement en `enforce` (pas de phase observe) :

- superviseur toutes-stations, 2 chaînes en parallèle, budget global de rebind ;
- `CatalogClaims` : un hostname testé par une seule station, 429 non retestés
  avant reset ;
- `ComposeHostnameBinder` : override Compose **par station** (le fichier unique
  causait des binds croisés) ;
- `SqliteIpRegistry.assign_observed()` : un 200 est écrit dans `ip_state`,
  `observations` et `assignments`, puis conservé ;
- réconciliation des assignations avec l’état live du proxy.

Preuve live : station 2 → `us13317.nordvpn.com` / `146.70.238.59`, probe 200,
assignée. Détail : [`ALLOCATOR_ALL_STATIONS.md`](ALLOCATOR_ALL_STATIONS.md).

## 1. Ce que l’audit apprend

L’audit n’a pas seulement trouvé des bugs : il a trouvé des **écarts entre ce
que le système prétend faire et ce qu’il fait réellement**.

| Constat | Preuve | Leçon |
|---|---|---|
| La qualité était mesurée sans être attribuée | 795 IP, 10 783 requêtes, **0 hostname** sur 72 h | Une mesure sans clé d’attribution est inexploitable : la présélection ne pouvait pas fonctionner |
| La documentation avait précédé le code | `server_host` documenté en phase 0, absent du schéma `requests` | Une doc n’est pas un contrat ; seuls les tests de schéma le sont |
| Des réglages existaient sans effet | 8 clés `ip_registry.*` mortes | Un réglage inerte est pire qu’un réglage absent : il donne une fausse impression de contrôle |
| Une feature peut être `enabled` et ne rien faire | présélection active, **50/50 candidats en tier B** | Le flag dit l’intention, pas la capacité ; il faut mesurer la sortie |
| La rotation qualité était désactivée | `quality_rotation.enabled=false` | C’était le bon choix : sans hostname, elle aurait brûlé des IP au hasard |
| Les dépendances externes sont fragiles | premier run JSON : timeout API Albanie → 0 candidat | Un rapport doit distinguer « aucun candidat » de « source indisponible » |
| Le vert des tests n’est pas une preuve d’intégration | 34 tests Docker exclus, mocks ayant masqué pin/IP distincte/curl_cffi | Les doubles de test doivent porter les mêmes invariants que le runtime |
| Le registre est construit mais inactif | `ip_registry.enabled=false`, `nordcache` sans décodeur | La valeur d’un registre est nulle tant qu’il n’est pas en `observe` puis canary |
| Le quota est la partie la plus mature | tests ledger/429 verts, `reset_at=max(...)` | C’est la zone à protéger pendant l’activation du registre |

**Conclusion centrale** : le prochain gain n’est pas “plus de code”. C’est
**rendre la chaîne mesurable, activable par étapes et vérifiable par des
contrats**. Performance, fiabilité et qualité viendront de cette discipline,
pas d’optimisations prématurées.

## 2. Principes de correction

1. **Mesurer avant d’optimiser** : aucun réglage de seuil sans distribution
   réelle (tok/s, TTFB, 429) sur au moins 72 h.
2. **Provenance explicite** : `source_id`, `stale`, `no-adapter`,
   `rebindable=false`. Jamais de fallback silencieux.
3. **Activer par étapes** : `off → observe → canary → enforce`, avec rollback
   par flag et critère de sortie mesurable.
4. **Un seul propriétaire par décision** : le fresh-chain possède les stations
   pinnées ; l’allocateur ne doit pas se battre avec lui.
5. **Contrats exécutables** : schéma DB, ports, config et fixtures de test
   vérifiés par le code, pas par la prose.
6. **Une modification = une mesure** : ne pas mélanger activation registre,
   rotation qualité et optimisation catalogue dans la même fenêtre.

## 3. Plan par horizon

### P0 — Rendre la vérité vérifiable (48 h)

Objectif : supprimer les angles morts qui ont permis à l’audit de trouver des
écarts.

| ID | Action | Critère d’acceptation |
|---|---|---|
| P0.1 | Redémarrer le proxy pour appliquer la migration `requests.server_host` | `SELECT COUNT(*) FROM requests WHERE server_host IS NOT NULL` > 0 après la première requête free |
| P0.2 | Ajouter un log/métrique de démarrage « attribution hostname » | alerte si 0 hostname après 20 requêtes free |
| P0.3 | Exposer un statut `ip-registry` : enabled, source, stale, comptes par source, `no-adapter` | `GET /api/free/ip-registry/status` répond sans secret et sans blocage |
| P0.4 | Rendre l’audit config strict sur `ip_registry.*` et `ip_rotation.*` | une clé inconnue ou non lue fait échouer le test, pas seulement un rapport |
| P0.5 | Ajouter un test de contrat du schéma `requests` piloté par une liste unique de colonnes | impossible de re-décaler 35/36 colonnes sans échec |
| P0.6 | Isoler les états globaux des tests (`_vpn_manager`, `_free_ip_pool`, `_current_free_attempt`, vault d’identité) | suite exécutable dans l’ordre inverse sans échec |

### P1 — Attribution et présélection prouvée (semaine 1)

Objectif : faire passer la présélection de « code présent » à « qualité
mesurée et utilisée ».

- **P1.1 Rebuild qualité au boot** : reconstruire le registre qualité
  `host → EWMA tok/s, TTFB, succès, n` depuis `requests` sur 7 jours
  (`GROUP BY server_host`). Sans cela, chaque redémarrage repart à froid et la
  présélection retombe sur le `load`.
- **P1.2 Index DB** : ajouter `idx_requests_server_host_time(server_host,
  timestamp)` pour que le rebuild reste < 2 s.
- **P1.3 Ratio d’attribution** : métrique
  `free_requests_with_host_total / free_requests_total`, objectif ≥ 95 % après
  24 h.
- **P1.4 Rapport de présélection robuste** : retry/backoff sur l’API pays et
  recommendations, cache 5 min, statut explicite `source_unavailable` au lieu
  d’un rapport vide. Le mode `--output` existe déjà.
- **P1.5 Preuve avant/après** : relancer 24 h et 72 h après redémarrage et
  comparer tiers A/B/C, couverture hostname, 429 et TTFB.

Critère de sortie : ≥ 10 hostnames avec `n ≥ 3`, au moins un tier A, et aucun
rapport « 0 candidat » confondu avec une panne API.

### P2 — Registre en `observe` (semaine 2)

Objectif : activer le registre sans toucher au routage.

- **P2.1 Hook de démarrage non bloquant** : refresh catalogue (TTL 6 h),
  `sync_snapshot`, `prune_history`, journal `catalog_snapshots`.
- **P2.2 Mode `observe`** : aucune assignation, aucune rotation ; seulement
  catalogue + observations + éligibilité calculée.
- **P2.3 API/dashboard** : source, `stale`, dernier ETag, comptes par source,
  IP éligibles/exhausted, leases expirés, `no-adapter`.
- **P2.4 Métriques** : durée refresh, âge snapshot, `stale`, tests/heure,
  leases expirés, erreurs binder.
- **P2.5 Benchmark SQLite** : 10 000 lignes, `eligible()` p95 < 50 ms,
  `claim()` < 10 ms ; test CI avec `EXPLAIN QUERY PLAN`.
- **P2.6 Comparaison shadow** : le registre propose un classement, le pool
  réel reste décisionnaire ; écrire les divergences.

Critère de sortie : 24 h en `observe`, 0 impact sur le trafic, catalogue
toujours disponible si l’API tombe (`stale` visible).

### P3 — Allocateur canary (semaines 3-4)

Objectif : prouver le cycle `need → claim → bind → probe → assign` sur **une**
station.

- **P3.1 Propriété fresh-chain** : l’allocateur ignore les stations pinnées ou
  le fresh-chain est mis en pause pendant le canary. Décision écrite, pas
  implicite.
- **P3.2 Canary station 1** : `allocator.mode=enforce` pour une seule station ;
  les autres restent `observe`.
- **P3.3 Chaos minimal** : crash pendant claim, lease expiré, DB verrouillée,
  binder timeout, source down.
- **P3.4 Rollback** : `allocator.mode=off` en une commande, sans migration
  destructive ; vérifier que la station reprend l’ancien chemin.
- **P3.5 Critère de sortie** : 200 via allocateur, 0 double assignation,
  reprise < 60 s après crash, 24 h sans churn.

### P4 — Qualité et performance (semaines 5-8)

Objectif : utiliser les données accumulées, puis optimiser ce qui est mesuré.

- **P4.1 Rotation qualité canary** : activer sur une station seulement, avec
  `min_requests=10`, `min_samples=3`, `max_per_hour=6`, garde-fou flotte lente.
  Comparer 429, TTFB et tok/s avant/après.
- **P4.2 Seuils dérivés des données** : calculer les quantiles tok/s par
  hostname sur 7 jours ; remplacer les 55/25 actuels par des seuils justifiés.
- **P4.3 Sonde post-rotation** : 1–4 tokens, 2 essais maximum, derrière flag ;
  mesurer le coût réel (quota + AUTH) avant activation.
- **P4.4 Catalogue allégé** : `fields[...]` + gzip + cache conditionnel ;
  objectif < 10 Mo et refresh < 15 s au lieu des ~29 Mo actuels.
- **P4.5 SQLite batch** : `executemany` pour le sync catalogue, `PRAGMA`
  vérifiés, objectif 10 000 upserts < 2 s.
- **P4.6 Sources parallèles bornées** : fetch primaire/fallback avec timeout,
  backoff et disjoncteur ; jamais de blocage du chemin de requête.
- **P4.7 Budget de tests** : présélection et allocation ne testent que les
  stations en besoin ; `max_tests_per_hour` réintroduit quand le job existe.

### P5 — Maintenabilité et modularité (continu)

- **P5.1 Schéma de config typé** pour `ip_registry` et `ip_rotation` :
  validation au boot, clés inconnues refusées, valeurs bornées. Remplace
  l’audit permissif par une vraie source de vérité.
- **P5.2 Règle de dépendance** : `domain` n’importe jamais `adapters`,
  `opencode`, `sqlite3` ; test automatisé (import-linter ou test AST).
- **P5.3 Contrats de ports** : un test par port (`CatalogSource`, `IpRegistry`,
  `StationBinder`, `ProbeClient`) ; les fakes doivent porter les invariants
  réels (pin, IP distincte, mode proxy, curl_cffi).
- **P5.4 Tests d’isolation** : reset autouse des globals, exécution d’un
  sous-ensemble en ordre inversé dans la CI.
- **P5.5 Docs-as-code** : chaque chiffre d’un rapport doit venir d’un JSON de
  preuve versionné ou d’une commande reproductible.
- **P5.6 Extraction ciblée** : sortir de `opencode.py` les hotspots
  `_save_request`, préflight free et chemin direct, avec tests dédiés. Pas de
  refonte big-bang.

## 4. Matrice des axes

| Axe | Actions principales | KPI |
|---|---|---|
| **Performance** | fields/gzip, batch SQLite, cache recommendations, rebuild qualité indexé | refresh < 15 s ; 10k upserts < 2 s ; eligible p95 < 50 ms |
| **Fiabilité** | observe → canary, leases, chaos, rollback, source circuit breaker | 0 double assignation ; reprise < 60 s ; 0 fallback silencieux |
| **Qualité** | attribution hostname, rebuild 7 j, seuils data-driven, canary rotation | ≥ 95 % requêtes attribuées ; ≥ 10 hosts n ≥ 3 ; tier A ≥ 1 |
| **Modularité** | ports + contrats, sources isolées, règle de dépendance | 0 import domaine → infra ; nouvel adaptateur sans toucher au domaine |
| **Maintenabilité** | config typée, schéma généré, tests isolés, docs-as-code | 0 clé morte ; 0 test flaky/20 runs ; suite < 5 min |
| **Optimalité** | lazy allocation, budget de tests, pas de scan si stations saines | tests/heure bornés ; aucun test inutile |

## 5. KPI et cibles

| Métrique | Base 2026-11-02 | Cible | Mesure |
|---|---:|---:|---|
| Requêtes free avec `server_host` | 0 % (0 / 10 783) | ≥ 95 % après 24 h | `requests` |
| Hostnames avec n ≥ 3 | 0 | ≥ 10 en 72 h | rapport JSON |
| Candidats tier A | 0 / 50 | ≥ 1 | rapport JSON |
| Catalogue : serveurs | 9 948 | stable ± 5 % | `catalog_snapshots` |
| Catalogue : refresh | ~29 Mo, non mesuré | < 10 Mo, < 15 s | benchmark |
| `eligible()` 10k | non mesuré | p95 < 50 ms | benchmark CI |
| `claim()` | non mesuré | p95 < 10 ms | benchmark CI |
| Double assignation | non exercé | 0 | test invariant |
| Reprise après crash | non exercé | < 60 s | chaos test |
| Suite complète | 4 452 passés, 34 Docker exclus | 0 flaky/20 runs + job Docker | CI |
| Clés config mortes | 0 | 0 en continu | audit strict |

## 6. Risques et arbitrages

| Risque | Impact | Décision |
|---|---|---|
| Allocateur vs fresh-chain | deux propriétaires sur la même station | l’allocateur ignore les stations pinnées ; canary hors fresh-chain |
| Plafond NordVPN (4 tunnels) | canary + rotations se disputent les sessions | canary sur une seule station, rotations qualité désactivées |
| Rotation qualité prématurée | brûle IP + AUTH sans preuve | reste `false` jusqu’à n ≥ 3 par hostname |
| Historique sans hostname | pas de backfill fiable | assumer 0 % avant redémarrage, ne pas joindre l’IP d’entrée |
| API non officielle | schéma/format peut changer | adaptateur + tests de contrat + fallback `stale` |
| Activation registre | latence ou blocage au boot | refresh en tâche de fond, jamais dans le chemin requête |
| `nordcache` non décodé | fallback déclaré mais indisponible | garder `no-adapter` visible ; ne pas le promouvoir |

## 7. Ce qu’on ne fait pas maintenant

- Décoder `nordcache` sans fixture versionnée et tests de contrat.
- Reconstruire l’historique `server_host` : impossible de façon fiable.
- Activer `allocator.mode=enforce` sur toute la flotte avant canary.
- Activer la rotation qualité avant d’avoir ≥ 3 mesures par hostname.
- Ajouter de nouvelles sources tant que les sources actuelles ne sont pas
  prouvées en production.
- Refondre `opencode.py` en bloc : extractions ciblées uniquement.

## 8. Prochaines 48 h

1. Redémarrer le proxy et vérifier `server_host` (P0.1).
2. Ajouter le statut `ip-registry` + la métrique d’attribution (P0.3, P1.3).
3. Rebuilder la qualité au boot depuis `requests` (P1.1).
4. Ajouter retry/backoff et statut `source_unavailable` au rapport (P1.4).
5. Relancer la preuve 24 h après redémarrage et comparer aux JSON du
   2026-11-02 (P1.5).
6. Rendre l’audit config strict sur les sections `ip_registry`/`ip_rotation`
   (P0.4).

## 9. Definition of Done

- P0 : migration appliquée, statut visible, audit strict, tests isolés.
- P1 : ≥ 95 % d’attribution, ≥ 10 hosts n ≥ 3, preuve JSON versionnée.
- P2 : 24 h en `observe`, 0 impact trafic, fallback `stale` visible.
- P3 : canary 200, 0 double assignation, rollback testé.
- P4 : seuils data-driven, refresh < 15 s, benchmarks CI verts.
- P5 : config typée, contrats de ports, suite sans flaky.
