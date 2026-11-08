# Implementation Plan: Toutes les stations et toutes les IP utilisables

**Branch**: `011-station-fleet-usability` | **Date**: 2026-11-07 | **Spec**: [spec.md](./spec.md)  
**Input**: Feature specification from `specs/011-station-fleet-usability/spec.md`.  
**Audit**: [docs/AUDIT_STATIONS_NORDVPN_WARP_2026-11-07.md](../../docs/AUDIT_STATIONS_NORDVPN_WARP_2026-11-07.md).

## Summary

Rendre les **six stations NordVPN** et les **six endpoints WARP** réellement
client-utilisables, avec des IP/buckets uniques, une maintenance autonome et
une rotation qui préserve les slots.

Le plan corrige d'abord la cause racine observée le 2026-11-07 : trois writers
(`.env`, mémoire du manager, compose) se disputent le stack des stations, le
reconcile de boot supprime les conteneurs, le superviseur s'arrête parce que
NordVPN n'est pas sélectionné, et la config WARP n'est jamais réconciliée avec
l'egress live. Il introduit ensuite un cycle de vie déclaratif (desired state →
actual state → reconcile → health → repair → rotation) qui traite les deux
providers avec les mêmes invariants.

**Consolidation** : NordVPN et WARP partagent un noyau unique (slot, états,
santé, réparation, rotation, budgets, preuves, statut) et n'exposent que leurs
mécanismes spécifiques via deux adapters. Les duplications de normalisation de
bloc, de résolveur d'egress, de réparation, de statut et de logs sont
supprimées.

**Principe directeur** : on ne répare pas une boucle de reconcile par un
restart de plus. On supprime la boucle en rendant l'état désiré unique et le
reconcile idempotent.

## Technical Context

**Language/Version**: Python 3.12+, PowerShell 7 pour les scripts Windows.  
**Primary Dependencies**: FastAPI, Docker CLI/Compose, Gluetun, `caomingjun/warp`, SQLite (WAL), `curl_cffi`, pytest.  
**Storage**: `logs/registry.db` (source de vérité), `logs/requests.db` (télémétrie), `config.yaml`/`.env` (intention opérateur), JSON `logs/*` (exports en lecture seule à terme).  
**Testing**: pytest unitaire/contrat sans Docker, tests d'intégration avec runner Docker factice, tests live opt-in, `svelte-check`/build pour le GUI.  
**Target Platform**: Windows 10/11 + Docker Desktop, conteneurs Linux Gluetun/WARP.  
**Project Type**: service web + CLI/scripts + GUI Svelte.  
**Performance Goals**: reconcile complet < 2 s pour 12 endpoints ; API stations p95 < 300 ms ; MTTR < 180 s ; < 3 restarts/h/station ; rotation p95 < 240 s.  
**Constraints**: free-only, pas de second compte, pas de payant, pas d'attente comme solution ; un seul processus proxy ; ports loopback ; reset fournisseur = barrière dure.  
**Scale/Scope**: 6 stations NordVPN + 6 endpoints WARP (extensible 10+10), 1 compte NordVPN, 2 providers, 12 buckets actifs.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principe | Gate | Statut plan |
|---|---|---|
| I — disponibilité mesurée | `client_usable` exige une preuve, fenêtres 1 h/24 h/7 j/30 j | **PASS** — machine à états + `observations`/`incidents` |
| II — trois cycles de vie | station, session, IP séparés | **PASS** — entités distinctes, leases séparés |
| III — source de vérité | `registry.db` autorité | **PASS** — desired state + station_state + rotation_jobs |
| IV — reset fournisseur | aucune IP avant `reset_at` | **PASS** — ledger + reset_at |
| V — unicité IP | 1 egress = 1 station | **PASS** — contrainte unique + buckets |
| VI — indépendance stations | pas de quota pays | **PASS** — inchangé |
| VII — preuve de succès | 200 avec contenu, bind avec egress | **PASS** — probe client |
| VIII — un seul réparateur | superviseur propriétaire | **PASS** — watchdog observateur |
| IX — score vs portes dures | pas de contournement | **PASS** — inchangé |
| X — dégradation explicite | pas de faux 6/6 | **PASS** — verdict basé preuves |
| XI — fallbacks | plusieurs chemins | **PASS** — WARP/NordVPN/proxys/direct |
| XII — options GUI effectives | chaque option a un consommateur | **PASS** — contrat UI |
| XIII — traçabilité | migrations, backups, rétention | **PASS** — migration registre |
| XIV — modularité | modules < 1 500 lignes, strangler | **PASS** — nouveaux modules purs, flag shadow |

