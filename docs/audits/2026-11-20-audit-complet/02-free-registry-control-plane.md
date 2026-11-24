# Audit 2026-11-20 — 02 · `free/ip_registry` · `free/control_plane` · `free/qualification` · catalogue d'egress

**Zone auditée** : `free/ip_registry/**` (25 fichiers, 4 623 l.), `free/control_plane/**`
(8 fichiers, 2 143 l.), `free/qualification/**` (38 fichiers, 7 801 l.),
`free/egress_catalog.py` (440 l.), `free/catalog_claims.py` (534 l.),
`free/egress_sources.py` (424 l.), `free/scan_audit.py` (297 l.),
`free/threat_intel.py` (211 l.), `free/profile_registry.py` (604 l.),
`free/profiles/`, plus la couche de persistance `rotation/infrastructure/db/**`
(1 105 l.) qui est le **propriétaire réel du schéma de `registry.db`**.

**Méthode** : lecture intégrale par tranches (`Read` + `limit`/`offset`), `Grep`
ciblé de preuve d'usage sur tout le repo (hors `Lib/`, `_research/`, `.audit-wt/`,
`.audit-logs-wt/`, `node_modules/`, `logs/`), et **mesures en lecture seule sur la
base de production** (`file:logs/registry.db?mode=ro`, `uri=True`). Aucun fichier
de code modifié, aucune écriture en base, `logs/requests.db` et `logs/debug.log`
jamais ouverts.

**Base mesurée** : `logs/registry.db` — 159,4 Mo (40 804 pages × 4 096),
WAL 5,25 Mo, **52 tables**, **96 index**, `schema_migrations` max = **3**.
Mesures du 2026-11-20 ≈ 13:05 UTC.

---

## 1. Résumé exécutif

1. **`registry.db` n'a pas un propriétaire de schéma mais six.** 52 tables vivent
   dans la base ; **30 seulement** sont dans `rotation/infrastructure/db/schema.sql`.
   22 tables sont créées ailleurs, par **15 modules** différents, et 4 tables
   (`server_transports`, `station_state`, `incidents`, `repair_jobs`) sont
   déclarées **deux fois avec des DDL divergentes** — premier écrivain gagne,
   silencieusement (principe III, XIII).
2. **Les clés étrangères ne sont pas appliquées** : 5 des 6 connexions qui écrivent
   dans `registry.db` omettent `PRAGMA foreign_keys=ON`. Mesure :
   `PRAGMA foreign_key_check` → **15 179 violations** (toutes
   `server_transports.hostname → servers.hostname`), parce que
   `store.upsert_server_transports` insère des hostnames sans ligne `servers`.
3. **Une boucle d'écriture non bornée tourne en production** : la passe de
   qualification en mode shadow re-claim le **même** hostname 40 fois et écrit une
   ligne `qualification_epochs` toutes les 60 s. Mesure : **21 220 epochs, dont
   21 182 avec 0 tentative**, 2 344 epochs aujourd'hui, et
   `SUM(tested)` = **846 983** pour **41 118 tentatives réelles** (×20).
4. **La rétention configurée n'est jamais exécutée** : `retention_days: 7`
   n'atteint que `store.purge_attempts` (`store.py:1443`), qui n'a **aucun appelant
   en production** (seulement `tests/test_qualification_store.py:216`).
   `qualification_attempts` (41 k), `incidents` (35 k), `scan_attempts` (61 k),
   `egress_observations` (327 k) croissent sans purge applicative fiable.
5. **Les sauvegardes n'ont aucune rétention** : 186 fichiers, **11,79 Go** dans
   `logs/backups/`, une copie complète de ~140 Mo à chaque maintenance (2×/jour),
   et la copie produite n'est **pas** vérifiée par `integrity_check` (c'est la base
   source qui l'est).
6. **Le TTL des profils d'identité n'est pas appliqué** : `refresh_status` dégrade
   `verified → stale` **une seule fois**, puis re-promeut au 2ᵉ appel avec un
   nouveau `expires_at = now + 7 j` **sans nouvelle preuve**. Les deux profils
   livrés sont expirés depuis le 2026-11-13 et l'un référence un fichier de capture
   supprimé (principe VII).
