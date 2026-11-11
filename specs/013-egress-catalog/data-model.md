# Data Model — Catalogue d'egress

**Feature**: 013-egress-catalog

## 1. Vue d'ensemble

```text
SourceDefinition 1 ──< SourceOption >── 1 EgressOption 1 ──< Observation
                                              │
                                              ├──< RotationSlot
                                              └──< ProviderState (agrégat)

CatalogRun 1 ──< RunSourceStat
```

- Une **source** peut fournir plusieurs options.
- Une **option** peut être fournie par plusieurs sources (dédup).
- Une **option** a un historique borné d'observations.
- Un **slot** pointe l'option active d'un provider à un instant donné.
- Un **run** raconte un cycle de découverte/validation.

## 2. Tables `registry.db`

### `egress_sources`

```sql
CREATE TABLE IF NOT EXISTS egress_sources (
  id             TEXT PRIMARY KEY,
  type           TEXT NOT NULL,          -- public_text|public_json|geonode_json|warp|nordvpn
  url            TEXT NOT NULL DEFAULT '',
  scheme         TEXT NOT NULL DEFAULT '',
  enabled        INTEGER NOT NULL DEFAULT 1,
  interval_s     INTEGER NOT NULL DEFAULT 21600,
  config_json    TEXT NOT NULL DEFAULT '{}',
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  last_run_at    INTEGER,
  last_error     TEXT
);
```

### `egress_options`

```sql
CREATE TABLE IF NOT EXISTS egress_options (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  key            TEXT NOT NULL UNIQUE,   -- provider|kind|host:port|egress_ip
  provider       TEXT NOT NULL,          -- public|warp|nordvpn
  kind           TEXT NOT NULL,          -- https|socks5|warp_container|nordvpn_station
  host           TEXT,
  port           INTEGER,
  station        TEXT,                   -- container/station name
  egress_ip      TEXT,
  block          TEXT,                   -- /64, /32 or host
  status         TEXT NOT NULL,          -- candidate|reachable|usable|quota|auth|dead|expired
  first_seen_at  INTEGER NOT NULL,
  last_seen_at   INTEGER NOT NULL,
  last_checked_at INTEGER,
  expires_at     INTEGER NOT NULL DEFAULT 0,
  reset_at       INTEGER NOT NULL DEFAULT 0,
  success_count  INTEGER NOT NULL DEFAULT 0,
  fail_count     INTEGER NOT NULL DEFAULT 0,
  last_status    INTEGER,
  last_latency_ms REAL,
  score          REAL NOT NULL DEFAULT 0,
  source_ids     TEXT NOT NULL DEFAULT '[]',
  meta_json      TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_egress_options_provider_status
  ON egress_options(provider, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_egress_options_egress
  ON egress_options(provider, egress_ip) WHERE egress_ip IS NOT NULL AND egress_ip <> '';
```

### `egress_observations`

Append-only, rétention 7 jours par défaut.

```sql
CREATE TABLE IF NOT EXISTS egress_observations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  option_id   INTEGER NOT NULL REFERENCES egress_options(id) ON DELETE CASCADE,
  observed_at INTEGER NOT NULL,
  phase       TEXT NOT NULL,   -- liveness|free_gate|auth|egress
  status       INTEGER NOT NULL DEFAULT 0,
  result      TEXT NOT NULL,   -- ok|quota|auth|dead|duplicate
  latency_ms  REAL,
  message     TEXT NOT NULL DEFAULT '',
  run_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_egress_obs_option_time
  ON egress_observations(option_id, observed_at DESC);
```

### `egress_catalog_meta`

Petit magasin clé/valeur pour le curseur de découverte public (un run
one-shot reprend après le dernier lot testé, pas au premier candidat).