**Aucune violation à justifier.** Le plan remplace un monolithe existant par
des modules ciblés, sans créer de nouveau monolithe ; il conserve les anciens
chemins derrière un flag jusqu'à validation.

## Project Structure

### Documentation (this feature)

```text
specs/011-station-fleet-usability/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── station-lifecycle.md
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
ops/
├── desired_state.py        # NOUVEAU — DesiredStationSpec/DesiredEndpointSpec, génération
├── reconciler.py           # NOUVEAU — diff desired/actual, actions idempotentes
├── lifecycle.py            # NOUVEAU — SlotSpec/Observation/State, moteur commun
├── lifecycle_policy.py     # NOUVEAU — santé, réparation, rotation, budgets partagés
├── lifecycle_evidence.py   # NOUVEAU — preuves, incidents, logs, statut commun
├── providers/
│   ├── base.py             # NOUVEAU — SlotProvider Protocol
│   ├── nordvpn.py          # NOUVEAU — adapter Gluetun/Compose/auth
│   └── warp.py             # NOUVEAU — adapter WARP/docker run/registration
├── fleet.py                # étendu — actions start/stop/rotate/repair par station
├── health.py               # étendu — classification, anti-flapping
└── supervisor.py           # étendu — un superviseur par station

free/
├── station_health.py       # étendu — états lifecycle + preuve client
├── warp_provisioner.py     # NOUVEAU — plan/apply create/delete WARP
├── egress_reconcile.py     # NOUVEAU — live → config → ledger
├── capacity.py             # étendu — unique_buckets, client_usable
├── pool.py                 # étendu — comptage buckets, refresh périodique
├── warp_rotation.py        # étendu — budget persisté, unicité /64
└── quota_ledger.py         # étendu — reset_at, source de vérité

vpn/
├── manager.py              # réduit — délègue reconcile/repair, plus de rm destructeur
└── auth.py                 # inchangé — garde auth globale

station_repair.py           # étendu — policy unique, classification par cause
scripts/
├── fresh_chain_supervisor.py # étendu — maintenance indépendante de la sélection
└── station_audit.py         # NOUVEAU — audit CLI JSON des 12 stations

rotation/infrastructure/db/
├── schema.sql              # migration — desired_state, rotation_jobs, colonnes
└── migrate.py              # migration versionnée + backfill

dashboard/
└── api.py                  # étendu — /api/stations, /api/stations/{id}, actions

dashboard-ui/src/pages/vpn/
├── VpnStations.svelte      # NOUVEAU — vue par station
└── VpnCapacity.svelte      # étendu — buckets uniques

tests/
├── test_desired_state.py   # NOUVEAU — validation, génération, idempotence
├── test_reconciler.py      # NOUVEAU — diff, non-destructif, rollback
├── test_warp_provisioner.py# NOUVEAU — plan/apply, unicité /64, ports
├── test_egress_reconcile.py# NOUVEAU — drift live/config/ledger
├── test_station_lifecycle.py # NOUVEAU — états, preuves, réparation
├── test_capacity.py        # étendu — buckets uniques
├── test_warp_rotation.py   # étendu — budget persisté
└── test_station_audit.py   # NOUVEAU — CLI JSON
```

**Structure Decision** : le domaine (desired state, diff, décisions) reste pur
dans `ops/desired_state.py` et `ops/reconciler.py` ; les adapters Docker,
Compose, registre et probes sont injectés. Les modules existants sont étendus
par strangler, pas réécrits.

## Phase 0 — Recherche et décisions

