# Modèle de données — serveurs, IP, quota et décisions

**Feature** : `002-vpn-catalog-rotation`  
**Statut** : plan à valider — aucune implémentation  
**Objectif** : une base de données fiable, normalisée, traçable et rapide pour
les serveurs, les IP d’egress, le quota, les performances et les décisions.

---

## 1. Principes

1. **Une source de vérité** : `registry.db` (SQLite WAL). Les fichiers JSON
   (`free_pool.json`, `fresh_chain_state*.json`, `shared_rotation.json`,
   `latency_engine.json`) deviennent des exports en lecture seule, puis sont
   supprimés.
2. **Séparer le registre et la télémétrie** : `registry.db` (petit, critique)
   et `telemetry.db` (volumineux, rétention courte) pour éviter que 3 Go de
   requêtes ralentissent les décisions.
3. **Append-only pour les observations** : on n’écrase jamais l’historique ;
   les scores sont matérialisés à part.
4. **Provenance et fraîcheur** pour chaque serveur, endpoint et IP.
5. **Contraintes en base** : l’unicité d’IP et les transitions d’état ne
   dépendent pas du code applicatif seul.
6. **Migrations versionnées**, forward-only, testées sur copie.
7. **Rétention et sauvegarde** explicites ; pas de croissance infinie.
8. **Pas de JSON pour les champs requêtables** ; JSON uniquement pour le brut
   de debug.

---

## 2. Fichiers de base

| Fichier | Contenu | Rétention |
|---|---|---|
| `logs/registry.db` | catalogue, serveurs, transports, endpoints, IP, observations, quota, leases, warm pool, sweep, jobs, incidents | observations 30 j, incidents 90 j, reste illimité |
| `logs/telemetry.db` | requêtes réelles, latences, tokens, modèles, statuts | 7–30 j + agrégats |
| `logs/backups/registry-YYYYMMDD.db` | copie `VACUUM INTO` | 7 jours |
| `logs/backups/telemetry-YYYYMMDD.db` | copie | 2 jours |

`PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`,
`busy_timeout=5000`, `wal_autocheckpoint=1000`.

---

## 3. Schéma `registry.db`

### 3.1 Méta et sources

```sql
CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  REAL NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE catalog_sources (
    source_id      TEXT PRIMARY KEY,       -- nordvpn_api, gluetun_manifest, cache, embedded
    kind           TEXT NOT NULL,          -- api | manifest | cache | embedded
    fetched_at     REAL,
    etag           TEXT,
    stale          INTEGER NOT NULL DEFAULT 0,
    server_count   INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    last_ok_at     REAL
);
```

### 3.2 Serveurs et transports

```sql
CREATE TABLE servers (
    hostname        TEXT PRIMARY KEY,
    country         TEXT NOT NULL,
    city            TEXT,
    region          TEXT,
    load            REAL NOT NULL DEFAULT 0,
    status          TEXT,                  -- online/offline (API)
    source_id       TEXT NOT NULL REFERENCES catalog_sources(source_id),
    api_seen_at     REAL,
    manifest_seen_at REAL,
    first_seen_at   REAL NOT NULL,
    last_seen_at    REAL NOT NULL,
    stale           INTEGER NOT NULL DEFAULT 0,
    score_host      REAL NOT NULL DEFAULT 0,
    CHECK (hostname <> ''),
    CHECK (country <> '')
);
CREATE INDEX idx_servers_country ON servers(country, stale, score_host DESC);
CREATE INDEX idx_servers_source ON servers(source_id, last_seen_at DESC);
CREATE INDEX idx_servers_score ON servers(score_host DESC, stale);

CREATE TABLE server_transports (
    hostname   TEXT NOT NULL REFERENCES servers(hostname) ON DELETE CASCADE,
    transport  TEXT NOT NULL CHECK (transport IN ('wireguard','openvpn-udp','openvpn-tcp')),
    supported  INTEGER NOT NULL DEFAULT 0,
    wg_pubkey  TEXT,
    last_ok_at REAL,
    last_error TEXT,
    PRIMARY KEY (hostname, transport)
);
CREATE INDEX idx_transport_supported ON server_transports(transport, supported, hostname);
```

### 3.3 Endpoints

```sql
CREATE TABLE endpoints (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname     TEXT NOT NULL REFERENCES servers(hostname) ON DELETE CASCADE,
    transport    TEXT NOT NULL CHECK (transport IN ('wireguard','openvpn-udp','openvpn-tcp')),
    endpoint_ip  TEXT NOT NULL,
    port         INTEGER,
    source       TEXT NOT NULL,            -- api | manifest | dns | cache
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    last_rtt_ms  REAL,
    last_ok_at   REAL,
    last_error   TEXT,
    UNIQUE (hostname, transport, endpoint_ip)
);
CREATE INDEX idx_endpoints_host ON endpoints(hostname, transport, last_seen_at DESC);
CREATE INDEX idx_endpoints_ip ON endpoints(endpoint_ip);
```