7. Le chemin d'import est un god-init : `from free.ip_registry.registry_db import
   reconcile` (`opencode.py:3589`) charge **60 modules `free.*`** dont
   `free.control_plane`, `adapters/probe` et `compose_binder` — **1,14 s** mesuré
   à froid, pour un `reconcile()` qui n'en a pas besoin.
8. `free/qualification/store.py` (1 475 l.) est à **25 lignes** de la limite
   constitutionnelle XIV ; `readmodel.py` (971 l.) et `sqlite_registry.py`
   (1 039 l.) sont les suivants.
9. Aucun `TODO`/`FIXME`/`XXX`/`HACK` n'existe dans la zone (vérifié par `grep`) ;
   la dette est tracée dans `docs/debt.md` (D-014-07/09/10/11/12) et les marqueurs
   présents sont des notes « legacy / cold path » **opposables par un test**
   (`tests/test_no_docker_exec_hot_path.py:100-105`).
10. `free/egress_catalog.py`, `free/egress_sources.py` et `free/threat_intel.py`
    sont propres (domaine pur, aucune I/O SQLite pour le premier ; I/O réseau
    uniquement en tâche de fond pour les deux autres).

---

## 2. Carte des modules (rôle réel, LOC, appelants)

### 2.1 `free/ip_registry/`

| Module | LOC | Rôle réel | Appelants (production / scripts / tests) |
|---|---|---|---|
| `__init__.py` | 67 | Re-export **eager** de 6 adapters + allocator + sources + sqlite_registry + status | Chargé par **tout** import `free.ip_registry.*`, donc par `opencode.py:3589`, `scripts/fresh_chain*.py`, `free/ip_state.py:175`, `free/probe_budget.py:51`, `free/control_plane/events.py:19` |
| `registry_db.py` | 407 | Surface runtime des leases (`assigned_for_station`, `release_station`, `assign_observed`, `reconcile`, `ingest_control_events`) | **`opencode.py:3589`** (lifespan), `scripts/fresh_chain_supervisor.py:361-366`, `free/ip_state.py:175`, `free/probe_budget.py:51`, `free/control_plane/events.py:19` |
| `sqlite_registry.py` | 1 039 | `SqliteIpRegistry` legacy (`ip_state`/`assignments`, base `logs/ip_registry.db` = **0 octet**) | `scripts/ip_registry_{canary,discover,observe}.py` + tests. **Aucun appelant runtime** |
| `sqlite.py` | 193 | Schéma legacy v1→v2 + `connect`/`ensure_schema` | vivant uniquement via `sqlite_registry.py:32` |
| `domain.py` | 224 | Dataclasses pures + `can_transition` | 15 fichiers internes ; `can_transition` **mort** |
| `config.py` | 214 | Loader YAML `ip_registry` | `status.py:17`, `scripts/fresh_chain_supervisor.py:42`, 3 scripts CLI |
| `status.py` | 395 | Payload dashboard `GET /api/free/ip-registry/status` | **`dashboard/routers/free_routes.py:28,50-55`**, enregistré par `dashboard/api.py:2617` |
| `preflight.py` | 280 | Préflight DNS/TCP/UDP des candidats | `scripts/qualification_daemon.py:65-67` **seulement si `--preflight`** (jamais passé) |
| `catalog.py` | 174 | Wrapper catalogue `NordVpnCatalog` | `scripts/ip_registry_{discover,observe}.py` |
| `scoring.py` | 94 | Score `[0,100]` d'un `IpRecord` | `sqlite_registry.py` uniquement |
| `eligibility.py` | 58 | Prédicat « cette IP est-elle réclamable » | **aucun appelant hors tests** (dupliqué par du SQL) |
| `allocator.py` | 144 | Boucle need→claim→bind→probe→assign | `scripts/ip_registry_canary.py` + tests |
| `ports.py` | 70 | 6 `Protocol` | `allocator.py:14` ; 3 des 6 jamais référencés |
| `dns.py` | 52 | Échelle DNS par station | `scripts/fresh_chain.py:1970` |
| `sources/` (6 fichiers) | 559 | Manifeste + registry de sources + parseurs | `status.py:323` (**vivant**), `catalog.py`, tests |
| `adapters/probe.py` | 499 | Sonde officielle free (payload exact, identité par IP, SOCKS5h→SOCKS5) | `scripts/fresh_chain.py:2888`, `free/qualification/station_adapter.py:209`, 2 scripts manuels |
| `adapters/compose_binder.py` | 427 | Recréation Compose (`up -d --force-recreate --no-deps`) | `free/qualification/station_adapter.py:178`, `scripts/ip_registry_{canary,discover}.py` |
| `adapters/hybrid_binder.py` | 182 | Dispatcher control-API ↔ Compose | **`free/qualification/station_adapter.py:208` seul** (chemin qualifieurs) |
| `adapters/gluetun_control.py` | 200 | Binder froid (`docker cp` + `docker exec wget`) | `scripts/fresh_chain.py:2902` (défaut), `station_adapter.py:188` |
| `adapters/gluetun_control_v2.py` | 358 | Binder chaud (client HTTP control plane) | `scripts/fresh_chain.py:2916` (**binder actif**) |
| `adapters/egress.py` | 287 | Détection d'IP d'egress en couches | `scripts/fresh_chain.py:2876` (`host_public_ip`), `station_adapter.py:177`, supervisor |

### 2.2 `free/control_plane/`

| Module | LOC | Rôle réel | Appelants |
|---|---|---|---|
| `client.py` | 538 | Client HTTP typé + idempotent du fork Gluetun | **`dashboard/api.py:2577`** (6 routes `/control/*`), `gluetun_control_v2.py`, `scripts/fresh_chain.py:2917`, `scripts/rollout_control_plane.py` |
| `config.py` | 218 | Loader YAML `control_plane` | `scripts/fresh_chain.py:2910`, `dashboard/api.py:2578` |
| `models.py` | 375 | Vues dataclass du contrat JSON | `client.py`, `gluetun_control_v2.py`, `dashboard/routers/station_control_routes.py` |
| `errors.py` | 103 | Hiérarchie d'erreurs typées | `client.py`, `gluetun_control_v2.py` |
| `events.py` | 429 | Ingestion idempotente `/v1/events` → `registry.db` | **Aucun appelant hors tests.** `control_events`/`control_cursor` **absentes** de la base live |
| `shadow.py` | 325 | Observateurs shadow (switch `dry_run`, comparaison transport) | **Aucun appelant hors tests** ; `is_shadow` n'a **zéro** consommateur |
| `telemetry.py` | 171 | Sink SQLite shadow (`logs/telemetry.db`) | **Aucun appelant hors tests** ; `logs/telemetry.db` **n'existe pas** |
| `__init__.py` | 84 | Shim de re-export | `scripts/fresh_chain.py:2729` uniquement |

### 2.3 `free/qualification/`

| Module | LOC | Rôle réel | Appelants |
|---|---|---|---|
| `store.py` | **1 475** | **Seul écrivain de `registry.db` de la zone** (`QualificationStore`) | `runtime.py:116`, `chain_bridge.py:41-50`, `apply.py`, `dashboard/routers/qualification_routes.py:386,476`, `qualifiers.py:488` |
| `readmodel.py` | 971 | Projections lecture seule du dashboard (18 routes) | `dashboard/routers/qualification_routes.py` (18 sites) |
| `qualifiers.py` | 598 | Manager des slots 7-10 (Docker + sweep live) | **`opencode.py:4238`** (toujours construit ; `enabled: false`) |
| `sweep.py` | 439 | Claim 1 host → transports → tentatives → promotion | `qualifiers.py:437`, `scripts/qualification_{daemon,sweep}.py`, `scripts/fresh_chain.py:3228` |
| `scheduler.py` | 267 | Boucle périodique (discovery + watchdog + preflight + sweep) | `scripts/qualification_daemon.py:81` seulement |
| `watchdog.py` | 292 | Probe station → incident → réparation bornée | `runtime.py:170`, `scripts/qualification_sweep.py:113` |
| `runtime.py` | 316 | Racine de composition | `qualifiers.py:488`, 3 scripts, `apply.py:106` |
| `domain.py` | 307 | Cycle de vie / classification / backoff purs | 13 fichiers internes |
| `source.py` / `discovery.py` | 210 / 184 | Lecture `logs/nordvpn_api.json` → candidats ; run de découverte | `runtime.py:122,154` |
| `station_adapter.py` | 233 | Pont vers les binders/prober de `ip_registry` | `apply.py:97`, `cutover_adapter.py`, `qualifiers.py:477`, `scripts/fresh_chain.py:3196` |
| `preflight.py` | 240 | Préflight relatif à l'opérateur | **inatteignable** (`--preflight` jamais passé par `daemon_control.py`) |
| `scoring.py` | 180 | `potential_score` / `measured_score` | `source.py`, `preflight.py`, `store.py:26` |
| `transport.py` | 187 | Plan de cycle des transports | `sweep.py:30` |
| `station_runtime.py` | 170 | Workers asyncio par station | `scripts/load_qualification.py` seulement (hors gate CI) |
| `repair_adapter.py` | 51 | `SupervisorRepairAdapter` → `ops.supervisor.restart` | `scripts/qualification_sweep.py:110,116` |
| `transport_health.py` | 155 | Agrégat incrémental `transport_health` | `store.py:1265,1332` |
| `diagnostics.py` / `operator.py` / `repair_store.py` / `epochs.py` | 53 / 41 / 23 / 110 | Mixins et helpers du store | `readmodel.py:19`, `store.py:23-27` |
| `metrics.py` | 97 | Snapshot JSON/Prometheus | `qualification_routes.py:342` |
| `cutover.py` / `rotation.py` / `rotation_executor.py` / `cutover_adapter.py` / `apply.py` | 175 / 146 / 121 / 58 / 137 | Chaîne de rotation make-before-break | `dashboard/api.py:2869`, `qualification_routes.py:472` — **`station_cutovers` = 0 ligne** : jamais exécutée en production |
| `rotation_batch.py` | 74 | Rotation parallèle bornée | **Aucun appelant hors tests** |
| `canary.py` / `release.py` | 183 / 64 | Canary et manifeste de release | `scripts/{canary,release}_qualification.py` |
| `daemon_control.py` / `supervisor_bridge.py` / `chain_bridge.py` / `station_probe.py` | 40 / 115 / 98 / 130 | Lancement du daemon, tick superviseur, tentatives de chaîne, probe HTTP | `scripts/fresh_chain_supervisor.py:2204`, `scripts/fresh_chain.py:2394-2534`, daemon |

### 2.4 Modules racine de la zone

| Module | LOC | Rôle réel | Appelants |
|---|---|---|---|
| `free/egress_catalog.py` | 440 | Domaine **pur** (stdlib seul) : `EgressOption`, `score_option`, `transition` | 19 fichiers (`free/egress_*`, `free/pool` indirect) |
| `free/catalog_claims.py` | 534 | Catalogue + claims + budget de rebind global | **`scripts/fresh_chain.py:1972`** seul écrivain |
| `free/egress_sources.py` | 424 | Registry de sources proxys publics + parseurs + fetch SSRF-guardé | `free/egress_adapters/public.py:28`, `free/egress_runtime.py:30`, `free/threat_intel.py:19`, dashboard |
| `free/scan_audit.py` | 297 | Piste d'audit `scan_runs`/`scan_attempts` | `scripts/fresh_chain.py:2001`, `scripts/fresh_chain_supervisor.py:3264` |
| `free/threat_intel.py` | 211 | Threat intel (5 feeds) dans `egress_threat_intel` | `free/egress_runtime.py:368`, `public.py:236,652` |
| `free/profile_registry.py` | 604 | Registre de profils vérifiés + TTL + assignation | **`opencode.py:10832`** (chemin free, `free.station_identity: true`) |
| `free/profiles/verified_profiles.json` | 122 l. | 2 profils, tous deux `expires_at` = 2026-11-13 (expirés) | `profile_registry.load_registry` via `opencode.py:10825` |

---

## 3. Schéma de `registry.db` tel qu'implémenté + écarts avec la constitution XIII

### 3.1 Qui crée quoi

| Propriétaire DDL | Fichier:ligne | Tables créées | Dans `schema.sql` ? |
|---|---|---|---|
| Schéma canonique | `rotation/infrastructure/db/schema.sql` (538 l.) | **30** | — |
| Store de qualification | `free/qualification/store.py:29-255` | **12** | 12/12 **dupliquées** |
| Store d'egress | `free/egress_store.py:30-157` | **9** | **0** |
| Claims (runtime) | `free/catalog_claims.py:41-68` | 3 | 0 |
| Claims (migration) | `rotation/infrastructure/db/claims.py:13-39` | 3 | 0 (copie **octet pour octet** de `catalog_claims`) |
| Scan audit | `free/scan_audit.py:44-79` | 2 | **2/2 dupliquées** |
| Curseur d'événements | `free/ip_registry/registry_db.py:29-41` | 2 | 0 |
| Legacy ip_registry | `free/ip_registry/sqlite.py:17-130` | 6 | 0 (autre fichier : `logs/ip_registry.db`) |
| Shadow telemetry | `free/control_plane/telemetry.py:19,36` | 2 | 0 (autre fichier : `logs/telemetry.db`) |
| État d'IP free | `free/ip_state.py:49,74` | 2 | 0 |
| Budget de probe | `free/probe_budget.py:23` | 1 | 0 |
| Rotation WARP | `free/warp_rotation.py:71` | 1 | 0 |
| Réserve client | `free/client_reserve.py:74` + `free/reserve_settings.py:42` | 2 | 0 |
| File de réparation | `free/repair_queue.py` | 1 | 0 |
| Observabilité | `observability/db.py` (6 `CREATE TABLE`) | 6 | 0 |

**Mesure** : `52` tables vivantes, `30` dans `schema.sql`, **`22` hors schéma** :

```
['auth_events', 'auth_pauses', 'catalog', 'catalog_runs', 'claims', 'client_reserve',
 'client_reserve_settings', 'egress_catalog_meta', 'egress_observations', 'egress_options',
 'egress_provider_state', 'egress_rotation_slots', 'egress_sources', 'egress_threat_feeds',
 'egress_threat_intel', 'ip_state', 'ip_state_events', 'pins', 'probe_budget',
 'rebind_budget', 'rotation_jobs', 'slo_samples']
```

`only-in-schema: []` — aucune table déclarée n'est absente de la base : la
convergence est réelle, mais elle est obtenue par effet de bord, pas par migration.

### 3.2 Tables et contraintes — état réel (mesuré)

| Table | PK | UNIQUE | CHECK | Lignes |
|---|---|---|---|---|
| `schema_migrations` | `version` | — | — | 3 |
| `catalog_sources` | `source_id` | — | — | 3 |
| `servers` | `hostname` | — | 2 | 2 193 |
| `server_transports` | `(hostname, transport)` | — | 1 | 20 945 |
| `endpoints` | `id` | `(hostname, transport, endpoint_ip)` | 1 | **0** |
| `egress_ips` | `ip` | — | 2 | 6 557 |
| `ip_server_map` | `(ip, hostname, transport)` | — | 1 | 6 288 |
| `observations` | `id` | — | 1 | 8 657 |
| `quota_buckets` | `(ip, model, day, bucket)` | — | — | 8 519 |
| `leases` | `lease_id` | **2 index partiels UNIQUE** | 1 | 2 233 |
| `warm_pool` | `ip` | — | — | 742 |
| `sweep_cursor` | `id` | `(batch_id, country, hostname, transport)` | — | **0** |
| `repair_jobs` | `id` | — | — | **0** |
| `repair_requests` | `station` | — | — | 32 |
| `fallback_events` | `id` | — | — | **0** |
| `incidents` | `id` | — | — | **35 384** |
| `station_state` | `station` | — | 1 | 6 |
| `config_snapshots` | `id` | — | — | **0** |
| `score_snapshots` | `(kind, key, transport)` | — | 1 | **0** |
| `chain_state` | `namespace` | — | — | 27 |
| `scan_runs` | `run_id` | — | — | 3 818 |
| `scan_attempts` | `attempt_id` | — | — | 61 268 |
| `qualification_candidates` | `hostname` | — | — | 7 124 |
| `qualification_attempts` | `attempt_id` | — | — | **41 118** |
| `transport_health` | `(transport, station)` | — | — | 30 |
| `qualification_epochs` | `epoch_id` | — | — | **21 220** |
| `station_runtime` | `station` | — | — | **0** |
| `station_cutovers` | `cutover_id` | — | — | **0** |
| `watchdog_state` | `id` (+`CHECK id=1`) | — | 1 | 1 |
| `discovery_runs` | `id` | — | — | 40 |

**8 tables à 0 ligne** dont 5 jamais écrites par aucun chemin de production :
`sweep_cursor`, `score_snapshots`, `fallback_events`, `config_snapshots`
(0 `INSERT` repo-wide), `station_runtime` et `station_cutovers` (écrites
seulement par `rotation_executor.py`, lui-même jamais atteint).

### 3.3 Le point le plus grave : 4 tables à double définition divergente

`CREATE TABLE IF NOT EXISTS` signifie **premier écrivain gagne**. Deux modules
déclarent les mêmes noms avec des DDL **différentes** :

| Table | `schema.sql` (canonique) | `free/qualification/store.py` | Perdu si `store.py` gagne |
|---|---|---|---|
| `server_transports` | `REFERENCES servers(hostname) ON DELETE CASCADE`, `CHECK transport IN (3)`, `supported DEFAULT 0` | pas de FK, pas de CHECK, `supported DEFAULT 1` | **la cascade FK, le CHECK transport, et le défaut 0→1** |
| `station_state` | `CHECK (state IN (8 états))` | pas de CHECK, `container DEFAULT ''` | **le CHECK des 8 états** |
| `incidents` | `scope TEXT NOT NULL` | `scope TEXT NOT NULL DEFAULT 'station'` | l'exigence de valeur explicite |
| `repair_jobs` | `transports_tried INTEGER NOT NULL DEFAULT 0`, `result TEXT` (nullable) | `transports_tried TEXT NOT NULL DEFAULT ''`, `result TEXT NOT NULL DEFAULT 'running'` | **le type de `transports_tried`** |

**Preuve sur la base live** (`sqlite_master`) : les quatre tables portent bien le
DDL de `schema.sql` (`supported INTEGER NOT NULL DEFAULT 0`,
`CHECK (state IN (...8 états))`, `transports_tried INTEGER NOT NULL DEFAULT 0`) —
donc `apply_schema` a gagné la course **cette fois**. Rien ne garantit l'ordre :
`QualificationStore` est construit depuis le dashboard
(`dashboard/routers/qualification_routes.py:386,476`) et depuis
`free/qualification/runtime.py:116`, tandis que `apply_schema` ne tourne qu'au
boot du superviseur (`scripts/fresh_chain_supervisor.py:3247`) et toutes les 6 h
(`:2245`). **Un démarrage du dashboard avant le premier passage de maintenance
fige définitivement les tables faibles.**

**Collisions de noms d'index à corps différents** — le second `CREATE INDEX IF NOT
EXISTS` est un no-op silencieux :

| Index | `schema.sql` | `store.py` / `sqlite.py` | Live |
|---|---|---|---|
| `idx_transport_supported` | `server_transports(transport, supported, hostname)` | `store.py:133` → `server_transports(supported)` | **canonique** (course gagnée) |
| `idx_servers_country` | `servers(country, stale, score_host DESC)` | `ip_registry/sqlite.py:37` → `servers(country, active)` | canonique |
| `idx_servers_source` | `servers(source_id, last_seen_at DESC)` | `ip_registry/sqlite.py:122` → `servers(source_id, active)` | canonique |
| `idx_incidents_open` | *(absent)* | `store.py:195` → `incidents(ended_at, station, cause)` ; **3ᵉ définition** dans `scripts/station_slo_watch.py:57` → `incidents(station, ended_at)` | celle de `store.py` |

### 3.4 Intégrité référentielle — non appliquée

Une seule connexion active `PRAGMA foreign_keys=ON` dans la zone :

```
rotation/infrastructure/db/migrate.py:57     conn.execute("PRAGMA foreign_keys=ON")
free/egress_store.py:182                     self._conn.execute("PRAGMA foreign_keys=ON")
```

**Absent de** : `free/qualification/store.py:316-320` (le plus gros écrivain),
`free/catalog_claims.py:38-40`, `free/scan_audit.py:33-35`,
`free/ip_registry/registry_db.py` (délègue à `migrate.connect`, donc OK),
`free/quota_ledger.py:725-731` (connexion brute sans PRAGMA).

**Mesure** :

```
PRAGMA foreign_key_check  → 15 179 violations
Counter({('server_transports', 'servers'): 15179})
server_transports : 20 945 lignes / 6 982 hostnames distincts
servers           : 2 193 lignes
=> 5 060 hostnames de server_transports n'ont AUCUNE ligne servers
```

Cause identifiée : `free/qualification/store.py:468-486`
`upsert_server_transports` insère `(hostname, transport, 1)` sans jamais créer la
ligne parente `servers`. Avec `foreign_keys=OFF` l'insert passe ; la base est
durablement incohérente et la FK ne peut plus être activée sans purge préalable.
`quick_check` = `ok` (l'intégrité *structurelle* est bonne — seule la cohérence
référentielle est cassée).

Les autres relations sont propres (mesuré par `LEFT JOIN`) : `leases.ip`,
`ip_server_map.ip`, `ip_server_map.hostname`, `servers.source_id`,
`quota_buckets.ip`, `warm_pool.ip` → **0 orphelin**.

### 3.5 Index : ce qui manque sur les colonnes filtrées

Requêtes chaudes dont le plan est un **scan complet** (mesuré par
`EXPLAIN QUERY PLAN`) :

| Requête | Fichier:ligne | Plan | Coût mesuré |
|---|---|---|---|
| `_aggregate(transport)` | `free/qualification/transport_health.py:35-43` | `SCAN qualification_attempts` + `USE TEMP B-TREE FOR ORDER BY` | 60,9 ms par tentative |
| `egress_ip_in_use` | `free/qualification/store.py:1122-1144` | `SCAN egress_options` | — |
| `list_station_states` (N+1) | `store.py:759-764` | 1 `SELECT` par station | — |
| `open_incidents` sans station | `store.py:832-840` | scan `incidents` | 22 ms |
| `repairs(limit=200)` | `readmodel.py:657` | `SCAN repair_jobs` + tri | — |
| `COUNT(*)` sans WHERE | `readmodel.py:192,194,201` et `store.py:1373,1377,1381` | 3 scans de 41 k lignes | 15,6 ms/appel |

Index **absents** : `qualification_attempts(transport)` — la seule colonne filtrée
par l'agrégat le plus appelé ; `qualification_attempts(finished_at)` — utilisée par
`MAX(finished_at)` et par la purge ; `observations(provider)` — colonne ajoutée par
`migrate.py:20` sans index ; `incidents(station, cause)` — filtrée par
`repair_attempts` et `last_repair_at` ; `egress_options(egress_ip, status)` — le
seul index proche est `idx_egress_options_egress`, **UNIQUE partiel sur
`(provider, egress_ip)`**, inutilisable pour la requête `WHERE egress_ip=? AND
status='usable' AND station<>?`.

**Aucun `ANALYZE` ni `PRAGMA optimize`** n'existe dans le repo (grep sur tout le
code hors tests → 0 résultat), alors que `sqlite_stat1` est présent et que
`egress_threat_intel` a 174 k lignes.

### 3.6 Écarts avec la constitution XIII

| Exigence XIII | État | Preuve |
|---|---|---|
| « clés primaires, clés étrangères, contraintes » | **partiel** | PK partout ; FK déclarées mais **non appliquées** (15 179 violations) |
| « index » | **partiel** | 96 index ; 4 colonnes filtrées sans index sur les tables les plus lues |
| « migrations versionnées » | **partiel** | `schema_migrations` max = 3, mais 22 tables créées hors migration, et `_ADDITIVE_COLUMNS` (`migrate.py:14-29`) applique des `ALTER TABLE` **sans bump de version** |
| « rétention, purge » | **violé** | `retention_days: 7` n'atteint qu'une méthode sans appelant ; 5 tables de rétention dans `retention.py` mais les 6 plus grosses tables de la zone ne sont pas dans la liste |
| « checkpoint WAL » | **OK** | `retention.py:42-43` + `maintenance.run_once:46` (2×/jour) |
| « backup `VACUUM INTO` vérifié » | **partiel** | `retention.py:51-57` fait bien `VACUUM INTO` ; `integrity_ok` vérifie la **source**, pas la **copie** ; aucune rétention de sauvegardes → 11,79 Go |
| « `integrity_check` régulier » | **OK** | `maintenance.py:47` → `retention.py:46-48` |
| « observations append-only » | **OK** | aucun `UPDATE`/`DELETE` sur `observations` hors purge de rétention |
| « scores matérialisés à part » | **partiel** | `score_snapshots` existe (0 ligne) ; `egress_options.score` est un dérivé persisté (`egress_store.py:565`) réparable par `recompute_scores:660` |
| « migration testée sur copie et rollback par backup » | **non** | aucune migration *down* (`grep down\|rollback\|downgrade rotation/infrastructure/db/migrate.py` → 0) ; le rollback repose entièrement sur la sauvegarde disque |

---

## 4. Findings

Convention : **fichier:ligne** absolu, **preuve** = commande ou ligne de code
exacte, **effort** S (< 1 h) / M (1 j) / L (> 1 j).

### 4.1 P0 — corruption de données, boucle non bornée, perte d'état

#### P0-01 — Le mode shadow re-claim le **même** hostname 40 fois par epoch et écrit un epoch toutes les 60 s, sans borne

**Fichier** : `free/qualification/sweep.py:159-162`
**Preuve** (code exact) :

```python
result.claimed += 1
if is_shadow:
    self.store.release(candidate.hostname, status=CandidateStatus.PENDING, now=self._clock())
    continue
```

`store.release` (`free/qualification/store.py:1180-1199`) remet
`lease_until=0` **et** `next_eligible_at=0.0`. `claim_next`
(`store.py:1077-1094`) trie par `potential_score DESC … LIMIT 1` **sans** exclure
la ligne qu'on vient de libérer → la même ligne est re-sélectionnée au tour suivant.
La boucle est `while limit == 0 or result.claimed < limit` (`sweep.py:150`) : avec
`max_hosts=0` elle est **infinie**, avec la valeur configurée (`daemon_max_hosts:
40`, `config.yaml:1196`) elle brûle 40 claims en re-claimant un seul host.

**Mesure en production** (base live) :

```
qualification_epochs ............ 21 220
epochs avec 0 tentative ......... 21 182  (99,8 %)
epochs créés aujourd'hui ........ 2 344
epochs/heure (10 dernières) ..... 83 – 110
SUM(tested) ..................... 846 983   <-- compteur fleet-wide
tentatives réelles .............. 41 118
SUM(usable) ..................... 577
```

Chaque epoch écrit aussi `total=self.store.summary()["total"]` (`sweep.py:110`)
qui exécute **3 scans complets** de `qualification_attempts` (mesuré 15,6 ms), donc
~100 × 15,6 ms = **1,6 s de scans par heure** uniquement pour compter.

**Impact** : `qualification_epochs` est un journal de bord inexploitable (le
dashboard `/epochs` affiche 100 % d'epochs vides), le disque croît de ~100
lignes/h, `SUM(tested)` surestime le travail réel d'un facteur **20,6**, et le
tick superviseur (`scripts/fresh_chain_supervisor.py:2204`) paie une écriture
inutile par minute. Toute lecture d'epoch (canary, SLO, release) est polluée.

**Recommandation** : exclure les hostnames déjà réclamés dans l'epoch
(`AND hostname NOT IN (SELECT …)`) ou, en shadow, itérer un curseur
`top_candidates(limit)` au lieu de re-claim ; et sortir `summary()` de
`create_epoch` (le passer en paramètre optionnel).

**Effort** : S (le correctif), M (le nettoyage de l'historique).

---

#### P0-02 — Les clés étrangères ne sont pas appliquées : 15 179 violations en base

**Fichier** : `free/qualification/store.py:468-486` (cause) et `store.py:316-320`
(absence de PRAGMA)
**Preuve** (code exact, `store.py:480`) :

```sql
INSERT INTO server_transports(hostname, transport, supported)
VALUES(?, ?, 1)
ON CONFLICT(hostname, transport) DO UPDATE SET supported=1
```

Aucune ligne `servers` n'est créée pour ce `hostname`, alors que `schema.sql:55`
déclare `hostname TEXT NOT NULL REFERENCES servers(hostname) ON DELETE CASCADE`.
La connexion (`store.py:316`) ne fait que `journal_mode=WAL` et
`busy_timeout=10000` — **pas de `PRAGMA foreign_keys=ON`**.

**Mesure** :

```
PRAGMA foreign_key_check  → 15 179 violations
Counter({('server_transports','servers'): 15179})
server_transports 20 945 / 6 982 hostnames ; servers 2 193 lignes
```

**Impact** : principe XIII (clés étrangères) violé sur la table de transports ;
les vues `readmodel.transports` (`readmodel.py:820-826`) comptent 6 982 hostnames
pour 2 193 serveurs connus → la couverture de transports affichée est fausse.
Toute activation future de `foreign_keys=ON` échouera sur ces 15 179 lignes.

**Recommandation** : ajouter `PRAGMA foreign_keys=ON` à toutes les connexions de
la zone, faire de `upsert_server_transports` un `INSERT … servers … ON CONFLICT
DO NOTHING` + `INSERT server_transports`, et purger les orphelins existants dans
une migration versionnée.

**Effort** : S (PRAGMA) / M (réparation + migration).

---

#### P0-03 — Le TTL des profils d'identité est neutralisé par une auto-promotion au 2ᵉ appel

**Fichier** : `free/profile_registry.py:242-266`
**Preuve** (code exact) :

```python
if (self.status == STATUS_VERIFIED and self.expires_at is not None
        and ts >= float(self.expires_at)):
    self.status = STATUS_STALE          # 1er appel : dégradation
    return self.status
...
if passes >= MIN_PASSES:
    if self.status != STATUS_VERIFIED:
        self.verified_at = ts           # 2e appel : nouvelle date
        self.expires_at = ts + float(self.ttl_s)   # nouveau TTL de 7 j
        self.status = STATUS_VERIFIED
```

La branche d'expiration ne se déclenche que depuis `STATUS_VERIFIED`. Après le
premier appel la ligne est `stale` : le 2ᵉ appel saute la vérification, lit
`passes >= MIN_PASSES` sur la **même liste d'évidences figées**, et re-promeut
avec un `expires_at` neuf — **sans aucune nouvelle preuve**.

**Preuve d'exécution** (sur le fichier livré, profils expirés depuis le
2026-11-13) :

```
before: status=verified expires_at=1794562485.73 now=1795176454.97 expired? True
call1 -> stale   expires_at=1794562485.73
call2 -> verified verified_at=1795176454.97 expires_at=1795781254.97   (+7 jours)
```

**Impact combiné sur le chemin de requête free** : `free/runtime_identity.py:82-90`
appelle `runtime_profile_versions(registry)` (qui appelle `verified()` → 1ᵉʳ
`refresh_status`, renvoie `()` pour la paire expirée) puis
`select_for_station(..., allowed_versions=())` → rien ; le repli
`registry.canonical()` appelle `verified()` une 2ᵉ fois, ressuscite le profil,
mais `candidate.version in allowed` est faux (`allowed == ()`) → `profile = None`
→ `opencode.py:11106-11108` lève
`RuntimeError("free station identity: aucun profil vérifié")`. La lecture est donc
**dépendante de l'ordre** et à effet de bord. Principe VII (« une mesure non
conservée est une opinion ») : le TTL de 7 j n'est jamais respecté.

**Recommandation** : mémoriser la transition (`if self.status == STATUS_STALE:
return self.status` avant la branche `passes`) et exiger une évidence postérieure
à `verified_at` pour re-promouvoir.

**Effort** : S.

---

#### P0-04 — Une capture référencée n'existe plus : le profil 1.19.5 est devenu un template sans erreur

**Fichier** : `free/profiles/verified_profiles.json:26` +
`free/runtime_identity.py:116-127`
**Preuve** : `"body_capture": "free/captures/1.19.5.json"` alors que
`git status --short free/captures/` → ` D free/captures/1.19.5.json` (supprimé ;
restent `2.2.5.json`, `2.3.0.json`, `archive/`). `_capture_body` fait
`except Exception: return None` → `build_body` (`runtime_identity.py:180-186`)
retombe sur `template_official_body`.

**Impact** : le profil `oc-1.19.5-desktop-11` est déclaré `verified` avec
`body_capture` pointant sur un fichier absent ; le corps envoyé devient un
template reconstruit, donc **pas** la paire octet-exacte annoncée. Principe VII
(« aucune preuve, aucun succès ») : la preuve est un chemin mort, silencieusement.
C'est le profil par défaut de l'identité station (`identity_for(label,
version="2.2.5")` en signature, mais `VERSION_SPECS` dans `free/identities.py:72`
couvre 1.19.5).

**Recommandation** : valider au chargement que chaque `body_capture` existe et a
un `sha256` correspondant ; dégrader le profil en `stale` sinon.

**Effort** : S.

---

#### P0-05 — `ingest_control_events` exécute du DDL dans la transaction d'ingestion et le curseur n'est jamais borné

**Fichier** : `free/ip_registry/registry_db.py:346-352`
**Preuve** (code exact) :

```python
conn.execute("BEGIN IMMEDIATE")
_ensure_control_schema(conn)      # CREATE TABLE IF NOT EXISTS x2, DANS la txn
```

`_ensure_control_schema` (`registry_db.py:44-46`) exécute 2 `CREATE TABLE` à
**chaque** batch d'événements, à l'intérieur d'un `BEGIN IMMEDIATE`. En SQLite un
DDL est transactionnel et prend un verrou d'écriture : chaque ingestion paie le
coût du DDL et bloque les lecteurs plus longtemps que nécessaire.

Aggravant : `control_events` n'a **aucune purge** (ni dans `retention.py`, ni dans
`store.purge_attempts`), et `apply_control_event` fait un `COUNT(*)` par
événement (`free/control_plane/events.py:152` → `_event_count` `:298-303`) sans
index sur `(station, event_type)` → scan des événements de la station à chaque
`job.rolled_back`.

**État actuel** : `control_events` et `control_cursor` sont **absentes** de la
base live — le module n'a jamais tourné en production (aucun appelant hors
`tests/test_control_plane_events.py`). Le défaut est donc latent, mais il
s'activera au premier branchement.

**Impact** : à l'activation, chaque batch paie un DDL transactionnel et chaque
événement un scan ; la table croît sans rétention.

**Recommandation** : sortir `_ensure_control_schema` de la transaction (le faire
une fois à l'ouverture), ajouter `idx_control_events_type(station, event_type)` et
inscrire `control_events` dans la rétention.

**Effort** : S.

---

#### P0-06 — Aucune rétention effective sur les 6 tables les plus volumineuses de la zone

**Fichier** : `free/qualification/store.py:1443-1462` (`purge_attempts`) et
`rotation/infrastructure/db/retention.py:18-39` (`prune`)
**Preuve** :

```
$ grep -rn "purge_attempts" --include=*.py .
./free/qualification/store.py:1443:    def purge_attempts(self, *, now: float | None = None) -> int:
./tests/test_qualification_store.py:216:        assert store.purge_attempts(now=300_000.0) == 1
```

`purge_attempts` a **un seul appelant : un test**. `retention_days: 7`
(`config.yaml:1176`) n'arrive que dans `QualificationStore.__init__`
(`store.py:308-312`, via `runtime.py:118`) et meurt là.

La boucle `prune` de `retention.py:18-39` ne couvre que 5 tables :
`observations`, `fallback_events`, `incidents`, `repair_jobs`, `config_snapshots`.
**Hors rétention** :

| Table | Lignes | Croissance mesurée | Ancienne ligne |
|---|---|---|---|
| `egress_observations` | 326 812 | 38 102 / 24 h | 2026-11-12 |
| `egress_threat_intel` | 174 163 | par `prune_threat_feeds` seulement | — |
| `scan_attempts` | 61 268 | 11 339 / 24 h | 2026-11-05 |
| `qualification_attempts` | 41 118 | 11 232 / 24 h | 2026-11-10 |
| `incidents` | 35 384 | 6 805 / 24 h | 2026-11-07 |
| `qualification_epochs` | 21 220 | 2 344 / 24 h | 2026-11-10 |
| `server_transports` | 20 945 | — | — |
| `egress_options` | 31 900 | — | — |

Note : `egress_observations` **est** purgée par `EgressStore.purge`
(`free/egress_store.py:927-943`) appelée par `EgressRuntime.maintenance`
(`free/egress_runtime.py:161-167`) — mais la boucle dort
`await asyncio.sleep(86_400.0)` **avant** la première exécution
(`egress_runtime.py:172-173`), donc aucune purge avant 24 h d'uptime, et aucune
au shutdown. Mesure : 24 327 lignes de plus de 7 jours sont encore présentes.

**Impact** : la base fait 159,4 Mo pour ~5 tables qui en représentent ~110 Mo
(69 %) ; principe XIII (rétention, purge) violé ; principe XVII (« mémoire et
disque bornés : rétention ») violé. `readmodel.summary` paie 3 scans de 41 k
lignes à chaque cache-miss parce que rien n'est jamais purgé.

**Recommandation** : câbler `purge_attempts` + `checkpoint` dans le tick du
superviseur (à côté de `qualification_tick`, `fresh_chain_supervisor.py:2204`),
ajouter `qualification_epochs`, `scan_attempts`, `qualification_attempts` à
`retention.prune`, et exécuter `EgressStore.purge` au boot **et** après le sleep.

**Effort** : S (câblage) / M (validation de la purge).

---

#### P0-07 — Les sauvegardes n'ont aucune rétention : 11,79 Go en 15 jours

**Fichier** : `rotation/infrastructure/db/retention.py:51-57` et
`rotation/infrastructure/db/maintenance.py:48-53`
**Preuve** :

```python
def backup(conn, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    conn.execute("VACUUM INTO ?", (str(target),))   # copie complète
```

`maintenance.run_once` est appelé au boot (`scripts/fresh_chain_supervisor.py:3247`)
**et** toutes les `max(3600, maintenance_interval_s)` = 6 h (`:2245`, avec
`maintenance_interval_s: 21600`). Chaque passage écrit
`logs/backups/registry-{stamp}.db` — **~140 Mo**. Aucun code ne supprime jamais
une sauvegarde.

**Mesure** :

```
logs/backups/ : 186 fichiers registry-*.db, 11,79 Go, fenêtre 14,9 jours
6 derniers : 138,5 / 142,4 / 146,3 / 134,6 / 138,6 / 142,6 Mo
+ 1 fichier requests-*.db de 1 046 884 352 octets (1 Go)
```

Aggravant : `integrity_ok` (`retention.py:46-48`) est appelé sur la **source**
avant le backup ; la **copie** n'est jamais vérifiée. Le principe XIII demande un
« backup `VACUUM INTO` **vérifié** ». Et deux passages dans la même seconde
écrasent le même fichier (le stamp est à la seconde).

**Impact** : +~280 Mo/jour de sauvegardes, non bornés ; le disque se remplit sans
alerte. Une sauvegarde non vérifiée n'est pas une sauvegarde.

**Recommandation** : garder N=3 sauvegardes (rotation par nom), vérifier la copie
(`PRAGMA integrity_check` sur le fichier produit, puis suppression si KO), et
passer le stamp à la microseconde.

**Effort** : S.

---

#### P0-08 — `egress_ip_in_use` compare un entier à une colonne TEXT : le garde d'unicité d'IP ne fonctionne pas

**Fichier** : `free/qualification/store.py:1122-1144`
**Preuve** (code exact) :

```sql
SELECT 1 FROM egress_options
WHERE egress_ip=? AND status='usable' AND station<>?
LIMIT 1
```

avec `(ip, str(int(station)))`. Or la colonne est TEXT et contient **`'station-1'`**,
**`'warp-3'`**, **`'public'`** — jamais `'1'` :

```
$ SELECT typeof(station), COUNT(*) FROM egress_options GROUP BY 1
('text', 31900)
$ SELECT DISTINCT station FROM egress_options LIMIT 12
'warp-1' … 'warp-12'
$ SELECT COUNT(*) FROM egress_options WHERE status='usable' AND station<>'1'
1627      <-- toutes les options usable
$ SELECT COUNT(*) FROM egress_options WHERE status='usable'
1627
```

En SQLite `'station-1' <> 1` est **vrai** : la requête retourne donc toujours
`True` dès qu'une option usable existe pour cette IP. Appelants :
`free/qualification/sweep.py:256-260` (promotion → `DUPLICATE`) et
`free/qualification/rotation.py:128` (exclusion du candidat).

**Impact** : un hostname dont l'IP est déjà enregistrée `usable` par **une autre
station** est marqué `DUPLICATE` et parqué en infra au lieu d'être promu — la
réserve de candidats se vide alors que des IP fonctionnent. Principe V
(unicité d'IP) : le garde n'est pas faux par excès de prudence, il est **non
déterministe** (il dépend de la présence d'une ligne `usable` sans rapport).

**Recommandation** : normaliser la comparaison (`station <> ?` avec le label réel,
ou stocker un `station_id INTEGER` séparé), et ajouter un index
`egress_options(egress_ip, status)`.

**Effort** : S.

### 4.2 P1 — bugs fonctionnels, concurrence, performance, dette structurelle

#### P1-01 — Le bouton « qualifiers » du GUI rapporte `ok: true` mais n'écrit jamais `config.yaml`

**Fichier** : `free/qualification/qualifiers.py:314` (appel) vs `:221` (signature)
**Preuve** (les deux lignes exactes) :

```python
# qualifiers.py:314
await asyncio.to_thread(self._persist, config.as_mapping())
# qualifiers.py:221
def persist_qualifier_request(*, enabled: bool, count: int) -> dict[str, Any]:
```

`as_mapping()` renvoie un `dict` **positionnel** ; la fonction cible n'accepte que
des arguments **par mot-clé**. Vérifié par exécution :

```
$ python -I -c "…persist_qualifier_request(cfg.as_mapping())"
TypeError: persist_qualifier_request() takes 0 positional arguments but 1 was given
```

L'`except Exception` de `qualifiers.py:315-319` n'émet qu'un log
`"qualifiers: persistance impossible"` — l'appelant (`dashboard/routers/
qualification_routes.py:61`, `POST /api/egress/qualification/qualifiers`) renvoie
`{"ok": true}`. Le repli correct de la route (`qualification_routes.py:96`, qui
appelle la fonction avec les bons kwargs) est **inatteignable** parce que
`opencode.py:4238` construit toujours le manager (donc `_qualifier_manager` n'est
jamais `None`).

**Impact** : principe XII (« toute option GUI a un effet réel ») violé. L'opérateur
active des slots 7-10, le GUI confirme, `config.yaml` reste
`enabled: false, count: 0` (`config.yaml:1197-1204`) et tout est perdu au
redémarrage.

**Recommandation** : `await asyncio.to_thread(self._persist, enabled=…, count=…)`
et faire remonter l'échec dans la réponse HTTP.

**Effort** : S.

---

#### P1-02 — `measured_score` est alimenté par des constantes : le classement des candidats est aveugle

**Fichier** : `free/qualification/scoring.py:150,153`
**Preuve** (code exact) :

```python
"last_latency_ms": candidate.meta.get("last_latency_ms", 0.0),
"throughput_score": candidate.meta.get("throughput_score", 0.5),
```

Les seuls écrivains de ces clés sont `free/egress_catalog.py:329` et
`free/quality.py:310-313`, pour le meta du **catalogue d'egress**. Le
`meta_json` de `qualification_candidates` n'est écrit que par
`store.set_preflight` (`store.py:569`) avec `dns_ms/connect_ms/handshake_ms/
endpoint_ip`.

**Mesure** :

```
SELECT COUNT(*) FROM qualification_candidates WHERE meta_json NOT IN ('{}','')
0
SELECT COUNT(*) FROM qualification_candidates WHERE meta_json LIKE '%last_latency_ms%'
0
SELECT COUNT(*) FROM qualification_candidates WHERE meta_json LIKE '%throughput_score%'
0
```

Donc `latency` vaut toujours `1.0` et `throughput` toujours `0.5` dans
`measured_score`. Or `catalog_claims.py:247` trie par `q.measured_score DESC` :
le classement qui décide quels hostnames la chaîne répare est en pratique
`reliability + stability + freshness` seuls.

**Impact** : principe IX (le score ordonne) partiellement inopérant ; les 5
candidats `usable` en base ont `measured_score` 90,0 et 0,0 sur deux valeurs
extrêmes non comparables.

**Recommandation** : soit écrire `last_latency_ms`/`throughput_score` dans
`meta_json` au moment de la promotion (`store.promote`), soit retirer les deux
termes du calcul.

**Effort** : S.

---

#### P1-03 — `transport_health` recalcule 2 agrégats O(n) à **chaque** tentative : ~61 ms par tentative, croissance O(n²)

**Fichier** : `free/qualification/transport_health.py:35-43` et `:94-95`, appelé
depuis `store.py:1265` et `store.py:1332`
**Preuve** (code exact, `transport_health.py:36-43`) :

```python
rows = conn.execute(
    f"""
    SELECT status, rtt_ms, latency_ms, error, finished_at
    FROM qualification_attempts
    WHERE transport = ?{where}
    ORDER BY finished_at
    """, params).fetchall()
```

Sans `LIMIT`, sans borne temporelle, **et deux fois** par appel
(`for scope in sorted({0, station})`, `:94`). `EXPLAIN QUERY PLAN` :

```
(3, 0, 149, 'SCAN qualification_attempts')
(18, 0, 0, 'USE TEMP B-TREE FOR ORDER BY')
```

**Mesure** : 60,9 ms par paire d'agrégats sur 34 800 lignes `wireguard`
(× 2 = 2 requêtes + 2 tris Python). Avec 11 232 tentatives/jour, c'est
**~11 minutes de CPU/jour** qui croissent linéairement avec l'historique.

**Impact** : principe XVII (performance : « aucune requête ne charge 20 000
lignes ») violé sur le chemin d'écriture. La table `qualification_attempts` n'a
pas d'index sur `(transport, finished_at)` — mesuré :
`SEARCH qualification_attempts USING INDEX idx_attempt_station (station=?)`
seulement pour la variante avec station.

**Recommandation** : agréger de façon incrémentale (`UPDATE … SET attempts =
attempts+1, successes = successes+?)` au lieu du recompute, ou borner par
`finished_at > now - 30 j` avec l'index manquant.

**Effort** : M.

---

#### P1-04 — `record_attempt` n'est pas atomique avec `refresh_transport_health` : deux transactions distinctes

**Fichier** : `free/qualification/store.py:1261-1266`
**Preuve** (code exact) :

```python
def record_chain_attempt(self, attempt: Attempt) -> str:
    """Tentative réelle de la chaîne → transport_health."""
    attempt_id = self.record_attempt(attempt)      # commit sa propre txn
    with self._lock, self._conn:                   # 2ᵉ txn
        refresh_transport_health(self._conn, attempt, min_attempts=100, now=…)
```

`record_attempt` (`store.py:1206`) ouvre `with self._lock, self._conn:` et
commite à la sortie. L'agrégat `transport_health` est écrit dans une seconde
transaction. Un crash entre les deux laisse une tentative sans agrégat — et comme
l'agrégat est un **recompute complet**, la divergence est silencieuse jusqu'au
prochain appel.

**Impact** : principe XVII (« toute écriture critique est transactionnelle ») :
`transport_health` peut désigner un état antérieur à la tentative enregistrée.
Un test de crash-resume existe (`sweep.py:351-353` gère le doublon
`(hostname, transport, round, epoch)`), mais il ne couvre pas cette fenêtre.

**Recommandation** : fusionner les deux écritures dans une seule transaction
(passer le `refresh` en paramètre de `record_attempt`).

**Effort** : S.

---

#### P1-05 — `rebind_allowed` est un compteur à jeton en read-then-write **sans** `BEGIN IMMEDIATE`

**Fichier** : `free/catalog_claims.py:504-523`
**Preuve** (code exact) :

```python
with self._conn:                                  # BEGIN DEFERRED implicite
    row = self._conn.execute("SELECT window_start, count FROM rebind_budget WHERE id=1")…
    …
    self._conn.execute("UPDATE rebind_budget SET count=count+1 WHERE id=1")
```

En mode `isolation_level=''`, la première écriture ouvre un `BEGIN DEFERRED` : la
transaction prend SHARED sur le SELECT et doit **monter** en RESERVED sur
l'UPDATE. Sous un écrivain concurrent, la montée échoue immédiatement en
`SQLITE_BUSY` — et `busy_timeout` ne rejoue **pas** une montée de verrou. Or ce
compteur est le **limiteur de débit global de rebind** de la flotte
(`rebind_limit_per_min: 8`, `config.yaml:1320`), utilisé par
`scripts/fresh_chain.py:2368`.

Le commentaire de `docs/debt.md` (D-014-11) documente que ce limiteur a déjà été
la cause d'un épisode `AUTH_FAILED` en cascade.

**Impact** : deux chaînes peuvent lire `count = limit-1` et incrémenter toutes les
deux → dépassement du budget ; ou bien une des deux reçoit `SQLITE_BUSY` et le
rebind est refusé à tort. Principe XVII (fiabilité).

**Recommandation** : `BEGIN IMMEDIATE` explicite (comme `store.claim_next`,
`store.py:1066`, qui est le seul endroit du store à le faire correctement).

**Effort** : S.

---

#### P1-06 — Le god-init de `free.ip_registry` charge 60 modules pour un `reconcile()`

**Fichier** : `free/ip_registry/__init__.py:6-33` + `opencode.py:3589`
**Preuve** :

```python
# opencode.py:3589 — la SEULE utilisation de free.ip_registry dans le proxy
from free.ip_registry.registry_db import reconcile as _reconcile_registry
```

L'exécution de `free/ip_registry/__init__.py` importe **eager** les 6 adapters
(`compose_binder`, `egress`, `gluetun_control`, `gluetun_control_v2`,
`hybrid_binder`, `probe`), `allocator`, `catalog`, `sources`, `sqlite_registry`,
`status` — donc `subprocess`, `urllib`, `free.control_plane` (8 modules) et
`curl_cffi`.

**Mesure** :

```
$ python -I -c "import free.ip_registry.registry_db"
1,144 s ; 60 modules free.* chargés
control_plane loaded: True
probe loaded: True
compose_binder loaded: True
```

La docstring du paquet (`__init__.py:1-4`) est devenue fausse :
*« Pure domain, scoring and eligibility rules. No I/O, no provider import. »*

**Impact** : le boot du proxy paie ~1,1 s et ~60 imports pour un `reconcile()`
qui n'en utilise aucun ; `scripts/fresh_chain_supervisor.py:361-366` paie la même
chose à chaque appel. Principe XIV (domaine pur sans I/O ; pas d'import d'adapter
par le domaine) et XVII (démarrage à froid).

**Recommandation** : vider `__init__.py` (ou le rendre paresseux via
`__getattr__`), et importer les adapters directement là où ils servent.

**Effort** : S (le `__init__`) / M (vérifier les 4 scripts qui utilisent le
re-export).

---

#### P1-07 — `free/qualification/store.py` à 1 475 lignes : 25 lignes sous la limite constitutionnelle

**Fichier** : `free/qualification/store.py` (1 475 l.)
**Preuve** : `python -I scripts/module_size_audit.py` → `violations=9` (aucune dans
la zone) ; `tests/test_qualification_architecture.py:56-59` :
`assert lines <= 1500, f"{path.name} has {lines} lines"`.

Le mixin `epochs.py` a été extrait précisément pour rester sous 1 500
(« Kept as a mixin so ``store.py`` stays under the 1 500-line architecture
gate », `epochs.py:1-5`). Il reste 25 lignes de marge : toute évolution
fonctionnelle du store casse la gate.

Suivants dans la zone : `readmodel.py` (971), `sqlite_registry.py` (1 039),
`profile_registry.py` (604), `qualifiers.py` (598), `client.py` (538),
`catalog_claims.py` (534).

**Impact** : principe XIV (« aucun nouveau module ne dépasse 1 500 lignes ») —
la marge est nulle, la prochaine fonctionnalité force une extraction non planifiée.

**Recommandation** : plan de découpage par lots, dans l'ordre :
(a) `attempts` (record/apply/promote/purge) → `attempt_store.py` (~400 l.) ;
(b) `incidents` + `repair_jobs` + `station_state` → `station_store.py` (~300 l.) ;
(c) `server_transports` + `transport_health` reste dans `transport_health.py`.
Cible : ~700 l. pour `store.py`.

**Effort** : L.

---

#### P1-08 — 8 tables de la zone sont mortes ou jamais alimentées, dont 5 sans aucun `INSERT` repo-wide

**Preuve** (`grep -c "INSERT INTO <table>"` sur tout le repo hors tests) :

| Table | Lignes | `INSERT` repo-wide | Statut |
|---|---|---|---|
| `sweep_cursor` | 0 | 1 (`repositories.py:331`, jamais appelé) | **morte** |
| `score_snapshots` | 0 | 1 (`repositories.py`, jamais appelé) | **morte** |
| `fallback_events` | 0 | **0** | **morte** |
| `config_snapshots` | 0 | **0** | **morte** |
| `slo_samples` | 48 | **0** | figée depuis le 2026-11-14 |
| `endpoints` | 0 | 3 (`repositories.py`, jamais appelé) | **morte** |
| `station_runtime` | 0 | 1 (`rotation_executor.py`, jamais atteint) | **morte** |
| `station_cutovers` | 0 | 1 (idem) | **morte** |

Ces 8 tables occupent des pages, des index (`idx_sweep_next`, `idx_warm_score`
pour `warm_pool`, `idx_cutover_station`, `idx_config_time`, `idx_fallback_dep`,
`idx_fallback_station`, `idx_endpoints_host`, `idx_endpoints_ip`) et de
l'attention de maintenance, pour zéro donnée.

`free/qualification/rotation*.py` + `cutover*.py` + `apply.py` (637 lignes au
total) forment une chaîne complète de rotation make-before-break dont la sortie
finale (`station_runtime`, `station_cutovers`) est **toujours vide** : le
dashboard `POST /api/egress/qualification/rotate?apply=true` est le seul chemin,
et il n'a jamais été exercé en production.

**Recommandation** : décider explicitement — soit brancher (`apply_rotation` sur
le chemin superviseur), soit supprimer les tables et les 637 lignes qui les
alimentent. Le principe XIV interdit de laisser les deux.

**Effort** : M.

---

#### P1-09 — N+1 dans `list_station_states` / `list_station_runtimes`, composé en O(N²) sur le chemin de rotation

**Fichier** : `free/qualification/store.py:759-764` et `:1046-1051`
**Preuve** (code exact, `store.py:759-764`) :

```python
rows = self._conn.execute("SELECT station FROM station_state ORDER BY station").fetchall()
return [state for row in rows if (state := self.station_state(int(row["station"]))) is not None]
```

`station_state()` (`store.py:736-741`) exécute `SELECT * FROM station_state WHERE
station=?` — **un SELECT par station**. Même schéma pour `list_station_runtimes`
(`:1046-1051` → `station_runtime()` `:1023`).

Composition mesurée : `free/qualification/apply.py:91-95`
`sum(1 for row in store.list_station_states() if row.get("client_usable"))`
appelle la version N+1, puis `apply.py:93` recompose. Sur 6 stations le coût est
négligeable (0,2 ms mesuré), mais `watchdog.py:153-155` utilise aussi
`list_station_states()` comme source des stations à sonder, et `readmodel.stations`
(`readmodel.py:497-538`) fait déjà la version correcte (un `SELECT *` puis un
dict) — **deux implémentations de la même lecture, dont une seule est efficace**.

**Impact** : la lecture correcte existe (`readmodel.py:507-516`) et n'est pas
réutilisée. Coût réel aujourd'hui : faible ; coût structurel : deux vérités.

**Recommandation** : `list_station_states` → un seul `SELECT * FROM station_state
ORDER BY station` + construction en mémoire (copier `readmodel.stations`).

**Effort** : S.

---

#### P1-10 — `chain_bridge` garde un cache global de `QualificationStore` jamais fermé

**Fichier** : `free/qualification/chain_bridge.py:22,41-50`
**Preuve** (code exact) :

```python
_STORES: dict[str, Any] = {}
def _store(db_path):
    path = str(Path(db_path or DEFAULT_DB).resolve())
    with _LOCK:
        store = _STORES.get(path)
        if store is None:
            from .store import QualificationStore
            store = QualificationStore(path)
            _STORES[path] = store
        return store
```

`QualificationStore` (`store.py:316`) ouvre `sqlite3.connect(…,
check_same_thread=False)` + WAL + `busy_timeout`. `close()` existe (`:346`) et
**tous** les autres appelants l'appellent dans un `finally`
(`apply.py:134`, `qualification_routes.py:392,500`, `runtime.py:93`,
`scripts/qualification_daemon.py:106`). Le cache de `chain_bridge` — utilisé par
`scripts/fresh_chain.py:45,2394-2534`, un processus court — ne le ferme jamais.

**Impact** : fuite de descripteur par processus de chaîne ; en WAL, une connexion
non fermée peut retenir le verrou de lecture au moment où le processus sort,
retardant le checkpoint du superviseur. Aggravant : `QualificationStore.__init__`
n'a **pas** de `try/finally` autour de `executescript(SCHEMA_SQL)` (`:320`) → si
le schéma échoue, la connexion fuit aussi.

**Recommandation** : `atexit.register` ou `contextlib` sur le cache ;
`try/finally` dans `__init__`.

**Effort** : S.

---

#### P1-11 — Connexions SQLite non fermées et DDL sur le chemin de lecture dans `control_plane.telemetry`

**Fichier** : `free/control_plane/telemetry.py:62-63, 90-91, 121-122, 145-146`
**Preuve** (code exact) :

```python
with sqlite3.connect(self._path) as connection:   # NE FERME PAS la connexion
    connection.execute(_SCHEMA)                   # CREATE TABLE à CHAQUE appel
```

Vérifié expérimentalement : après sortie du bloc `with`, la connexion est
**toujours utilisable** (`with sqlite3.connect(...)` gère la transaction, pas la
ressource). Donc chaque `record()` / `recent()` laisse une connexion ouverte
jusqu'au GC par comptage de références CPython.

Aggravant : `connection.execute(_SCHEMA)` (un `CREATE TABLE IF NOT EXISTS`) est
exécuté **à chaque insertion et à chaque lecture** — donc sur le chemin de
lecture du dashboard (si branché). Aucun `PRAGMA journal_mode`, aucun
`busy_timeout`.

**État actuel** : module **mort** (aucun appelant hors tests,
`logs/telemetry.db` inexistant). Le défaut est latent.

**Recommandation** : `sqlite3.connect` + `try/finally: close()`, `_SCHEMA` une
seule fois, PRAGMAs WAL/busy_timeout.

**Effort** : S.

---

#### P1-12 — Le préflight rapporte un endpoint UDP mort comme joignable et alimente le score de qualification

**Fichier** : `free/ip_registry/preflight.py:255-278` et `:150-186`
**Preuve** (code exact, `:269-278`) :

```python
sock.sendto(b"\x00", (ip, int(port)))
try:
    sock.recvfrom(2048)
except (TimeoutError, OSError):
    pass
```

et `handshake_ok=True` est posé **inconditionnellement** quand
`self._handshake is None` (`:150-186`). La docstring assume le choix
(« a silent protocol can legitimately not answer »), mais le résultat `ok=True`
est ensuite écrit dans le score :

```python
# free/qualification/preflight.py:149
score = potential_score(updated, now=ts, weights=self.weights)
self.store.set_preflight(candidate.hostname, rtt_ms=rtt, … potential_score=score, …)
```

**Impact** : un endpoint WireGuard injoignable obtient `ok=True` et un
`potential_score` alimenté par un `rtt` de send-only. Principe VII (« un `200` ne
compte que si le flux a produit du contenu ») : ici c'est un `sendto` qui compte
comme une preuve. Atténuation : le préflight n'est **pas** atteignable en
production (`--preflight` jamais passé, voir P1-14).

**Recommandation** : distinguer `endpoint_ok` (send-only) de `handshake_ok` dans
le score, ou exiger un `handshake` injecté pour les transports UDP.

**Effort** : S.

---

#### P1-13 — `_version_for(egress_ip)` ignore son argument : la branche v1 de `_wire_bytes` est morte

**Fichier** : `free/ip_registry/adapters/probe.py:438-453` et `:405-435`
**Preuve** (code exact, `:445-453`) :

```python
try:
    from free.impersonation import load_spec
    version = str(load_spec().get("version") or "").strip()
    if version:
        return version
except Exception:
    pass
return "2.3.0"
```

`egress_ip` n'est **jamais** lu. `free/impersonation.json` porte `version:
"2.3.0"` (mesuré) : le retour est donc toujours `"2.3.0"`, jamais `"1.18…"` ni
`"1.19…"`. Conséquence : `v1 = str(version).startswith(("1.18","1.19"))` dans
`_wire_bytes` (`:420`) est **toujours faux**, donc `self._body_v1`
(`:88`, chargé depuis le `.bak`) n'est **jamais** utilisé — et le paramètre par
défaut `version: str = "2.2.5"` (`:413`) est une valeur qui ne peut plus
apparaître.

**Impact** : code mort + un chargement de fichier inutile au boot de chaque
probe. La docstring de `_wire_bytes` (« The 1.18.x/1.19.x identities use the
matching 1.19.5 capture ») décrit un comportement qui n'existe plus.

**Recommandation** : soit supprimer `_body_v1` et la branche, soit rétablir la
sélection par version depuis le profil utilisé.

**Effort** : S.

---

#### P1-14 — `PYTEST_CURRENT_TEST` conditionne trois chemins de production dans `probe.py`

**Fichier** : `free/ip_registry/adapters/probe.py:109,179,252`
**Preuve** :

```
109:            if egress_ip and not os.getenv("PYTEST_CURRENT_TEST"):
179:                        if egress_ip and not os.getenv("PYTEST_CURRENT_TEST"):
252:                        and not os.getenv("PYTEST_CURRENT_TEST")
```

Ces trois gardes désactivent respectivement : la réutilisation de la preuve, la
persistance du 429, et la transition `proven` vers `free.ip_state`.

**Impact** : le chemin **testé** n'est pas le chemin **livré**. Un test qui passe
sur la persistance du 429 ne prouve rien du comportement en production. Principe
XVII (qualité : « chaque exigence fonctionnelle a un test automatisé »).

**Recommandation** : injecter un flag de configuration explicite
(`probe.persist_evidence: true`) au lieu de lire une variable d'environnement de
pytest.

**Effort** : S.

---

#### P1-15 — Le budget de probe est débité **avant** la requête, et trois `except Exception: pass` masquent l'état de preuve

**Fichier** : `free/ip_registry/adapters/probe.py:141-143`, `:229-230`, `:271-272`
**Preuve** (code exact, `:141-145`) :

```python
_budget.record_probe("nordvpn", _bucket)     # débité ici
except Exception:
    pass
last_error = "no-attempt"
for scheme in ("socks5h", "socks5"):          # …puis les POST
```

**Impact** : une sonde dont les deux POST échouent consomme quand même un jeton du
budget par IP — le budget protège l'IP d'un abus qui n'a pas eu lieu, au prix
d'une réduction de la capacité réelle de sondage. Et les trois
`except Exception: pass` (142, 229, 271) suppriment toute trace d'un schéma
`free.ip_state` cassé : la réutilisation de preuve, la classification 429 et la
transition `proven` s'éteignent sans un log.

**Recommandation** : débiter après le premier POST, et logguer en `warning` avec
la cause dans les trois `except`.

**Effort** : S.

---

#### P1-16 — `host_public_ip` fait jusqu'à 25 s d'I/O réseau bloquante sur le thread du superviseur

**Fichier** : `free/ip_registry/adapters/egress.py:44-63`
**Preuve** (code exact) :

```python
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for url in urls:                                  # jusqu'à 5 URLs
    with opener.open(url, timeout=max(1.0, float(timeout_s))) as response:   # 5 s chacune
```

Appelé **synchroniquement** depuis `scripts/fresh_chain_supervisor.py:2153`
(importé à `:41`) — sur la boucle du superviseur, pas dans un
`asyncio.to_thread`. Pire cas mesuré par lecture : 5 × 5 s = **25 s**.

**Impact** : principe XVII (performance : « aucune I/O réseau … dans le chemin de
requête ; ces travaux vont en tâche de fond ou en `asyncio.to_thread` »). Un site
d'IP lent gèle le superviseur 25 s, ce qui retarde le tick de qualification et la
détection de panne.

**Recommandation** : envelopper l'appel dans `asyncio.to_thread` côté superviseur
et réduire `IP_URLS` à 2 entrées.

**Effort** : S.

---

#### P1-17 — `dns.py` : l'échelle de repli est inerte avec la configuration livrée

**Fichier** : `free/ip_registry/dns.py:24,39-47` + `config.yaml:1286-1287`
**Preuve** : `config.yaml` porte `fallback_order: ['dot']` → `_order = ('dot',)` ;
`dns.py:45` `min(self._index.get(station,0)+1, len(self._order)-1)` ne peut
produire que `0`. Donc `note_failure()` (`:39-47`) n'avance jamais le mode,
`mode()` retourne toujours `"dot"`, et `scripts/fresh_chain.py:2386` passe
`dns_mode=(dns_mode if dns_mode != "dot" else None)` → toujours `None`. Le mode
`"doh"` accepté en `:24` est **inatteignable**.

**Impact** : principe XI (« DNS (DoH/DoT/plain) » : chaîne de repli) : la chaîne
existe en code mais pas en configuration, et rien ne le signale. Principe XII
(option sans effet).

**Recommandation** : soit livrer `fallback_order: ['doh','dot','plain']`, soit
supprimer la classe et laisser le resolver unique explicite.

**Effort** : S.

---

#### P1-18 — `free/ip_registry/status.py` : version de schéma fausse et chemin de base en dur

**Fichier** : `free/ip_registry/status.py:53` et `:182`
**Preuve** (code exact, `:53`) :

```python
row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
```

Sur `logs/registry.db` cela renvoie **3** — c'est `SCHEMA_VERSION = 3` de
`rotation/infrastructure/db/migrate.py:9`, **pas** le `SCHEMA_VERSION = 2` de
`free/ip_registry/sqlite.py:15`. Vérifié en exécutant `registry_status()` :
`schema_version reported: 3`. Le nom du champ (`db.schema_version`) laisse croire
qu'il décrit le registre d'IP legacy.

Et `:182` : `path = ROOT / "logs" / "registry.db"` en dur dans `_claims_summary`,
alors que `registry_status` (`:316-318`) résout
`cfg["registry_db_path"] or cfg["db_path"]` : un même payload décrit **deux** bases
différentes si la config pointe ailleurs.

Aggravant (mesuré) : `registry_status()` prend **0,087 s** et n'a **aucun cache**,
alors que la route (`dashboard/routers/free_routes.py:50-55`) est appelée par
`/api/free/ip-registry/status` **et** `/api/free/allocator/status` (`:32-37`),
chacune rechargeant le manifeste de 918 lignes et relisant les chain-state.

**Recommandation** : lire la version du bon fichier de schéma (ou renommer le
champ), utiliser `registry_db_path` partout, et mémoïser 5 s.

**Effort** : S.

---

#### P1-19 — `incidents` : 35 384 lignes pour 28 combinaisons (station, cause), 6 805 écritures/24 h, aucun dedup temporel

**Fichier** : `free/qualification/store.py:766-795` (`open_incident`) et
`free/qualification/watchdog.py:190-200`
**Preuve** (code exact, `store.py:778-787`) :

```python
row = self._conn.execute(
    "SELECT id FROM incidents WHERE station=? AND cause=? AND ended_at IS NULL …").fetchone()
if row is not None:
    return int(row["id"])          # réutilise l'incident OUVERT
```

Le dedup ne s'applique qu'aux incidents **ouverts**. Or `watchdog.py:177` ferme
l'incident dès que la station est saine (`close_station_incidents(…,
remediation="auto-cleared")`), et `:183` le ferme aussi en cas de quota. Comme le
watchdog sonde toutes les 30 s (`watchdog_probe_interval_s: 30`) et que les
stations oscillent, chaque oscillation **ouvre puis ferme** un nouvel incident.

**Mesure** :

```
incidents ............................ 35 384  (dont 35 392 fermés, 2 ouverts)
combinaisons (station, cause) ........ 28
cause dominante ...................... tunnel_down 26 257
durée moyenne tunnel_down ............ 5,23 s
écart médian entre 2 tunnel_down (st1) 63,8 s  (min 58,9 / max 87,6)
débit station 1 ....................... 55–56 incidents/heure
24 h ................................. 6 805 lignes
```

**Impact** : 7 Mo de bruit pour 28 états ; toute lecture « quels incidents sont
ouverts / quel est le MTTR » doit filtrer 35 k lignes ; la rétention `incidents`
est de 90 j (`retention.py:13`) donc ~600 k lignes à terme. Principe I
(« la disponibilité se mesure depuis un journal d'incidents persistant ») : le
journal existe mais la granularité (une ligne par oscillation) le rend inutilisable
pour un calcul de SLO.

**Recommandation** : ne pas fermer un incident de moins de N secondes (ou agréger
les fermetures < 60 s dans un compteur `oscillations` sur l'incident existant).

**Effort** : M.

---

#### P1-20 — 4 tables à double DDL divergente : la forme du schéma dépend de l'ordre de démarrage

(voir §3.3 pour la table comparative complète)

**Fichiers** : `rotation/infrastructure/db/schema.sql:55,169,219,227` vs
`free/qualification/store.py:123,170,185,198`
**Preuve** : `CREATE TABLE IF NOT EXISTS` des deux côtés, colonnes et contraintes
différentes pour `server_transports` (FK cascade + CHECK perdus),
`station_state` (CHECK 8 états perdu), `incidents`, `repair_jobs`
(`transports_tried` TEXT vs INTEGER).

**Impact** : principe III (une seule source de vérité) et XIII. Une base créée par
le dashboard avant le premier `apply_schema` garde définitivement les tables
faibles ; les lectures (`readmodel.transports`, `repair_attempts`) changent de
sémantique sans erreur.

**Recommandation** : retirer les 12 `CREATE TABLE` de `store.py` et n'appeler que
`apply_schema` (déjà fait par `free/quota_ledger.py:617-623` et
`scripts/fresh_chain.py:434-438` — le motif existe) ; ajouter un test qui échoue si
un module hors `rotation/infrastructure/db` déclare une table du schéma canonique.

**Effort** : M.

### 4.3 P2 — qualité, cohérence, options mortes

#### P2-01 — `qualification_epochs` : 99,8 % d'epochs vides et un compteur fleet-wide dans un journal par-run

**Fichier** : `free/qualification/sweep.py:110` et `free/qualification/epochs.py:53-70`
**Preuve** : `create_epoch(eid, total=self.store.summary()["total"], …)` — le
`total` est le total **de la flotte**, pas de l'epoch. Et `close_epoch` a un
repli documenté (`epochs.py:57-66`) : « Historical callers omit the counters: the
global summary is then written, which is only a fallback ».

**Mesure** : `SUM(tested)` = 846 983 pour 41 118 tentatives réelles ; 21 182 epochs
sur 21 220 ont 0 tentative ; 2 epochs `status='running'` depuis > 1 h (jamais
fermés — `close_running_epochs` n'est appelé que par `qualifiers.py:443`, sur le
préfixe `qualifier-`, donc jamais pour `epoch-*`).

**Impact** : le journal d'epochs est un compteur de flotte déguisé ; la gate de
canary (`free/qualification/canary.py:107,175`) et le readmodel `epochs`
consomment ces chiffres.

**Recommandation** : compteurs par-run uniquement ; appeler
`close_running_epochs(prefix="epoch-")` au démarrage du daemon.

**Effort** : S.

---

#### P2-02 — 201 runs de scan restent ouverts alors que `reconcile_stale` existe

**Fichier** : `free/scan_audit.py:207-230` (`reconcile_stale`) vs
`scripts/fresh_chain_supervisor.py:3266`
**Preuve** : `reconcile_stale(max_age_s=7200.0)` ferme les runs dont
`started_at < now - 7200`. Mesure :

```
scan_runs ouverts .................. 211 (dont 201 plus vieux que le cutoff)
scan_attempts ouverts .............. 191 (status='intent')
plus ancien run ouvert ............. 2026-11-18 10:58
répartition par station ............ st4: 104, st2: 104, st5: 3, st3: 1
```

`reconcile_stale` **est** appelé au boot du superviseur (`:3266`), mais le
superviseur a tourné (maintenance au 2026-11-20 10:30 confirmée par le backup) :
donc soit `reconcile_stale` échoue silencieusement (`except Exception: return 0`,
`:228-230`), soit les runs sont recréés plus vite qu'ils ne sont fermés. 201 runs
plus vieux que 7 200 s contredisent le cutoff.

**Impact** : `scan_runs` est le journal d'audit du principe VII ; 211 runs ouverts
signifient que la réconciliation au boot ne fonctionne pas comme annoncé. Les
`scan_attempts` correspondants restent `status='intent'` (191 lignes).

**Recommandation** : logguer le résultat de `reconcile_stale` (aujourd'hui
`stale = audit.reconcile_stale()` sans usage à `:3266`), et vérifier que le
`_close_run` de `scripts/fresh_chain.py:2013-2020` s'exécute bien sur tous les
chemins de sortie (`return 2` de `:2145` par exemple).

**Effort** : S.

---

#### P2-03 — `transport_health.degraded` est binaire et reste à 0 malgré 33 682 `bind_error`

**Fichier** : `free/qualification/transport_health.py:96`
**Preuve** (code exact) :

```python
degraded = int(attempts_count >= threshold and successes == 0)
```

La condition exige **zéro** succès. Mesure :

```
wireguard st0 : 34 800 tentatives, 2 775 succès → degraded=0 (taux 8 %)
openvpn-tcp st0: 1 962 tentatives,    12 succès → degraded=0
wireguard st7 :    739 tentatives,     2 succès → degraded=0
3 lignes sur 30 ont degraded=1
```

**Impact** : un transport à 0,06 % de succès (`openvpn-tcp`) n'est jamais signalé
dégradé ; `readmodel.transports` et le badge dashboard montrent « sain ». Le
`success_rate` est bien stocké (0,0797 pour wireguard) mais aucune porte ne
l'utilise.

**Recommandation** : `degraded = attempts >= threshold and success_rate <
min_success_rate` (seuil configurable), au lieu de `successes == 0`.

**Effort** : S.

---

#### P2-04 — `os.getenv`/`yaml_get` relus dans des boucles chaudes

**Fichiers** : `free/ip_registry/adapters/probe.py:109,179,252` (3 `getenv` par
sonde) ; `free/qualification/store.py:1373-1381` (3 scans par `summary()`) ;
`free/qualification/watchdog.py:153-155` (`list_station_states()` par tick).
**Preuve** : `readmodel` a un cache de 5 s (`readmodel.py:41-68`) mais le store,
lui, n'en a aucun : `readmodel.summary` appelle `store.summary` via
`readmodel._cached` (donc 5 s), tandis que `sweep.run_epoch` appelle
`self.store.summary()` **directement** (`sweep.py:110`) à chaque epoch.

**Mesure** : `store.summary()` = 15,6 ms (3 scans de 41 k lignes) × ~100 epochs/h.

**Recommandation** : mettre `total` en cache 60 s dans le store, ou le lire par
`COUNT(*)` indexé.

**Effort** : S.

---

#### P2-05 — `preflight` : `PreflightSummary` et `from_results()` sont test-only, la vraie agrégation est réimplémentée ailleurs

**Fichier** : `free/ip_registry/preflight.py:47-70`
**Preuve** : `grep -rn "PreflightSummary"` → seulement
`tests/test_ip_registry_preflight.py:114,140` ;
`free/qualification/preflight.py:126-171` réimplémente sa propre agrégation
(`_MergedResult`, `ok/failed/errors`).

**Recommandation** : supprimer `PreflightSummary`/`from_results` ou les réutiliser.

**Effort** : S.

---

#### P2-06 — Trois implémentations du même `fetch_text`, dont deux sans garde SSRF

**Fichiers** : `free/egress_sources.py:370-387` (gardé : `validate_source_url` +
`_SafeRedirectHandler`), `scripts/fetch_proxy_lists.py:285` (`urlopen` nu),
`free/ip_registry/sources/adapters.py:64-70` (`requests.get` sans vérification de
schéma ni d'hôte).

**Impact** : le durcissement existe mais n'est pas la seule voie ; principe XI
(chaîne de repli) et XVII (qualité).

**Effort** : S.

---

#### P2-07 — `verified_profiles.json` : source de vérité fichier sans table, écrite à la main

**Fichier** : `free/profile_registry.py:562-571` (`save`) et
`free/profiles/verified_profiles.json`
**Preuve** : `grep -rn "\.save()" free/ scripts/ dashboard/ opencode.py` → **aucun
appelant**. Aucune table `profiles` n'existe (`grep "profiles" *.sql` → rien).
Le fichier est lu à chaud (`opencode.py:10825-10832`, cache par mtime) mais jamais
écrit par le runtime.

**Impact** : la « preuve » d'un profil (liste d'évidences avec verdicts) est figée
dans un JSON versionné par git ; le TTL n'a donc aucune source de renouvellement
(P0-03), et le principe III (« les JSON sont des exports en lecture seule ») est
ici inversé : le JSON est la seule source.

**Recommandation** : soit une table `verified_profiles` + `profile_evidence`
(append-only), soit assumer explicitement le JSON comme source et retirer la
machinerie TTL/audit qui suggère l'inverse.

**Effort** : M.

---

#### P2-08 — `audit_profile` / `ProfileRegistry.assign` / `quota_blocks` / `transient_blocks` : surface publique sans appelant

**Fichier** : `free/profile_registry.py:314` (`audit_profile`), `:447`
(`assign`), `:236,239` (`quota_blocks`/`transient_blocks`)
**Preuve** : `grep -rn "\.assign(\|\.audit(\|quota_blocks\|transient_blocks"` →
définitions + `free/profile_verifier.py:118-119` seulement.

**Effort** : S.

---

#### P2-09 — `catalog.py` : `_last_error` est non vide sur un refresh **réussi**

**Fichier** : `free/ip_registry/catalog.py:148`
**Preuve** (code exact) :

```python
self._last_error = "; ".join(resolution.errors) or None
```

Or `sources/registry.py:84` enregistre `no-adapter:<id>` comme **erreur** pour
chaque source sans adapter (comportement voulu par la docstring `:170-173`). Donc
`last_error` est toujours non vide après un refresh normal, et
`scripts/ip_registry_observe.py:36` affiche `catalog error:` sur un run sain.
Contradiction : `is_stale()` (`:134-139`) renvoie `False` au même moment.

**Effort** : S.

---

#### P2-10 — `qualifiers.py` : station 1 ne peut jamais être arrêtée

**Fichier** : `free/qualification/qualifiers.py:560`
**Preuve** (code exact) : `command = ["docker", "stop", f"opencode-vpn-{int(station)}"]`
produit `opencode-vpn-1` alors que le conteneur de la station 1 est
`opencode-vpn` (cas spécial présent dans `station_adapter.py:132` et
`qualifiers.py:497`). L'échec est avalé (`:572`, `"no such container" in detail`
→ `return True`).

**Impact** : inatteignable avec `station_base: 7` (défaut), mais faux dès qu'un
opérateur descend `station_base` à 1 — et le manager croit avoir arrêté le
conteneur.

**Effort** : S.

---

#### P2-11 — `station_probe.py` lit des clés absentes du payload `/api/vpn-status`

**Fichier** : `free/qualification/station_probe.py:92-98` et `:104-105`
**Preuve** (code exact) :

```python
vpn.get("type") or vpn.get("protocol") or vpn.get("ovpn_protocol_effective") or row.get("transport")
```

Le payload réel expose `transport_effective` au niveau ligne
(`free/pool/__init__.py:7854`) et `vpn.ovpn_protocol_effective` seulement pour
OpenVPN. Mesure sur `station_state` : `transport_effective` = **`'tcp'`** pour les
6 stations (une valeur qui n'appartient à aucun vocabulaire du code :
`wireguard|openvpn-udp|openvpn-tcp`). Idem `vpn.get("model_usable_reason")`
(`:105`) alors que la clé est au niveau ligne (`free/pool/__init__.py:7851`).

**Impact** : le watchdog persiste un transport hors vocabulaire, ce qui alimente
`transport_health` et les vues de couverture avec une donnée fausse.

**Effort** : S.

---

#### P2-12 — `discovery.py` : un run manuel non forcé repousse la prochaine découverte automatique

**Fichier** : `free/qualification/discovery.py:83-91` et `:70-75`
**Preuve** : la branche `skipped` écrit une ligne `discovery_runs` dont
`started_at` devient la plus récente ; `is_due` lit
`store.last_discovery_run()["started_at"]`. Donc
`python scripts/qualification_sweep.py --discovery` (documenté dans
`specs/014-nordvpn-exhaustive-rotation/runbook.md:45`) décale l'intervalle de 6 h.

**Effort** : S.

---

#### P2-13 — `metrics.py` : la jauge `repair_jobs` sature à 200

**Fichier** : `free/qualification/metrics.py:82`
**Preuve** : `"repair_jobs": len(repairs.get("items") or [])` avec
`readmodel.repairs(path, limit=200)` (`metrics.py:26`). Toute valeur ≥ 200 est
publiée comme exactement 200.

**Effort** : S.

---

#### P2-14 — `dashboard/routers/qualification_routes.py:447` : `int()` non gardé → 500

**Fichier** : `dashboard/routers/qualification_routes.py:447`
**Preuve** : `station = int(body.get("station") or 1)` sans `try`, alors que la
route voisine garde explicitement (`:220-228`). `{"station": "abc"}` → 500.

**Effort** : S.

---

#### P2-15 — `maintenance.run_once` : `integrity_ok` vérifie la source, jamais la copie

**Fichier** : `rotation/infrastructure/db/maintenance.py:47-53`
**Preuve** : `ok = integrity_ok(conn)` (source) puis
`target = backup(conn, …)` — la copie n'est ni ouverte ni vérifiée. Le principe
XIII exige un « backup `VACUUM INTO` **vérifié** ».

**Effort** : S.

---

#### P2-16 — Aucun `ANALYZE` / `PRAGMA optimize` dans tout le dépôt

**Preuve** : `grep -rn "ANALYZE\|PRAGMA optimize" --include=*.py` (hors tests) →
**0 résultat**, alors que `sqlite_stat1` existe et que la plus grosse table
(`egress_threat_intel`, 174 163 lignes) est interrogée par `source`.

**Impact** : le planificateur travaille sur des statistiques obsolètes ; sur les
requêtes sans index dédié (P1-03, P0-08) le choix de plan est arbitraire.

**Effort** : S.

---

### 4.4 P3 — cosmétique, cohérence documentaire

| # | Titre | Fichier:ligne | Détail |
|---|---|---|---|
| P3-01 | `MAX_SLOTS` jamais lu | `free/qualification/qualifiers.py:35` | `grep '\bMAX_SLOTS\b'` → 1 seule occurrence (la définition) |
| P3-02 | `TERMINAL_STATES` jamais lu | `free/control_plane/client.py:81` | idem, 1 occurrence |
| P3-03 | `RuntimeQuotaObserver` jamais lu | `free/ip_registry/ports.py:57` | 3 des 6 `Protocol` sans référence |
| P3-04 | `TERMINAL_CANDIDATE_STATUSES` mort | `free/qualification/domain.py:45-52` | pas même re-exporté par `__init__.py` |
| P3-05 | `next_candidate_status` : 4 paramètres ignorés | `free/qualification/domain.py:186-195` | `now`, `infra_backoff_s`, `infra_backoff_max_s`, `skipped_reason` non lus dans le corps |
| P3-06 | `attempts_done`/`total_attempts` sans consommateur | `free/qualification/transport.py:62-67` | `grep` → définitions seules |
| P3-07 | `DIAGNOSTICS` sans consommateur | `free/qualification/diagnostics.py:12-20` | tuple jamais lue |
| P3-08 | `self.sweep` jamais relu | `free/qualification/scheduler.py:53` | attribut assigné, jamais lu |
| P3-09 | `self._clock` jamais relu | `station_probe.py:34`, `station_adapter.py:62` | assigné, jamais lu |
| P3-10 | `probe_timeout_s` jamais relu | `free/qualification/watchdog.py:76` | `WatchdogConfig` posé par `runtime.py:178`, non utilisé |
| P3-11 | `ProbeResult.is_quota_429` / `RuntimeResult.is_quota_429` morts | `free/ip_registry/domain.py:160,179` | définitions + tests |
| P3-12 | `IpRecord.version` / `with_state()` morts | `free/ip_registry/domain.py:98,112` | pas de colonne `version` en base |
| P3-13 | `can_transition()` + `_ALLOWED_TRANSITIONS` morts | `free/ip_registry/domain.py:194-224` | dupliqué par le SQL de `sqlite_registry.py:546-556,920-929` |
| P3-14 | `eligibility.check_eligibility` mort hors tests | `free/ip_registry/eligibility.py` (58 l.) | duplicata exact de `sqlite_registry.py:920-929` |
| P3-15 | `CatalogResolution.error`/`complete` morts | `free/ip_registry/sources/base.py:58-68` | `catalog.py:148` joint `errors` lui-même |
| P3-16 | `SourceManifest` : 6 champs validés mais jamais lus | `free/ip_registry/sources/manifest.py:20,24` | `name/url/auth/refresh/notes/used_by` : `_REQUIRED` les exige, personne ne les lit |
| P3-17 | `OfficialApiSource.complete` jamais mis à False | `free/ip_registry/sources/adapters.py:34` | `build_default_registry:182` garde le défaut |
| P3-18 | `OfficialApiSource._load` : `requests.get` bloquant 120 s | `free/ip_registry/sources/adapters.py:64-71` | via `asyncio.to_thread` : un worker bloqué 2 min |
| P3-19 | `gluetun_control.current_transport()` sans `station` | `free/ip_registry/adapters/gluetun_control.py:104` | v2 (`:294`) et hybrid (`:112`) prennent un station → `hybrid_binder.py:172-174` rattrape par `except TypeError` |
| P3-20 | `compose_binder._desired_env` lit un fichier que personne n'écrit | `compose_binder.py:168-184` | `logs/compose-control-plane-{station}.yml` : absent ⇒ la vérification de fraîcheur renvoie `False` (désactivée) |
| P3-21 | `compose_binder` : `BindResult(True, hostname)` sans preuve d'egress | `compose_binder.py:367-370` | quand `_egress is None` ; les scripts legacy passent `egress_wait_s=0.0` → un seul essai |
| P3-22 | `project_name` de `compose_binder` jamais passé | `compose_binder.py:42,61-68` | `grep "project_name="` → 0 appelant : le correctif « container name already in use » n'est pas activé |
| P3-23 | `hybrid_binder` ne mémorise pas `_credentials_checked` sur succès control | `hybrid_binder.py:70-78` | les 5 sondes de staleness (`docker inspect`) sont rejouées à chaque candidat |
| P3-24 | `ShadowObserver` réutilise une clé d'idempotence par cycle | `free/control_plane/shadow.py:235,307-308` | le bug documenté et corrigé dans `gluetun_control_v2.py:333-335` subsiste dans le shadow |
| P3-25 | `ControlPlaneConfig.api_key()` sans appelant de production | `free/control_plane/config.py:97-99` | la clé est résolue inline (`fresh_chain.py:2913`, `client.py:377`) |
| P3-26 | `RetryConfig.delay()` dupliqué par `client.py:465-468` | `free/control_plane/config.py:36-40` | même backoff réécrit inline |
| P3-27 | `environ` accepté puis jeté | `free/control_plane/config.py:190` | `del environ` |
| P3-28 | `client.dns_state()` / `client.cancel()` sans appelant | `free/control_plane/client.py:218,364` | `grep` → définitions seules |
| P3-29 | `ControlPlaneError.details` jamais lu | `free/control_plane/errors.py:28` | écrit seulement |
| P3-30 | `legacy_fallback()` + `LegacyFallback` sans appelant | `free/control_plane/client.py:72-78` | le binder fait le repli lui-même (`gluetun_control_v2.py:150-169`) |
| P3-31 | `SwitchJob.raise_for_status` sans appelant | `free/control_plane/models.py:213` | le binder mappe `job.state` lui-même |
| P3-32 | `Capabilities.supports` test-only | `free/control_plane/models.py:57` | — |
| P3-33 | `ShadowTelemetry.recent`/`recent_transport` sans appelant | `free/control_plane/telemetry.py:87,142` | rien ne lit ces tables |
| P3-34 | `ScanAudit.snapshot()` sans appelant | `free/scan_audit.py:265` | `grep` → définition seule |
| P3-35 | `CatalogClaims.stats()` sans appelant de production | `free/catalog_claims.py:525` | tests seulement |
| P3-36 | `ttl_for`, `BLOCKED`, `reset_from_retry_after` morts | `free/egress_catalog.py:258,50,416` | `grep` → définition + `__all__` |
| P3-37 | `source_signature` mort | `free/egress_sources.py:403` | idem |
| P3-38 | `validate_source_url` + `local_text` : chemin absolu accepté | `free/egress_sources.py:47-49,395-399` | `read_source_text` lit un chemin absolu sans contrôle de confinement ; la route `POST /api/egress/catalog/sources` (`egress_catalog_mutations.py:101-118`) persiste `{type,url}` verbatim, rejoué par `public.py:285-302` → lecture de fichier arbitraire depuis un POST authentifié |
| P3-39 | `threat_intel.load()` recharge 174 k lignes toutes les 300 s | `free/threat_intel.py:138-157` | TTL codé en dur, incohérent avec `refresh_interval_s: 21600` ; mesuré 63,5 ms × 288 = **18,3 s/jour** |
| P3-40 | `threat_intel.refresh_once` : `feeds == []` supprime tout | `free/threat_intel.py:188` | `prune_threat_feeds(generator vide)` → `keep=set()` → **DELETE** de tous les indicateurs (latent, `egress_runtime.py:367-372` empêche le cas) |
| P3-41 | `epochs.close_epoch` : repli documenté qui écrit le total flotte | `free/qualification/epochs.py:57-66` | voir P2-01 |
| P3-42 | `rotation_executor.py:85` : ternaire constante | `free/qualification/rotation_executor.py:85` | `state="idle" if cutover.status != "committed" else "serving"` dans la branche `else` de `if status == "committed"` |
| P3-43 | `rotation_executor` remet `availability_1h` à 0 sur une rotation réussie | `rotation_executor.py:71-81` | `upsert_station_runtime` sans `availability_1h` → `store.py:996` écrit 0 |
| P3-44 | `rotation.py:98` : `PROMOTE_STANDBY` inatteignable | `free/qualification/rotation.py:98` | aucun écrivain de `standby_option_key`/`standby_egress_ip`/`standby_transport` hors tests |
| P3-45 | `Candidate.quota_remaining` jamais écrit par la qualification | `free/qualification/store.py:408` | `COALESCE` conserve NULL → mesuré : **0 ligne non-nulle** ; le terme quota de `potential_score` (`scoring.py:107-113`) vaut toujours 0,5 et `ORDER BY COALESCE(quota_remaining,0)` (`store.py:505,1087`) trie sur 0 |
| P3-46 | `Candidate.rtt_ms` jamais écrit hors préflight | `store.py` | mesuré : **0 ligne non-nulle** → `ORDER BY COALESCE(rtt_ms,1e9)` trie sur 1e9 |
| P3-47 | `store.purge_attempts` : `DELETE` sans index sur `COALESCE(...)` | `store.py:1458-1461` | `station_cutovers`, `incidents`, `discovery_runs` : pas d'index utilisable pour l'expression |
| P3-48 | `scan_attempts` : `status='intent'` jamais traduit | `free/scan_audit.py:207-230` | 191 lignes orphelines (voir P2-02) |
| P3-49 | `attempts.run_id` vide sur 39 684 lignes | `store.py:1236` | mesuré : `''` = 39 700, `qualifier-7` = 1 367 ; le champ n'est pas exploité par `readmodel.attempts` |
| P3-50 | `qualification_epochs.cursor` jamais écrit | `store.py:114` | lu par `readmodel.py:387` seulement ; mesuré : 21 194 lignes à `''` |

---

## 5. Inventaire du code mort (sourcé)

### 5.1 Modules / paquets

| Cible | LOC | Statut | Preuve |
|---|---|---|---|
| `free/control_plane/events.py` | 429 | **jamais exécuté** | `grep -rn "EventIngestor" opencode.py scripts/ dashboard/` → **0**. Seul appelant : `tests/test_control_plane_events.py`. `control_events`/`control_cursor` **absentes** de la base live |
| `free/control_plane/shadow.py` | 325 | **jamais exécuté** | `grep -rn "ShadowObserver(\|TransportShadowObserver(" free/ dashboard/ scripts/ opencode.py` → 0 hors tests |
| `free/control_plane/telemetry.py` | 171 | **jamais exécuté** | `logs/telemetry.db` **n'existe pas** ; aucun importeur hors `__init__.py` et tests |
| `free/qualification/rotation_batch.py` | 74 | **jamais exécuté** | `grep -rn "build_rotation_batch"` → définition, `__init__.py:106`, tests |
| `free/qualification/station_runtime.py` | 170 | **hors production** | seul appelant `scripts/load_qualification.py:23`, lui-même absent de `scripts/quality_gate.py` (qui lance `bench_` et `soak_` uniquement, `:192,202`) |
| `free/ip_registry/eligibility.py` | 58 | **mort hors tests** | `grep -rn "check_eligibility"` → `eligibility.py`, `__init__.py:29,64`, test. Le prédicat vivant est en SQL (`sqlite_registry.py:920-929`) |
| `free/ip_registry/sqlite.py` + `sqlite_registry.py` | 193 + 1 039 | **chargés, jamais appelés par le runtime** | `ensure_schema` n'est appelé que par `SqliteIpRegistry.__init__` (`sqlite_registry.py:32`), lui-même construit uniquement par 3 scripts manuels. `logs/ip_registry.db` = **0 octet**. Pourtant les deux modules sont importés à chaque `import free.ip_registry.*` |
| `free/ip_registry/ports.py` (3 des 6 Protocols) | ~35 | morts | `ServerCatalog:23`, `RuntimeQuotaObserver:57`, `StationHealth:67` : 0 référence hors du fichier |
| `rotation/application/` + `rotation/domain/` + `rotation/ports/` + `rotation/interfaces/` | (19 fichiers au total dans `rotation/`) | **hors runtime** | `grep -rn "rotation.application"` → `tests/test_rotation_{repair,sweep,warm_pool}.py` seulement. Seuls `rotation/infrastructure/db/{migrate,maintenance,claims,legacy_ip}` sont importés (`opencode.py` non ; `scripts/fresh_chain.py:434`, `scripts/fresh_chain_supervisor.py:2243,3245`, `free/quota_ledger.py:616`, `free/ip_registry/registry_db.py:63`) |
| `rotation/infrastructure/db/repositories.py` (448 l.) | 448 | **partiellement mort** | `UnitOfWork` : 0 appelant hors son propre fichier. `RegistryRepository` : appelé par `rotation/application/*` (lui-même hors runtime). Les 3 `INSERT` (`server_transports:96`, `egress_ips:126`, `sweep_cursor:331`) alimentent les tables vides (`sweep_cursor` = 0, `endpoints` = 0) |

### 5.2 Symboles

| Symbole | Fichier:ligne | Preuve d'absence |
|---|---|---|
| `RuntimeQuotaObserver` | `free/ip_registry/ports.py:57` | 1 occurrence dans tout le repo |
| `TERMINAL_STATES` | `free/control_plane/client.py:81` | 1 occurrence |
| `MAX_SLOTS` | `free/qualification/qualifiers.py:35` | 1 occurrence |
| `TERMINAL_CANDIDATE_STATUSES` | `free/qualification/domain.py:45-52` | définition + `__all__` |
| `ServerCatalog`, `StationHealth` | `free/ip_registry/ports.py:23,67` | docs seulement (`docs/plan-ip-registry-allocator.md`) |
| `source_signature` | `free/egress_sources.py:403` | définition + `__all__:422` |
| `ttl_for` | `free/egress_catalog.py:258` | définition + `__all__:439` + `specs/013/contracts/option-map.md` |
| `BLOCKED` | `free/egress_catalog.py:50` | définition + `__all__` |
| `reset_from_retry_after` | `free/egress_catalog.py:416` | tests seulement |
| `ProfileAssignment`, `ProfileAudit` | `free/profile_registry.py:277,293` | specs seulement |
| `audit_profile`, `ProfileRegistry.assign`, `quota_blocks`, `transient_blocks` | `free/profile_registry.py:314,447,236,239` | `profile_verifier.py:118-119` seulement |
| `scan_audit.migrate` (legacy) | `free/ip_registry/sqlite.py:157` | appelé par `init_schema` (`:176`) seulement |
| `ControlPlaneConfig.api_key`, `RetryConfig.delay` | `free/control_plane/config.py:97,36` | tests seulement |
| `client.dns_state`, `client.cancel`, `legacy_fallback` | `free/control_plane/client.py:218,364,72` | 0 appelant de production |
| `Capabilities.supports`, `SwitchJob.raise_for_status` | `free/control_plane/models.py:57,213` | tests / 0 |
| `ScanAudit.snapshot`, `CatalogClaims.stats` | `free/scan_audit.py:265`, `free/catalog_claims.py:525` | 0 appelant de production |
| `ThreatStats` | `free/threat_intel.py:33` | constructeur + `__all__` |
| `PreflightSummary`, `from_results` | `free/ip_registry/preflight.py:47-70` | tests seulement |
| `NordVpnCatalog._parse`, `_parse_gluetun`, `_merge`, `is_stale` | `free/ip_registry/catalog.py:157,161,165,134` | 0 appelant de production |
| `AllocationReport.ok_count/failed`, `StationNeed.reason`, `IpAllocator.run` | `free/ip_registry/allocator.py:39-44,20,69` | `grep "need.reason"` → 0 ; `run()` appelé par un test |
| `sources/base.SourceSnapshot.ok`, `CatalogResolution.error`, `.complete` | `free/ip_registry/sources/base.py:36,58,64` | 0 appelant |
| `dns.DnsFallback.note_failure` (effectif) | `free/ip_registry/dns.py:39-47` | no-op avec `fallback_order: ['dot']` (P1-17) |
| `probe._body_v1` | `free/ip_registry/adapters/probe.py:88,421` | branche `v1` inatteignable (P1-13) |

### 5.3 Branches / constantes / colonnes mortes

| Cible | Fichier:ligne | Détail |
|---|---|---|
| Colonne `qualification_epochs.cursor` | `store.py:114` | 21 194 lignes à `''` ; lue par `readmodel.py:387` |
| Colonne `repair_jobs.candidates_tried` | `store.py:204` | jamais passée par `record_repair_job` (`:868-873`) |
| Colonnes `station_runtime.availability_24h`, `standby_transport` | `store.py:238-241` | jamais passées |
| `Candidate.quota_remaining`, `Candidate.rtt_ms` | `domain.py:89-90` | **0 ligne non-nulle** en base (mesuré) |
| `epochs.cursor`, `epochs.skipped` | `store.py:114,131` | `skipped` toujours 0 dans les epochs (`SUM(skipped)` = 0) |
| `sweep.py:435` : `transport=""` | `sweep.py:426-435` | `_terminal_attempt` écrit une tentative avec `transport` vide → 636 lignes `transport=''` en base |
| `_release_state_sql` sur IP jamais vue | `registry_db.py:77-83` | 5 610 des 6 557 `egress_ips` n'ont jamais eu de `last_ok_at` : la colonne `state` est `'fresh'` pour **100 %** des lignes — la machine à états `('fresh','candidate','assigned','draining','exhausted','error','retired')` de `schema.sql:110-113` n'est jamais exercée en production |

### 5.4 `TODO`/`FIXME`/`XXX`/`HACK`

**Aucun.** Vérifié :

```
$ grep -rnE "\b(TODO|FIXME|XXX|HACK)\b" free/ip_registry/ free/control_plane/ free/qualification/ \
    free/egress_catalog.py free/catalog_claims.py free/egress_sources.py free/scan_audit.py \
    free/threat_intel.py free/profile_registry.py
(0 résultat)
$ grep -rniE "#\s*(dead|unused|obsolete|deprecated|vestigial)" <même périmètre>
(0 résultat)
```

Les seules annotations sont des notes « legacy / cold path » **chargées de sens et
vérifiées par un test** (`tests/test_no_docker_exec_hot_path.py:85-105`) :
`free/ip_registry/adapters/gluetun_control.py:8-13` (cold path),
`gluetun_control_v2.py:5-7` (fallback froid), `hybrid_binder.py:131,173`.

---

## 6. Options / configuration sans consommateur

`python -I scripts/config_audit.py --strict` → `dead: 0, missing: 0`. **La gate ne
peut pas détecter les options de cette section** : sa règle (`config_audit.py:253-256`)
exige seulement qu'une clé soit lue par **un** `yaml_get` quelque part — or les
loaders de la zone (`free/ip_registry/config.py:33-181`,
`free/control_plane/config.py:112-173`) lisent **toutes** leurs clés pour
construire l'objet de config, sans qu'aucun code n'en dépende.

| Clé | Lecture | Consommateur réel | Statut |
|---|---|---|---|
| `ip_registry.allocator.refresh_min_available` | `free/ip_registry/config.py:117-119` | **aucun** — `grep` repo-wide : `config.py` + `config.yaml:1336` + un backup | **morte** |
| `ip_registry.enabled` | `free/ip_registry/config.py:33` | `status.py:381` (affichage) — la vraie porte est `allocator.mode` (`opencode.py:2383`) | **affichage seul** |
| `ip_registry.binder.mode` | `config.py` | `status.py:383` (affichage) | **affichage seul** |
| `ip_registry.db_path` | `config.py` | `sqlite_registry.py:74`, `status.py:317` — pointe vers `logs/ip_registry.db` (**0 octet**) | **morte de fait** |
| `control_plane.idempotency_window_h` | `free/control_plane/config.py:128,159` | **aucun** (tests seulement) | **morte** |
| `control_plane.transport_owner` | `config.py:129,160` | validé (`:74-77`) + `__repr__` (`:106`), jamais branché | **morte** |
| `control_plane.shadow.sample_rate` | `config.py:136,167` | aucun — la seule valeur lue est le défaut du constructeur `ShadowObserver` (`shadow.py:194`) | **morte** |
| `control_plane.shadow.compare_window_s` | `config.py:137,168` | aucun (tests) | **morte** |
| `control_plane.events.ingest` | `config.py:140,171` | aucun | **morte** |
| `control_plane.events.batch_size` | `config.py:141,172` | aucun — `EventIngestor.poll` code `limit: int = 100` en dur (`events.py:70`) | **morte** |
| `control_plane.events.poll_interval_s` | `config.py:142,173` | aucun | **morte** |
| `control_plane.mode` | `config.py:112` | **`scripts/fresh_chain.py:2911` seulement** (`is_active`). `opencode.py` ne lit jamais `mode` ; `dashboard/api.py:2572` construit le client sans le consulter | **actif mais partiel** |
| `egress_catalog.qualification.parallel_rotations` | `config.yaml:1226` | `rotation_batch.build_rotation_batch` (`rotation_batch.py:67`), module mort | **morte** |
| `egress_catalog.qualification.retention_days` | `config.yaml:1176` | `store.py:308` seulement ; `purge_attempts` sans appelant | **morte de fait** |
| `egress_catalog.qualification.station_standby_size` | `config.yaml` | `apply.py:86`, `rotation.py:34` — chaîne jamais exécutée (`station_runtime` = 0) | **morte de fait** |
| `ip_registry.allocator.probe_timeout_s`, `probe_validate_sse`, `egress_poll_s` | **absentes de `config.yaml`** | `station_adapter.py:209-216` retombe sur des défauts codés | **non documentées** |
| `ip_registry.allocator.probe_models` (3 modèles) | `config.yaml:1306-1310` | tronqué à 1 par `qualifiers.py:481` (`probe_models=(first_probe_model(app_config),)`) → la boucle de repli de `station_adapter.py:80-97` est un no-op sur ce chemin | **partiellement morte** |
| `OPENCODE_PROOF_MAX_AGE_S` | lue `free/ip_registry/adapters/probe.py:116` **seulement** | absente de `.env` et `.env.example` | **non documentée** |
| `WatchdogConfig.probe_timeout_s`, `probe_interval_s` | `watchdog.py:76-77` | posés par `runtime.py:178`, jamais lus | **mortes** |
| `dashboard/routers/qualification_routes.py:29 allow_live` | défaut `False` | toujours passé depuis `dashboard/api.py:2916` | défaut trompeur |
| `qualification.shadow_mode: false` + `allow_live=not _qualification_shadow` | `dashboard/api.py:2916` | **`allow_live=True` en production** : la route `POST /rotate?apply=true` peut modifier des stations réelles | à confirmer par l'opérateur (voir §8) |

**Options de `free/qualification` sans consommateur** : `WatchdogConfig.
probe_interval_s` (voir ci-dessus), `HttpStationProbe.timeout_s`,
`RegistryStationAdapter._clock`, `CutoverService` (flag `standby`),
`TransportPlan.attempts_done/total_attempts`.

---

## 7. Duplication

| Concept | Copie A | Copie B (et C) |
|---|---|---|
| `_container_name(station)` | `free/ip_registry/adapters/egress.py:40-41` | `free/qualification/station_adapter.py:131-132` ; **30 sites** repo-wide construisent `opencode-vpn-{n}` en clair |
| `_container_env` | `free/ip_registry/adapters/compose_binder.py:220` | `free/ip_registry/adapters/gluetun_control.py:135` |
| `transport_parts` | `free/qualification/station_adapter.py:37-43` | `free/chain_repair.py:114-122` (**octet pour octet**) |
| `_control_key` | `free/qualification/station_adapter.py:114-129` | `free/ip_registry/adapters/gluetun_control.py:158-162` |
| `_qualification_cfg` | `free/qualification/runtime.py:30-33` | `free/qualification/apply.py:60-65` |
| `_section` | `free/qualification/runtime.py:25-27` | `free/egress_runtime.py:262-264` |
| `_float`/`_int` ↔ `_as_int`/`_as_float` | `free/qualification/runtime.py:36-47` | `free/qualification/qualifiers.py:43-54` |
| `INFRA_STATUSES` | `free/qualification/domain.py:59-66` | `transport_health.py:17-24` (identique) ; ré-inliné `sweep.py:264-274` et `store.py:1287-1292` |
| Vocabulaire transport | `transport.py:20-21` | `runtime.py:70-73`, `qualifiers.py:32`, `canary.py:126`, `free/control_plane/events.py:27` |
| `SourceDefinition` (2 classes homonymes) | `free/egress_sources.py:25` | `free/ip_registry/sources/manifest.py:24` |
| `fetch_text` (3 implémentations) | `free/egress_sources.py:370` (gardé) | `scripts/fetch_proxy_lists.py:285` (nu), `free/ip_registry/sources/adapters.py:64` (nu) |
| Pondération IX | `free/egress_catalog.py:363-385` (40/25/20/10/5) | `free/qualification/scoring.py:133-171` (30/30/20/15/5) ; `rotation/infrastructure/config/schema.py:19` (3ᵉ `ScoreWeights`) ; `ip_registry/scoring.py` (4ᵉ) |
| Store de réparation | `free/qualification/store.py:198-210` (`repair_jobs`) | `free/repair_queue.py:17,34-45` (`repair_requests`) ; `free/station_repair.py:189` (état en mémoire) |
| Rétention | `rotation/infrastructure/db/retention.py:13,32` | `free/qualification/store.py:1443` ; `free/ip_registry/sqlite_registry.py:300` ; `free/egress_store.py:927` — **4 politiques indépendantes, aucune coordonnée** |
| Lecture des stations | `free/qualification/store.py:759-764` (N+1) | `free/qualification/readmodel.py:497-538` (correcte) |
| DDL de `catalog`/`claims`/`rebind_budget` | `free/catalog_claims.py:41-68` | `rotation/infrastructure/db/claims.py:13-39` (**octet pour octet**) |
| DDL de `scan_runs`/`scan_attempts` | `free/scan_audit.py:44-79` | `rotation/infrastructure/db/schema.sql:338-370` (identique) |
| DDL de `incidents` | `rotation/infrastructure/db/schema.sql:219-228` | `free/qualification/store.py:185-193` ; `scripts/station_slo_watch.py:46-58` (**3ᵉ définition, colonnes différentes**) |
| DDL de `ip_state` | `free/ip_state.py:49-72` (provider, ip, bucket_key, state…) | `free/ip_registry/sqlite.py:58-77` (egress_ip, hostname, country…) — **schémas incompatibles pour le même nom de table** |
| Sélection de version de probe | `probe._version_for:438-453` | `free/impersonation.py` (`load_spec`) ; `_wire_bytes` param par défaut `"2.2.5"` (valeur impossible) |

---

## 8. Zones non auditées / incertitudes

1. **`logs/` non inspecté** : `requests.db` (167 Mo) et `debug.log` sont hors
   périmètre. Les scripts `logs/*.py` (non suivis) référencent la zone
   (`logs/rotate_station3.py:9-10`, `logs/verify_fresh_proof.py:9-10`,
   `logs/probe_official.py:16`) mais ne sont pas du code de production.
2. **`free/egress_adapters/`, `free/egress_runtime.py`, `free/egress_store.py`,
   `free/pool/__init__.py`** : hors zone (traités par le rapport 01 et 04). Les
   constats sur `egress_store`/`egress_runtime` ici se limitent à leur interaction
   avec `registry.db` (DDL, rétention, purge).
3. **`rotation/application/`, `rotation/domain/`, `rotation/ports/`,
   `rotation/interfaces/`** : identifiés comme hors runtime par `grep`
   d'importeurs, mais non lus ligne à ligne — la conclusion « hors production »
   repose sur l'absence d'import, pas sur une revue de contenu.
4. **`allow_live=True` en production** : `dashboard/api.py:2916` calcule
   `allow_live=not _qualification_shadow` et `config.yaml:1167` porte
   `shadow_mode: false` → la route `POST /api/egress/qualification/rotate` avec
   `apply=true` peut modifier des stations réelles. Je n'ai **pas** vérifié
   l'historique d'usage de cette route (aucune trace : `station_cutovers` = 0,
   `station_runtime` = 0). **À confirmer par l'opérateur** : est-ce voulu, ou
   `shadow_mode` est-il sensé être `true` ?
5. **`free/qualification/store.py:1122` `egress_ip_in_use`** : j'ai prouvé le
   mismatch de type (`'station-1' <> 1`) et l'inversion du résultat, mais pas
   qu'aucun appelant ne compense ailleurs (`sweep.py:256` l'utilise pour produire
   `DUPLICATE`, ce qui semble bien être l'effet réel).
6. **`_body_v1 is _body`** : le rapport 01 et l'agent d'audit de
   `adapters/probe.py` affirment que les deux corps sont identiques
   (96 326 octets, même `_body_sha256`). Je n'ai **pas** revérifié cette égalité
   octet pour octet moi-même ; ce qui est prouvé ici est que la branche `v1` est
   inatteignable (`_version_for` retourne toujours `2.3.0`).
7. **`INCIDENTS` : débit d'ouverture** : j'ai mesuré 6 805 lignes/24 h et
   `durée moyenne 5,23 s`, mais je n'ai pas déterminé si la cadence vient du
   watchdog (30 s) ou de plusieurs instances concurrentes (2 epochs `running`
   suggèrent au moins un lancement concurrent). Le chiffre exact d'instances de
   daemon actives au moment de la mesure n'est pas établi.
8. **`free/qualification/preflight.py` et `free/ip_registry/preflight.py`** :
   classés « inatteignables » sur la base de `grep "preflight" free/qualification/
   daemon_control.py` → 0. Si un opérateur lance `qualification_daemon.py
   --preflight` à la main, P1-12 s'active.
9. **`scripts/station_slo_watch.py`** : troisième propriétaire DDL de `incidents`
   avec un schéma **incompatible** (`station INTEGER NOT NULL`, `started_at TEXT`,
   `started_epoch REAL`, `ip TEXT`). Le script écrit dans
   `logs/station_incidents.db` (335 872 octets, table `incidents` présente), pas
   dans `registry.db` — mais rien n'empêche de le pointer sur `registry.db`, auquel
   cas `CREATE TABLE IF NOT EXISTS` échouerait silencieusement sur le schéma
   canonique. Non traité comme finding faute de chemin de production.
10. **`free/warp_rotation.py`, `free/auto_egress.py`, `free/ip_quota.py`,
    `free/ip_state.py`, `free/probe_budget.py`** : lus uniquement pour leurs
    interactions DDL/écriture avec `registry.db` (`rotation_jobs`, `ip_state`,
    `probe_budget`, `egress_ips`). Leur logique interne est hors zone.

---

## 9. Synthèse des efforts

| Priorité | Nombre | Effort cumulé estimé |
|---|---|---|
| P0 | 8 | ~3 j (dont 2 S de correctif immédiat : P0-01, P0-07) |
| P1 | 20 | ~5 j |
| P2 | 16 | ~3 j |
| P3 | 50 | ~2 j (cosmétique / nettoyage) |

**Correctifs à effet immédiat et faible risque** (< 1 h chacun) :
P0-01 (borne du shadow), P0-07 (rétention des backups), P0-02 (PRAGMA
foreign_keys), P1-01 (kwargs de `persist_qualifier_request`), P1-04
(transaction unique), P1-05 (`BEGIN IMMEDIATE`), P1-06 (`__init__` paresseux),
P1-18 (cache de `registry_status`), P2-16 (`ANALYZE` au boot).