Les décisions sont dans [research.md](./research.md). Elles résolvent les points
suivants :

1. Un seul desired state avec génération (D-01).
2. Reconcile non destructeur (D-02).
3. Superviseur seul réparateur (D-03).
4. Maintenance indépendante de la sélection (D-04).
5. Santé par étapes + preuve client (D-05).
6. Capacité par buckets uniques (D-06).
7. Réconciliation live WARP (D-07).
8. Budgets de rotation persistés (D-08).
9. Reset fournisseur (D-09).
10. Provisioning WARP déclaratif (D-10).
11. Quota conservateur, mesure réelle (D-11).
12. Cible 6 slots, spare 0 tant que la limite de sessions n'est pas mesurée (D-12).
13. Observabilité registre (D-13).
14. Sécurité loopback/token (D-14).
15. Migration strangler shadow→enforce (D-15).

**Points mesurés, pas bloquants** : limite exacte de sessions NordVPN, plafond
de quota par bucket, opportunité d'un spare. Le plan prévoit les campagnes.

## Phase 1 — Design et contrats

### 1.1 Modèle de données

Voir [data-model.md](./data-model.md). Résumé :

- `desired_state` : `provider`, `station`, `generation`, `spec_json`, `updated_at`.
- `station_state` (existant) : ajouter `provider`, `generation`, `client_usable`, `last_evidence_at`.
- `egress_endpoints` : `provider`, `label`, `container`, `host`, `port`, `ipv6`, `image`, `enabled`, `generation`, `egress_ip`, `block`.
- `rotation_jobs` : `station`, `provider`, `trigger`, `old_ip`, `new_ip`, `result`, `attempts`, `started_at`, `ended_at`.
- `repair_jobs` (existant) : ajouter `action`, `evidence`, `generation`.
- `leases`, `egress_ips`, `quota_buckets` : inchangés, avec `provider`/`block_key` déjà présents.

Contraintes :

- `UNIQUE(provider, station)` pour `desired_state`.
- `UNIQUE(provider, label)` pour `egress_endpoints`.
- Index `egress_ips(provider, block_key, state)`.
- `CHECK` sur les états et les providers.
- Migration versionnée + backup `VACUUM INTO` avant application.

### 1.2 Contrat d'interface

Voir [contracts/station-lifecycle.md](./contracts/station-lifecycle.md).

- `GET /api/stations` : liste des 12 stations avec états et preuves.
- `GET /api/stations/{provider}/{station}` : détail, bucket, quota, dernière action.
- `POST /api/stations/{provider}/{station}/reconcile` : action idempotente.
- `POST /api/stations/{provider}/{station}/rotate` : rotation explicite.
- `POST /api/stations/{provider}/{station}/repair` : réparation explicite.
- `GET /api/capacity` : rapport avec `unique_buckets` et `client_usable`.
- CLI `python scripts/station_audit.py --json` : même vérité, sans GUI.

### 1.3 Quickstart

Voir [quickstart.md](./quickstart.md) : scénarios de validation, commandes,
résultats attendus, preuves live.

## Phase 2 — Implémentation

### Phase 2.0 — Stabilisation P0 (priorité absolue)

**Objectif** : arrêter la boucle et remettre les 12 stations en état de être
réparées.