### 3.4 IP d’egress

```sql
CREATE TABLE egress_ips (
    ip            TEXT PRIMARY KEY,
    asn           TEXT,
    country       TEXT,
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    last_ok_at    REAL,
    last_429_at   REAL,
    reset_at      REAL,
    successes     INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0,
    latency_ms    REAL,
    throughput_tps REAL,
    score_ip      REAL NOT NULL DEFAULT 0,
    state         TEXT NOT NULL CHECK (state IN
        ('fresh','candidate','assigned','draining','exhausted','error','retired')),
    CHECK (ip <> '')
);
CREATE INDEX idx_egress_available ON egress_ips(state, reset_at, score_ip DESC);
CREATE INDEX idx_egress_country ON egress_ips(country, state, score_ip DESC);
CREATE INDEX idx_egress_reset ON egress_ips(reset_at) WHERE reset_at IS NOT NULL;
```

### 3.5 Mapping IP ↔ serveur

```sql
CREATE TABLE ip_server_map (
    ip          TEXT NOT NULL REFERENCES egress_ips(ip) ON DELETE CASCADE,
    hostname    TEXT NOT NULL REFERENCES servers(hostname) ON DELETE CASCADE,
    transport   TEXT NOT NULL CHECK (transport IN ('wireguard','openvpn-udp','openvpn-tcp')),
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    successes   INTEGER NOT NULL DEFAULT 0,
    failures    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ip, hostname, transport)
);
CREATE INDEX idx_ip_server_host ON ip_server_map(hostname, transport, last_seen_at DESC);
```

### 3.6 Observations (append-only)

```sql
CREATE TABLE observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station       INTEGER,
    hostname      TEXT NOT NULL,
    transport     TEXT NOT NULL CHECK (transport IN ('wireguard','openvpn-udp','openvpn-tcp')),
    ip            TEXT,
    model         TEXT,
    status        INTEGER NOT NULL,        -- 0, 200, 429, 5xx
    valid         INTEGER NOT NULL DEFAULT 1,
    validation    TEXT,
    retry_after_s REAL NOT NULL DEFAULT 0,
    reset_at      REAL,
    latency_ms    REAL,
    tokens_out    INTEGER,
    error         TEXT,
    tested_at     REAL NOT NULL
);
CREATE INDEX idx_obs_host_time ON observations(hostname, tested_at DESC);
CREATE INDEX idx_obs_ip_time ON observations(ip, tested_at DESC);
CREATE INDEX idx_obs_station_time ON observations(station, tested_at DESC);
CREATE INDEX idx_obs_status_time ON observations(status, tested_at DESC);
```

### 3.7 Quota

```sql
CREATE TABLE quota_buckets (
    ip        TEXT NOT NULL REFERENCES egress_ips(ip) ON DELETE CASCADE,
    model     TEXT NOT NULL DEFAULT 'default',
    day       TEXT NOT NULL,               -- YYYYMMDD UTC
    bucket    TEXT NOT NULL DEFAULT 'default',
    used      INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    failures  INTEGER NOT NULL DEFAULT 0,
    last_status INTEGER,
    last_error TEXT,
    reset_at  REAL,
    source    TEXT NOT NULL DEFAULT 'observed',
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    PRIMARY KEY (ip, model, day, bucket)
);
CREATE INDEX idx_quota_reset ON quota_buckets(reset_at) WHERE reset_at IS NOT NULL;
CREATE INDEX idx_quota_ip_day ON quota_buckets(ip, day);
```

### 3.8 Leases et assignations

```sql
CREATE TABLE leases (
    lease_id    TEXT PRIMARY KEY,
    station     INTEGER NOT NULL,
    ip          TEXT NOT NULL REFERENCES egress_ips(ip),
    hostname    TEXT NOT NULL,
    transport   TEXT NOT NULL,
    model       TEXT,
    started_at  REAL NOT NULL,
    renewed_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    released_at REAL,
    reason      TEXT,
    status      TEXT NOT NULL CHECK (status IN ('active','released','expired','abandoned'))
);
CREATE UNIQUE INDEX idx_one_active_lease_per_station
    ON leases(station) WHERE status = 'active';
CREATE UNIQUE INDEX idx_one_active_lease_per_ip
    ON leases(ip) WHERE status = 'active';
CREATE INDEX idx_lease_expiry ON leases(expires_at) WHERE status = 'active';
```

### 3.9 Pool chaud et balayage