```sql
CREATE TABLE IF NOT EXISTS egress_catalog_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

### `egress_threat_intel` / `egress_threat_feeds`

Indicateurs (IP exactes et CIDR) issus de flux gratuits. Un flux en panne
garde ses anciennes lignes ; un flux retiré de la config est purgé.

```sql
CREATE TABLE egress_threat_intel (
  indicator TEXT NOT NULL,
  kind      TEXT NOT NULL,          -- ip|cidr
  source    TEXT NOT NULL,
  last_seen REAL NOT NULL,
  PRIMARY KEY (indicator, source)
);
CREATE TABLE egress_threat_feeds (
  source     TEXT PRIMARY KEY,
  url        TEXT NOT NULL,
  fetched_at REAL NOT NULL,
  count      INTEGER NOT NULL DEFAULT 0,
  error      TEXT NOT NULL DEFAULT ''
);
```

### `egress_rotation_slots`

```sql
CREATE TABLE IF NOT EXISTS egress_rotation_slots (
  provider     TEXT PRIMARY KEY,   -- public|warp|nordvpn
  option_id    INTEGER REFERENCES egress_options(id),
  selected_at  INTEGER NOT NULL,
  reason       TEXT NOT NULL DEFAULT '',
  expires_at   INTEGER NOT NULL DEFAULT 0
);
```

### `catalog_runs`

```sql
CREATE TABLE IF NOT EXISTS catalog_runs (
  id           TEXT PRIMARY KEY,
  provider     TEXT NOT NULL,
  started_at   INTEGER NOT NULL,
  finished_at  INTEGER,
  status       TEXT NOT NULL DEFAULT 'running', -- running|ok|partial|failed
  candidates   INTEGER NOT NULL DEFAULT 0,
  promoted     INTEGER NOT NULL DEFAULT 0,
  quarantined  INTEGER NOT NULL DEFAULT 0,
  dead         INTEGER NOT NULL DEFAULT 0,
  errors_json  TEXT NOT NULL DEFAULT '[]'
);
```

### `provider_state` (matérialisé, reconstructible)

```sql
CREATE TABLE IF NOT EXISTS egress_provider_state (
  provider        TEXT PRIMARY KEY,
  known           INTEGER NOT NULL DEFAULT 0,
  reachable       INTEGER NOT NULL DEFAULT 0,
  usable          INTEGER NOT NULL DEFAULT 0,
  quota           INTEGER NOT NULL DEFAULT 0,
  auth            INTEGER NOT NULL DEFAULT 0,
  dead            INTEGER NOT NULL DEFAULT 0,
  unique_blocks   INTEGER NOT NULL DEFAULT 0,
  last_run_at     INTEGER,
  last_ok_at      INTEGER,
  updated_at      INTEGER NOT NULL
);
```

## 3. États et transitions

```text
candidate ──liveness ok──> reachable ──free 200──> usable
    │                         │                      │
    │                         ├──free 429──────────> quota ──reset──> candidate
    │                         ├──timeout/refus─────> dead ──TTL──> expired
    │                         └──auth failed───────> auth ──repair──> candidate
    └──invalid/private──────────────────────────────> dead
```

Règles :

- `usable` exige une observation `free_gate` 200 dans le TTL (12 h par
  défaut).
- `quota` interdit toute sélection avant `reset_at` (barrière dure).
- `auth` interdit la sélection jusqu'à une nouvelle preuve de session.
- `dead` a un TTL court (7 j) et ne peut pas être promu sans une nouvelle
  observation.
- `expired` est un état calculé à la lecture ; le stockage peut rester
  `usable` avec `expires_at` dépassé, mais la sélection le rejette.

## 4. Clés canoniques

| Provider | `key` | `block` |
|---|---|---|
| public | `public|https|host:port|egress_ip` | IP ou /24 |
| warp | `warp|warp_container|container|block` | `/64` |
| nordvpn | `nordvpn|nordvpn_station|station|egress_ip` | IP ou /24 |

Deux options avec le même `(provider, egress_ip)` sont fusionnées. Pour WARP,
le `/64` est l'unité ; plusieurs IPs dans le même `/64` ne créent qu'une
option.

## 5. Rétention

- `egress_observations` : 7 jours (configurable), purge quotidienne.
- `egress_options` : `dead` 30 jours, `expired` 30 jours, `usable` conservé
  tant qu'il est rafraîchi.
- `catalog_runs` : 30 jours.
- `egress_provider_state` : reconstructible, pas de rétention.
- Export JSON : `logs/egress_catalog.json`, atomique, jamais lu en écriture.

## 6. Migration

- `PRAGMA user_version` / table `schema_migrations`.
- Migration `013` idempotente (`IF NOT EXISTS`), testée sur copie de
  `registry.db`.
- Aucune suppression de table existante ; le catalogue vit à côté.
- Backup `VACUUM INTO` avant migration (constitution XIII).
