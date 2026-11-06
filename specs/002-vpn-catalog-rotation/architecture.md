# Architecture cible — rotation, catalogue et données

**Feature** : `002-vpn-catalog-rotation`  
**Statut** : architecture cible appliquée par strangler ; `registry.db` est la
source runtime unique. Revue du 2026-11-05 ci-dessous.  
**But** : rendre le système optimal, fiable, maintenable, modulable et
débarrassé de la dette accumulée, sans big-bang.

---

## 0. État après migration (2026-11-05)

- `logs/registry.db` : catalogue, serveurs, IP, observations, quota, baux,
  pool chaud, sweep, incidents, états de chaîne, audit de scan.
- Les anciens `ip_registry.db`, `catalog_claims.db`, `quota_ledger.json`,
  `fresh_chain_state*.json`, `shared_rotation.json`, `latency_engine.json`,
  `free_pool.json` et `allocator_state.json` sont migrés puis archivés
  `.migrated` ; aucun n’est recréé au runtime.
- Le proxy et le superviseur utilisent `free/ip_registry/registry_db.py`
  (`egress_ips`/`leases`/`observations`) au lieu de l’ancien `ip_state`.
- Le DNS runtime est DoT avec repli plain `:53` ; l’image Gluetun est
  épinglée et le manifeste est installé atomiquement.
- Les alertes de flotte sont dans `incidents` et exposées par l’API.
- Dettes fermées en plus de D-01 : D-12, D-13, D-22.

---

## 1. Qualité visée

| Attribut | Objectif mesurable |
|---|---|
| Fiabilité | 5/6 client-utilisable ≥ 99,9 %, 6/6 ≥ 99 %, jamais 0/6 |
| Performance | remplacement 429 < 60 s p95 ; préflight < 2 s ; balayage < 24 h |
| Modularité | domaine sans I/O ; adapters remplaçables ; pas de cycle |
| Maintenabilité | aucun nouveau module > 1 500 lignes ; tests par couche |
| Observabilité | chaque décision/fallback a un événement structuré |
| Données | 0 IP partagée, 0 hostname non traçable, intégrité DB vérifiée |
| Config | 100 % des options GUI ont un consommateur et un test |
| Déploiement | migration progressive, rollback à chaque phase |

---

## 2. État actuel (dette structurelle)

- `opencode.py` ~1,4 Mo, `vpn/manager.py` ~584 Ko, `free/pool.py` ~276 Ko :
  plusieurs cycles de vie, plusieurs alphabets d'état, logique métier mêlée
  au transport et à Docker.
- `scripts/fresh_chain.py` + `fresh_chain_supervisor.py` : orchestration par
  sous-processus, fichiers JSON d'état, locks PID, logs par station.
- `free/ip_registry/` : bonne base domaine/ports/adapters, mais contournée par
  le chemin `fresh_chain`/`catalog_claims`.
- `logs/ip_registry.db` + `logs/catalog_claims.db` + `logs/free_pool.json` +
  `logs/fresh_chain_state*.json` + `logs/shared_rotation.json` +
  `logs/latency_engine.json` : plusieurs sources de vérité.
- `logs/requests.db` ~3 Go, `logs/debug.log` volumineux : pas de rétention
  structurée.
- `config.yaml` + `.env` + `docker-compose.yml` + `credentials.env` :
  divergences possibles, options GUI non prouvées.
- Hardcodes Albanie, lease 15 s, deux propriétaires de réparation, Gluetun
  `latest`, manifeste non vérifié.

Le détail chiffré est dans `debt.md` et `audit.md`.

---

## 3. Architecture cible

Architecture **hexagonale** (ports/adapters) + **use cases** + **strangler**.