```sql
CREATE TABLE warm_pool (
    ip          TEXT PRIMARY KEY REFERENCES egress_ips(ip) ON DELETE CASCADE,
    hostname    TEXT NOT NULL,
    transport   TEXT NOT NULL,
    model       TEXT,
    verified_at REAL NOT NULL,
    expires_at  REAL NOT NULL,
    score_ip    REAL NOT NULL DEFAULT 0
);
CREATE INDEX idx_warm_expiry ON warm_pool(expires_at);
CREATE INDEX idx_warm_score ON warm_pool(score_ip DESC);

CREATE TABLE sweep_cursor (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,
    country       TEXT NOT NULL,
    hostname      TEXT NOT NULL,
    transport     TEXT NOT NULL,
    last_tested_at REAL,
    next_eligible_at REAL NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0,
    last_status   TEXT,
    UNIQUE (batch_id, country, hostname, transport)
);
CREATE INDEX idx_sweep_next ON sweep_cursor(batch_id, next_eligible_at);
```

### 3.10 Jobs, fallbacks, incidents

```sql
CREATE TABLE repair_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    station     INTEGER NOT NULL,
    cause       TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    candidates_tried INTEGER NOT NULL DEFAULT 0,
    transports_tried INTEGER NOT NULL DEFAULT 0,
    result      TEXT,
    last_error  TEXT
);
CREATE INDEX idx_repair_station ON repair_jobs(station, started_at DESC);
CREATE INDEX idx_repair_open ON repair_jobs(station) WHERE finished_at IS NULL;

CREATE TABLE fallback_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dependency  TEXT NOT NULL,             -- dns | ip_echo | catalog | manifest | bind | transport | supervisor
    from_source TEXT,
    to_source   TEXT,
    reason      TEXT,
    station     INTEGER,
    started_at  REAL NOT NULL,
    duration_ms REAL,
    success     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_fallback_dep ON fallback_events(dependency, started_at DESC);
CREATE INDEX idx_fallback_station ON fallback_events(station, started_at DESC);

CREATE TABLE incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,             -- station | fleet | dependency
    station     INTEGER,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    cause       TEXT NOT NULL,
    remediation TEXT,
    details     TEXT
);
CREATE INDEX idx_incidents_time ON incidents(started_at DESC);
```

### 3.11 État des stations et config

```sql
CREATE TABLE station_state (
    station          INTEGER PRIMARY KEY,
    container        TEXT NOT NULL,
    transport_effective TEXT,
    state            TEXT NOT NULL CHECK (state IN
        ('BOOT','CONNECTING','VERIFYING','USABLE','DRAINING','REPAIRING','STANDBY','BROKEN')),
    country          TEXT,
    current_ip       TEXT,
    current_host     TEXT,
    client_usable    INTEGER NOT NULL DEFAULT 0,
    last_probe_at    REAL,
    last_200_at      REAL,
    reason           TEXT,
    updated_at       REAL NOT NULL
);

CREATE TABLE config_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    source      TEXT NOT NULL,             -- gui | file | boot
    payload     TEXT NOT NULL,             -- YAML/JSON normalisé
    checksum    TEXT NOT NULL
);
CREATE INDEX idx_config_time ON config_snapshots(created_at DESC);
```

### 3.12 Scores matérialisés

Les scores ne sont pas recalculés à chaque requête. Un worker les met à jour
toutes les 30–60 s à partir de `observations` :

```sql
CREATE TABLE score_snapshots (
    kind        TEXT NOT NULL CHECK (kind IN ('ip','host')),
    key         TEXT NOT NULL,             -- ip ou hostname
    transport   TEXT NOT NULL DEFAULT '',  -- '' = tous transports
    score       REAL NOT NULL,
    success_rate REAL,
    latency_p50 REAL,
    throughput_tps REAL,
    stability   REAL,
    freshness   REAL,
    samples     INTEGER NOT NULL,
    computed_at REAL NOT NULL,
    PRIMARY KEY (kind, key, transport)
);
```

---

## 4. `telemetry.db`

Séparé pour la rétention et la performance :

```sql
CREATE TABLE requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    station     INTEGER,
    model       TEXT,
    hostname    TEXT,
    transport   TEXT,
    ip          TEXT,
    status      INTEGER,
    success     INTEGER,
    duration_ms REAL,
    ttfb_ms     REAL,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    error       TEXT,
    timestamp   REAL NOT NULL
);
CREATE INDEX idx_req_time ON requests(timestamp DESC);
CREATE INDEX idx_req_host ON requests(hostname, timestamp DESC);
CREATE INDEX idx_req_ip ON requests(ip, timestamp DESC);

CREATE TABLE request_aggregates (
    day         TEXT NOT NULL,
    model       TEXT NOT NULL,
    ip          TEXT NOT NULL,
    requests    INTEGER NOT NULL,
    successes   INTEGER NOT NULL,
    p50_ms      REAL,
    p95_ms      REAL,
    tokens_out  INTEGER,
    PRIMARY KEY (day, model, ip)
);
```