| Tâche | Fichier | Changement |
|---|---|---|
| P0-1 | `vpn/manager.py:11190-11324` | Le reconcile compare le conteneur au **desired state** (ou, à défaut, à l'environnement réellement passé à compose), jamais au seul `.env`. Aucun `rm -f` si le conteneur est conforme. |
| P0-2 | `vpn/manager.py:5394-5485` | Une seule source de stack : `_compose_env` écrit/ lit le même `DesiredStationSpec` ; pas d'override mémoire divergent. |
| P0-3 | `scripts/fresh_chain_supervisor.py:1236-1241` | La maintenance ne s'arrête plus si NordVPN n'est pas sélectionné ; seule la **consommation trafic** est conditionnée. |
| P0-4 | `config.yaml:409-432` | `station_repair.enabled: true` ; `repair_owner: supervisor` ; budgets cohérents. |
| P0-5 | `vpn/manager.py` watchdog | Le watchdog signale et attend le superviseur ; il ne recrée plus une station prise en charge. |
| P0-6 | `config.yaml` / warm pool | Purger les pins `us10506/us10019/us10508/us10018` ; re-pin sur des hosts vivants (`al41` ou catalogue validé) ; recréer la station 2. |
| P0-7 | `free/pool.py` + `free/warp_rotation.py` | Réconcilier l'egress live des 6 WARP, détecter le doublon `2a09:bac1:27a0:2060::/64`, tourner l'endpoint en double. |
| P0-8 | `opencode.py` / serveur | Le proxy écoute (vérifié à 14:48) mais le statut ment : dériver les compteurs de `station_health` et exposer un mode dégradé honnête ; `/healthz` ne doit pas dire 6/6 avec `boot_error 0/6`. |
| P0-9 | `free/warp_rotation.py` / `free/warp_provisioner.py` | Publier les SOCKS5 WARP sur `127.0.0.1` uniquement (aujourd'hui `0.0.0.0` sans auth) ; rattacher `free.pool`, `free.warp_rotation`, `free.quota_ledger` aux logs. |
| P0-10 | `free/pool.py` / `dashboard/api.py` | Corriger le faux 6/6 : `usable_stations`, `unique_usable_ips`, `active_stations_with_quota`, `capacity_degraded` = même ensemble `client_usable` que `station_health` (F-32). |
| P0-10 | `vpn/manager.py:2093-2147` / `opencode.py:3431-3433` | Un propriétaire de démarrage explicite : `start()` connecte les stations désirées même si `repair_owner=supervisor` ; le boot ne se contente plus de `start()`. |
| P0-11 | `vpn/manager.py`, `scripts/fresh_chain_supervisor.py`, `scripts/fresh_chain.py` | Un seul writer `.env` atomique (lock inter-processus, tmp unique) ; pas de `.env.tmp` partagé. |
| P0-12 | `opencode.py:2370-2712` | Upscale : préflight ports/compose, rollback complet (count, registry, conteneur) si `connect()` échoue. |
| P0-13 | `docker-compose.yml:181-183` | Remplacer `depends_on: service_healthy` de la station 1 par `service_started` + healthcheck applicatif, pour que le proxy démarre même si la station 1 est down. |

**Critère de sortie P0** : 6 conteneurs NordVPN existent, aucun `rm -f`
injustifié pendant 30 min, le proxy écoute sur `:4000`, les 6 WARP ont 6 `/64`
uniques, et le superviseur ne s'arrête plus.

### Phase 2.1 — Desired state et registre (US1/US2)

- `ops/desired_state.py` : dataclasses pures, validation, génération.
- `rotation/infrastructure/db/schema.sql` : `desired_state`, `rotation_jobs`,
  colonnes `provider`/`generation`.
- `rotation/infrastructure/db/migrate.py` : backfill depuis `config.yaml`,
  `.env`, `socks5_proxies`, `station_count`.
- `ops/reconciler.py` : diff `desired → actual` ; actions `create`, `recreate`,
  `start`, `stop`, `noop` ; jamais de suppression sans preuve d'obsolescence.
- `ops/fleet.py` : actions idempotentes par station.
- Tests : `test_desired_state.py`, `test_reconciler.py`.

### Phase 2.1b — Consolidation inter-providers (US7)

**Objectif** : un seul moteur pour les concepts communs, deux adapters pour les
mécanismes spécifiques.

```text
ops/lifecycle.py            # SlotSpec, SlotObservation, SlotState, décisions
ops/lifecycle_policy.py     # santé, réparation, rotation, budgets, quota
ops/lifecycle_evidence.py   # preuves, incidents, logs, statut commun
ops/providers/base.py       # SlotProvider Protocol
ops/providers/nordvpn.py    # adapter Gluetun/Compose/auth/session
ops/providers/warp.py       # adapter WARP/docker run/registration
```

**Partagé** :

- identité de slot, génération, enabled ;
- machine à états et transitions ;
- classification des causes et actions de réparation ;
- budget de rotation, park, reset ;
- normalisation des buckets et des familles ;
- résolution d'egress et probe ;
- preuve, incident, log, statut.

**Spécifique aux adapters** :

- NordVPN : compose, credentials, AUTH throttle, session, transport WG/OV ;
- WARP : `docker run`, registration Cloudflare, image, `/64`, options de
  conteneur ; proxies/direct : endpoint statique.

**Règles de consolidation** :

1. `block_for_ip` (`free/warp_rotation.py:58-65`) et `block_for_egress`
   (`free/egress_provider.py:15-22`) → une seule fonction
   `quota_block(ip)`.
2. Les deux résolveurs d'egress (`free/pool.py:350-416` et
   `free/warp_rotation.py:366-410`) → `free/egress_probe.py`.
3. `RepairPolicy` de `station_repair.py` → appliqué aux deux providers ;
   les actions spécifiques sont des méthodes d'adapter.
4. `WarpRotator` et la rotation NordVPN → même `RotationBudget` persisté,
   même taxonomie de déclencheurs, même trace.
5. Les statuts `VPNManager.get_status()` et `Socks5Endpoint.get_status()` →
   `SlotStatus` commun + `provider_details`.
6. Les loggers `free.pool`/`free.warp_rotation`/`station_repair` → un pipeline
   unique rattaché au fichier de logs.

**Non-objectif** : forcer un modèle unique de création. Un conteneur Gluetun et
un conteneur WARP n'ont ni les mêmes options, ni les mêmes credentials, ni le
même cycle de registration. L'adapters garde ces différences.

**Tests** : `test_lifecycle_contract.py`, `test_provider_parity.py`,
`test_consolidation_no_duplicates.py`.

### Phase 2.2 — Provisioning WARP et création NordVPN (US2)

- `free/warp_provisioner.py` : `plan()` / `apply()` ; noms, ports, image,
  `ipv6`, `managed`, `provider`, `block`, `generation` ; unicité `/64` ;
  bind `127.0.0.1` ; rollback sur échec ; réapplication des options
  (`-e`, limites mémoire/PID, labels, devices) ; image épinglée par digest.
- `vpn/manager.py` : `_ensure_container` devient idempotent par génération ;
  préflight des ports ; pas de recréation si conforme.
- `tests/test_warp_provisioner.py` : plan vide, création, doublon, rollback,
  port occupé, 409, options préservées.
- Script de migration : créer les 6 endpoints manquants, adopter les existants.

### Phase 2.3 — Santé, réparation et propriétaire unique (US3)

- `free/station_health.py` : états `created/starting/tunnel_up/dns_ok/egress_ok/quota_ok/client_usable/degraded/parked/failed` ; preuve horodatée.
- `station_repair.py` : classification `auth`, `server_dead`, `dns`, `transport`, `quota_429`, `healthcheck`, `proxy_down` ; une action par cause ; toutes les clés de policy réellement lues (`max_recreates_per_hour`, `park_after_failures`, `verify_egress_before_usable`, `last_good_first`).
- `ops/supervisor.py` : un superviseur par station ; le watchdog passe en
  observateur ; `repair_owner` hot-reloadable.
- `scripts/fresh_chain_supervisor.py` : boucle de réparation indépendante de
  `egress_providers` ; un seul job par station ; `/api/vpn/next` et
  `pin_country` passent par le superviseur.
- Tests : `test_station_lifecycle.py`, `test_station_repair.py` (table-driven),
  simulation des 7 causes, test des clés de policy, test du verrou
  inter-processus.

### Phase 2.4 — Rotation et quotas (US4)

- `free/warp_rotation.py` : budget horaire persisté, `LAST_ROTATION` dans le
  registre, refus des blocs dupliqués même après restart, lecture de
  `station_max_rotations_per_hour`, une seule recréation par rotation,
  gate `upstream_event`, logs rattachés.
- `free/egress_reconcile.py` : refresh périodique, détection de dérive,
  écriture atomique `egress_ip`/`block`/ledger, détection des conteneurs morts.
- `free/quota_ledger.py` : `reset_at` fournisseur, source de vérité, jambe
  `provider`/`bucket`, écriture réelle de `model`/`provider` (pas
  `model='default'`).
- `free/rotation.py` : rotation préventive quota/latence/âge, seuils config.
- `free/pool.py` : exposer `egress_ip`/`block`/`managed`/`container`/`ipv6`,
  calculer les doublons et les parks (fin des zéros forcés).
- Tests : rotation, budget, doublon, reset, crash/restart, 429, conteneur mort,
  gate amont, logs.

### Phase 2.5 — Capacité et observabilité (US1/US5)

- `free/capacity.py` : `unique_buckets`, `client_usable`, `verdict` basé sur
  les preuves.
- `dashboard/api.py` : `/api/stations`, `/api/capacity`, actions ; lecture du
  registre.
- `dashboard-ui/src/pages/vpn/VpnStations.svelte` : vue par station avec état,
  IP, bucket, quota, preuve, prochaine action.
- `VpnCapacity.svelte` : buckets uniques, déficit réel.
- `scripts/station_audit.py` : audit CLI JSON, même calcul que l'API.
- Tests : `test_capacity.py` (6 endpoints/5 blocs), API, `svelte-check`.

### Phase 2.6 — Sécurité et exploitation (US6)

- Bind `127.0.0.1` pour WARP ; audit `docker ps`.
- Token dashboard obligatoire si `server.host != 127.0.0.1` ; token sur `/api/vpn/diagnostic`.
- Secret de control uniquement dans `credentials.env` (pas dans `.env` injecté au proxy).
- Refresh credentials atomique, avec clé NordLynx dans `vpn_configs/wireguard.env` et restart contrôlé.
- Image WARP épinglée par digest ; runbook de mise à jour ; updater NordVPN aligné sur le digest.
- Secrets : jamais dans les logs ; rotation documentée.
- Tests : `test_security_bind.py`, `test_dashboard_token.py`,
  `test_credentials_refresh_wg.py`, `test_updater_digest.py`.

### Phase 2.7 — Validation, chaos et soak

- `scripts/station_audit.py` : 12/12, 12 buckets, 0 doublon.
- Chaos : tuer un tunnel, couper un hostname, épuiser un quota, redémarrer
  Docker, couper le proxy, injecter un stack divergent.
- Soak 24 h : `client_usable` 6/6 NordVPN + 6/6 WARP, 0 doublon, 0 `rm -f`
  injustifié, MTTR < 180 s, < 3 restarts/h/station.
- Rapport dans `docs/evidence/011-fleet-usability.md`.

## Test baseline 2026-11-07

Les tests de plan sont exécutables avant l'implémentation. Ils mélangent
**caractérisation** (le comportement actuel) et **gaps** (`xfail(strict=True)`).

| Suite | Commande | Résultat |
|---|---|---|
| Baseline statique | `python -m pytest tests/test_plan_011_baseline.py -q` | **18 passed, 0 xfailed** |
| Consolidation | `python -m pytest tests/test_plan_011_consolidation.py -q` | **9 passed, 0 xfailed** |
| Desired/reconciler/provisioner | `python -m pytest tests/test_desired_state.py tests/test_reconciler.py tests/test_warp_provisioner.py -q` | **23 passed** |
| Budget/.env | `python -m pytest tests/test_rotation_budget.py tests/test_env_store.py -q` | **4 passed** |
| Live Docker | `python -m pytest tests/test_plan_011_live.py -m docker -q` | **5 XPASS, 1 XFAIL** |

**Tests de caractérisation qui passent** :

- `egress_providers=[warp]`, `station_count=6`, `repair_owner=supervisor`,
  `station_repair.enabled=false` ;
- six services NordVPN déclarés dans le compose ;
- six blocs WARP uniques **dans la config statique** ;
- capacité `ok` avec 6 endpoints et 5 buckets (le bug est reproduit) ;
- `Socks5Endpoint.status == "connected"` sans egress ;
- superviseur considère NordVPN non sélectionné.

**Gaps implémentés pendant cette itération** :

- `quota_block()` unique + alias `block_for_egress`/`block_for_ip` ;
- `free/egress_probe.py` unique ;
- `free/egress_reconcile.py` + détection des buckets dupliqués ;
- `WarpRotator` loopback/options/budget ;
- capacité `unique_buckets` + warning `duplicate_block` ;
- `SlotStatus` commun et statut WARP enrichi ;
- `ops/lifecycle*`, `ops/desired_state.py`, `ops/reconciler.py`,
  `ops/providers/*` ;
- `free/warp_provisioner.py` plan/apply/rollback ;
- `/api/stations`, `/api/stations/{provider}/{station}`,
  `/api/vpn/socks5/refresh-egress` ;
- `scripts/station_audit.py` ;
- logs `free.pool`/`free.warp_rotation`/`free.quota_ledger`/`station_repair` ;
- maintenance NordVPN indépendante de la sélection ;
- reconcile non destructif pour un conteneur vivant ;
- credentials NordVPN atomiques + clé WireGuard.

**Live après implémentation (2026-11-07 15:35)** :
- 6/6 conteneurs NordVPN créés ; 6/6 blocs WARP uniques ;
- capacité WARP honnête : `usable_stations=6`, `capacity_degraded=false`,
  `boot_error="boot 3/6..."` ; plus de faux 6/6 ;
- WARP 6/6 client-utilisables, 6 `/64` uniques, ports loopback ;
- NordVPN 2–3/6 client-utilisables selon les probes (variable) ; 1, 3 et 6
  restent en `AUTH_FAILED`/tunnel sans réponse malgré token et credentials
  valides (`refresh_nord_credentials.py --check` → 200) ; blocage externe
  compte/réseau, pas un bug de cycle de vie.

**Soak court 120 s** : WARP 6/6 stable, 6 buckets uniques, 0 doublon ;
NordVPN 3/6 stable. Un soak 24 h est démarré (JSONL `logs/station_soak.jsonl`,
synthèse `logs/station_soak.json`).

**Règle de mise à jour** : un `xfail(strict=True)` qui passe en XPASS casse la
suite. Il faut alors retirer le marqueur, cocher la tâche correspondante et
mettre à jour ce plan. C'est le lien entre les tests et le plan.

## Fichiers touchés (récapitulatif)

| Fichier | Rôle |
|---|---|
| `ops/desired_state.py` | Domaine pur : specs, génération, validation. |
| `ops/reconciler.py` | Diff desired/actual, actions idempotentes. |
| `ops/lifecycle.py` | Moteur commun NordVPN/WARP : slots, états, décisions. |
| `ops/lifecycle_policy.py` | Santé, réparation, rotation, budgets partagés. |
| `ops/lifecycle_evidence.py` | Preuves, incidents, logs, statut commun. |
| `ops/providers/base.py` | Protocole `SlotProvider`. |
| `ops/providers/nordvpn.py` | Adapter Gluetun/Compose/auth. |
| `ops/providers/warp.py` | Adapter WARP/docker run/registration. |
| `ops/fleet.py` | Actions flotte par station. |
| `free/warp_provisioner.py` | Provisioning WARP plan/apply. |
| `free/egress_reconcile.py` | Réconciliation live → config → ledger. |
| `free/station_health.py` | États lifecycle + preuve. |
| `free/warp_rotation.py` | Budget persisté, unicité. |
| `free/pool.py` | Comptage buckets, refresh. |
| `free/capacity.py` | Capacité par buckets uniques. |
| `vpn/manager.py` | Reconcile non destructeur, watchdog observateur. |
| `station_repair.py` | Classification et policy unique. |
| `scripts/fresh_chain_supervisor.py` | Maintenance indépendante de la sélection. |
| `scripts/station_audit.py` | Audit CLI. |
| `rotation/infrastructure/db/schema.sql` | Migration. |
| `rotation/infrastructure/db/migrate.py` | Backfill. |
| `dashboard/api.py` | API stations/capacité. |
| `dashboard-ui/src/pages/vpn/VpnStations.svelte` | Vue stations. |
| `dashboard-ui/src/pages/vpn/VpnCapacity.svelte` | Buckets uniques. |
| `config.yaml` | Flags, budgets, providers. |
| `tests/*` | Unités, contrats, intégration, live. |

## Tests

### Unitaires purs

- `DesiredStationSpec` : validation provider/stack/transport/port/generation.
- `reconcile()` : table de décisions (conforme, stack différent, génération
  différente, orphelin, port occupé, Docker down).
- `unique_buckets()` : 6 endpoints/5 blocs → 5 ; 6/6 → 6 ; IPv4 non compté.
- `classify_failure()` : 7 causes → 7 actions.
- `rotation_budget()` : plafond horaire, restart, backoff.

### Intégration (sans Docker réel)

- Runner Docker factice : création, inspect, rm, rollback.
- Registre SQLite temporaire : migration, backfill, transactions.
- API FastAPI : `/api/stations`, `/api/capacity`, actions.
- GUI : `npm run check`, `npm run build`.
- `tests/test_plan_011_baseline.py` : caractérisation + gaps du plan.
- `tests/test_plan_011_consolidation.py` : contrat de consolidation US7.

### Live (opt-in)

- `tests/test_plan_011_live.py` (marqueur `docker`) : 6+6, buckets, loopback,
  proxy, compteurs cohérents.
- 6 NordVPN + 6 WARP, 12 buckets, probes client.
- Rotation d'une station, doublon WARP, 429, reset.
- Redémarrage du proxy pendant une rotation.
- Chaos : tunnel tué, host mort, Docker restart.

## Migration et rollout

1. **Shadow** : `station_lifecycle.mode = shadow` ; le nouveau reconcile
   calcule les décisions mais n'agit pas ; comparaison avec l'ancien chemin
   dans les logs.
2. **Canary** : `mode = canary` sur la station 1 et l'endpoint WARP 1 ;
   actions réelles, surveillance 24 h.
3. **Enforce** : `mode = enforce` pour les 12 ; l'ancien chemin reste
   désactivable par flag pendant 2 versions.
4. **Nettoyage** : suppression des JSON de décision, des writers concurrents et
   du reconcile destructeur.

## Risques et mitigations

| Risque | Mitigation |
|---|---|
| Limite de sessions NordVPN inconnue | Campagne 4/5/6/7/8 avant tout spare ; auth guard + stagger. |
| WARP réutilise un `/64` | Unicité vérifiée avant création et à chaque refresh ; rotation ciblée. |
| Course Docker (watchdog/reconcile) | Un seul propriétaire, verrou par station, génération. |
| Migration registre | Backup `VACUUM INTO`, migration versionnée, rollback. |
| GUI stale | Lecture registre, pas mémoire ; `/api/stations` p95 < 300 ms. |
| Rotation storm | Budget horaire persisté, backoff, park. |
| Secrets | Audit logs, `.env` non suivi, token dashboard. |
| Windows/Docker Desktop | Tests avec runner factice + live opt-in. |

## Critères de sortie

- **G1** : 6/6 NordVPN et 6/6 WARP `client_usable`, 12 buckets uniques, 24 h.
- **G2** : 0 doublon, 0 `rm -f` injustifié sur 100 cycles.
- **G3** : MTTR < 180 s, < 3 restarts/h/station, 0 boucle > 10 min.
- **G4** : capacité `ok` uniquement si `unique_buckets >= target_slots`.
- **G5** : proxy `:4000` en < 30 s, mode dégradé explicite.
- **G6** : 0 port WARP sur `0.0.0.0`, token dashboard si non-loopback.
- **G7** : 100 % des actions tracées dans le registre.

## Complexity Tracking

Aucune violation de la constitution. Le plan introduit cinq modules purs
(`desired_state`, `reconciler`, `warp_provisioner`, `egress_reconcile`,
`station_audit`) et étend les modules existants par strangler. Aucun nouveau
module ne dépasse 1 500 lignes ; les responsabilités sont séparées
(domaine/adapters/API/GUI).