```text
┌──────────────────────────────────────────────────────────────┐
│ Interfaces                                                   │
│  supervisor CLI · dashboard API · admin CLI · metrics HTTP   │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│ Application (use cases)                                     │
│  ReconcileStations · RepairStation · RotateStation           │
│  SweepCatalog · RefreshCatalog · WarmPool · ProbeStation     │
│  ApplyConfig · PublishStatus                                 │
└──────────────────────────────┬───────────────────────────────┘
                               │ ports (interfaces)
┌──────────────────────────────▼───────────────────────────────┐
│ Domain (pur, sans I/O)                                       │
│  Entities: Server, Transport, Endpoint, EgressIp, Station,   │
│            Lease, Observation, QuotaBucket, Score, Incident  │
│  Policies: SelectionPolicy, UniquenessPolicy, QuotaPolicy,   │
│            TransportPolicy, FallbackPolicy, HealthPolicy     │
└──────────────────────────────┬───────────────────────────────┘
                               │ adapters implement ports
┌──────────────────────────────▼───────────────────────────────┐
│ Infrastructure                                               │
│  SQLite repositories · migrations · unit of work             │
│  Docker/Compose binder · Gluetun control API                 │
│  HTTP probes · DNS resolver · preflight handshake            │
│  NordVPN API · Gluetun manifest · cache                     │
│  Config loader · GUI contract · event bus · metrics          │
└──────────────────────────────────────────────────────────────┘
```

### 3.1 Règles de dépendance

1. `domain/` ne importe **rien** de `infrastructure/`, `docker`, `requests`,
   `sqlite3`, `asyncio` (sauf types neutres).
2. `application/` dépend de `domain/` et de **ports** abstraits, jamais d’un
   adapter concret.
3. `infrastructure/` implémente les ports ; elle peut dépendre de `domain/`.
4. `interfaces/` compose application + infrastructure ; aucune logique métier.
5. Aucun cycle entre packages ; un test d’import vérifie les règles.
6. Le temps, l’aléatoire, le DNS, le réseau et Docker sont injectés (clock,
   rng, ports) pour être testables.

### 3.2 Arborescence proposée

```text
rotation/
  domain/
    models.py                 # dataclasses immuables, value objects
    states.py                 # états + transitions valides
    policies/
      selection.py            # score composite, disponibilité
      uniqueness.py           # une IP = une station
      quota.py                # 429, reset_at, buckets
      transport.py            # ordre WG/OV UDP/OV TCP, supports
      fallback.py             # chaînes ordonnées, timeouts, backoff
      health.py               # client_usable, drains, incidents
  application/
    reconcile_stations.py
    repair_station.py
    rotate_station.py
    sweep_catalog.py
    refresh_catalog.py
    warm_pool.py
    probe_station.py
    apply_config.py
    publish_status.py
  ports/
    repositories.py           # CatalogRepo, EgressRepo, LeaseRepo, ...
    binder.py                 # StationBinder
    prober.py                 # EgressProbe, OfficialProbe
    runtime.py                # DockerRuntime, GluetunControl
    catalog.py                # CatalogSource
    clock.py                  # Clock, Rng
  infrastructure/
    db/
      schema.sql
      migrations/
      sqlite_repositories.py
      unit_of_work.py
      retention.py
      backup.py
    docker/
      compose_binder.py
      gluetun_control.py
      station_runtime.py
    net/
      egress_probe.py         # SOCKS5h/SOCKS5/HTTP/control/docker
      official_probe.py       # 200 + contenu, 429
      dns_resolver.py         # DoH/DoT/plain fallback
      preflight.py            # handshake WG
    catalog/
      nordvpn_api.py
      gluetun_manifest.py
      cache.py
    config/
      schema.py
      loader.py
      gui_contract.py
    observability/
      events.py
      metrics.py
      incidents.py
  interfaces/
    supervisor.py             # boucle unique, jobs par station
    cli.py
    dashboard.py
    health.py
```

Les anciens chemins (`free/ip_registry`, `scripts/fresh_chain*`) restent des
**shims** pendant la migration, puis sont supprimés.

### 3.3 Workers

| Worker | Rôle | Fréquence |
|---|---|---|
| `ReconcileWorker` | état des stations, besoins, pool chaud | 5–10 s |
| `RepairWorker` | un job par station, parallèle entre stations | à la demande |
| `SweepWorker` | catalogue, préflight, batches | continu, budgeté |
| `CatalogRefreshWorker` | API + manifeste + cache | 6 h / 24 h |
| `HealthWorker` | probes synthétiques, SLO, incidents | 10–30 s |
| `RetentionWorker` | purge observations/telemetry, backup | quotidien |
| `ConfigWorker` | applique les changements GUI, contrat | à la demande |