Le `requests.db` actuel (3 Go) est migré vers ce schéma, puis purgé selon la
rétention ; les agrégats sont conservés.

---

## 5. Contraintes de qualité

- **0 IP partagée** : index unique partiel sur `leases(ip) WHERE active` et
  sur `station_state` via `current_ip` (vérification applicative + test).
- **0 hostname non traçable** : `observations.hostname` NOT NULL,
  `servers.hostname` PK.
- **0 observation sans IP** : `ip` nullable uniquement pour `ip_unknown`,
  avec un champ `reason` et un retry.
- **Transitions d’état** validées par `states.py` ; la DB ajoute un `CHECK`.
- **Timestamps UTC** (epoch) ; jamais de date locale.
- **Provenance** : `source_id`, `fetched_at`, `stale`.
- **Pas de compteurs globaux non datés** : les scores viennent d’observations
  fenêtrées.

---

## 6. Migrations

1. `schema_migrations` versionne chaque migration.
2. Migrations SQL pures, forward-only, idempotentes.
3. Avant migration : backup `VACUUM INTO` + `integrity_check`.
4. Migration testée sur une copie de la DB de production.
5. Rollback = restauration du backup, pas de down-migration destructive.
6. Les anciennes tables (`servers`, `observations`, `ip_state`,
   `assignments`, `catalog_claims`) sont migrées :
   - `servers` → `servers` + `server_transports` + `endpoints` ;
   - `observations` → `observations` (ajout transport/model) ;
   - `ip_state` → `egress_ips` + `ip_server_map` ;
   - `assignments` → `leases` ;
   - `catalog_claims` → `servers` + `sweep_cursor` + `leases`.
7. Les lignes `hostname=''` sont archivées dans `legacy_ip_state` puis
   exclues ; jamais supprimées sans backup.

---

## 7. Rétention et maintenance

| Donnée | Rétention | Agrégat conservé |
|---|---|---|
| `observations` | 30 jours | `score_snapshots`, `request_aggregates` |
| `telemetry.requests` | 7–30 jours | `request_aggregates` |
| `fallback_events` | 30 jours | compteurs |
| `incidents` | 90 jours | — |
| `repair_jobs` | 30 jours | compteurs |
| `config_snapshots` | 90 jours | checksum |
| backups | 7 jours | — |

Tâches quotidiennes :

- `PRAGMA wal_checkpoint(TRUNCATE)` ;
- `PRAGMA integrity_check` ;
- purge selon rétention ;
- `VACUUM INTO` backup ;
- vérification de l’espace disque.

---

## 8. Capacité estimée

- `servers` : 10 000 lignes × ~200 octets ≈ 2 Mo.
- `server_transports` : 30 000 × ~100 octets ≈ 3 Mo.
- `endpoints` : 30 000 × ~100 octets ≈ 3 Mo.
- `egress_ips` : 100 000 × ~200 octets ≈ 20 Mo.
- `observations` : 500 k/mois × ~200 octets ≈ 100 Mo (30 j).
- `telemetry.requests` : 10 M/mois × ~150 octets ≈ 1,5 Go (7 j) ; séparé.
- `registry.db` cible : **< 250 Mo** avec 30 j d’observations.

---

## 9. Requêtes clés

- Meilleure IP disponible :
  `SELECT ip, score_ip FROM egress_ips WHERE state IN ('fresh','candidate')
   AND (reset_at IS NULL OR reset_at <= :now)
   AND ip NOT IN (SELECT ip FROM leases WHERE status='active')
   ORDER BY score_ip DESC LIMIT 1;`
- Prochain hostname de balayage :
  `SELECT hostname, transport FROM sweep_cursor WHERE next_eligible_at <= :now
   ORDER BY country, next_eligible_at LIMIT 1;`
- Taux de 200 récent :
  `SELECT AVG(CASE WHEN status=200 THEN 1.0 ELSE 0 END) FROM observations
   WHERE ip=:ip AND tested_at >= :cutoff;`
- Reset à venir :
  `SELECT MIN(reset_at) FROM quota_buckets WHERE reset_at > :now;`

---

## 10. Ce que la base ne doit pas devenir

- Un dump de JSON par ligne.
- Un journal de logs géant (`debug.log`).
- Un cache de secrets.
- Une source de vérité pour l’état runtime Docker (le container reste
  l’autorité pour `running`, la DB pour les décisions).
- Une base unique de 3 Go mélangeant télémétrie et décisions.