Chaque worker est idempotent, reprend sur état persistant, et expose
`last_run_at`, `next_run_at`, `last_error`.

---

## 4. Flux de décision

```text
Reconcile
  → calcule client_usable par station (tunnel + egress + 200 + pas de 429)
  → si besoin : DRAINING + RepairJob(station)
Repair
  → WarmPool best available (IP unique) sinon Selector
  → Preflight (endpoint/transport)
  → Binder (compose/control/docker)
  → EgressProbe (fallback chain)
  → OfficialProbe (modèles de secours)
  → 200 : assignation atomique + WarmPool + événement
  → 429 : QuotaBucket/reset_at + candidat suivant (même station)
  → échec : TransportPolicy suivant, puis serveur suivant, puis boucle
```

Aucune étape ne bloque une autre station : chaque `RepairJob` est isolé.

---

## 5. Migration strangler

### Étape A — Fondations (Phase 1)

- Créer `rotation/domain` + `ports` + schéma DB v1.
- Écrire les tests domaine (score, unicité, 429, transport, fallback).
- Ne brancher aucun worker : dual-write des observations vers l’ancien et le
  nouveau schéma.

### Étape B — Canary (Phase 2)

- `ReconcileWorker` + `RepairWorker` pour **une** station.
- Comparer les décisions avec l’ancien chemin (shadow).
- Le pool chaud et le sélecteur sont actifs pour cette station.

### Étape C — Bascule (Phase 3-4)

- 3 stations, puis 6.
- Le superviseur legacy est arrêté ; ses fichiers JSON sont en lecture seule.
- `fresh_chain.py` devient un shim qui appelle les use cases.

### Étape D — Décommissionnement (Phase 6)

- Supprimer `fresh_chain*`, `catalog_claims`, les JSON d’état autoritatifs.
- Réduire `vpn/manager.py` aux adapters runtime ; extraire le reste.
- Archiver les anciens modules, pas de suppression destructive sans backup.

### Critères de bascule

- Parité de décision sur 100 cas (shadow).
- 0 double assignment, 0 IP partagée.
- SLO canary ≥ ancien chemin.
- Rollback testé.

---

## 6. ADR (décisions d’architecture)

| ADR | Décision | Raison |
|---|---|---|
| ADR-001 | SQLite WAL pour le registre | local, fiable, sans serveur ; 5–50 Mo |
| ADR-002 | Une source de vérité DB, JSON en lecture seule | fin des états divergents |
| ADR-003 | Ports/adapters | testabilité, remplacement Docker/Gluetun |
| ADR-004 | Strangler, pas big-bang | risque maîtrisé, rollback |
| ADR-005 | Tri-stack par serveur | exigence opérateur, client officiel |
| ADR-006 | Fallback chains configurables | aucune dépendance unique |
| ADR-007 | Contrat GUI | aucune option inerte |
| ADR-008 | DB registre + télémétrie séparées | perf et rétention |
| ADR-009 | Score composite 40/25/20/10/5 | performance réelle, pas le pays |
| ADR-010 | Un seul propriétaire de réparation | fin des conflits |

---

## 7. Qualité et CI

- **Import rules test** : domaine sans I/O, pas de cycle.
- **Config audit** : `scripts/config_audit.py --strict`.
- **GUI contract** : 100 % des options testées.
- **DB integrity** : `PRAGMA foreign_keys=ON`, `integrity_check` quotidien,
  migrations testées sur une copie.
- **Chaos** : panne DNS, panne site IP, panne API, panne Docker, redémarrage
  superviseur.
- **Performance** : budget par use case (préflight, bind, probe, sweep).
- **No new debt** : pas de nouveau module > 1 500 lignes, pas de `except:
  pass` silencieux, pas de clé config sans consommateur.

---

## 8. Risques de la refonte

| Risque | Mitigation |
|---|---|
| Big-bang / régression | strangler + shadow + canary |
| Double écriture incohérente | unit of work + réconciliation + comparaison |
| Migration DB destructive | backup + migrations forward-only + test sur copie |
| Deux chemins actifs | feature flag par station, un seul propriétaire |
| Dérive de config | schéma typé + contrat GUI + audit CI |
| Temps de refonte | phases, valeur livrée à chaque phase |
