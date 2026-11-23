"""Canonical SQL DDL constants shared across the free/registry stores.

[024 T310 / INV-1] « Une table, un déclarant ».

Several runtime modules opened the same ``registry.db`` and each declared its
own version of the same table. ``CREATE TABLE IF NOT EXISTS`` made the result
depend on boot order: the first writer won, and the other module's INSERTs
failed on unknown columns. The worst cases were ``servers`` (two different
column sets) and ``ip_state`` (two incompatible schemas: ``egress_ip`` vs
``provider/ip``).

This module is the **single** home of the shared DDL. Stores import the
constant and append it to their own schema; they never re-type the CREATE
statement. The INV-1 gate (``tests/test_inv1_one_declarant.py``) scans every
production ``.py`` for executed ``CREATE TABLE`` constants and fails when a
table is declared by more than one file.

The canonical ``servers`` schema is the union of the qualification store's
columns and the IP registry's columns, with defaults so both stores can insert
their own subset. Existing databases are migrated additively by each store.
"""

from __future__ import annotations

#: Canonical ``servers`` table (union of qualification + ip_registry columns).
SERVERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    hostname           TEXT PRIMARY KEY,
    server_ip          TEXT,
    country            TEXT NOT NULL DEFAULT '',
    city               TEXT NOT NULL DEFAULT '',
    region             TEXT,
    load               REAL NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'pending',
    technologies       TEXT NOT NULL DEFAULT '[]',
    source_id          TEXT NOT NULL DEFAULT '',
    api_seen_at        REAL,
    manifest_seen_at   REAL,
    first_seen_at      REAL NOT NULL DEFAULT 0,
    last_seen_at       REAL NOT NULL DEFAULT 0,
    active             INTEGER NOT NULL DEFAULT 1,
    stale              INTEGER NOT NULL DEFAULT 0,
    catalog_fetched_at REAL,
    score_host         REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_servers_country
    ON servers(country, active);
"""

#: Hostname pins (019 T306). ``registry.db`` is the authority; ``.env`` is an
#: export. One row per station.
PINS_SCHEMA = """
CREATE TABLE IF NOT EXISTS pins (
    station     INTEGER PRIMARY KEY,
    provider    TEXT NOT NULL DEFAULT 'nordvpn',
    hostname    TEXT NOT NULL DEFAULT '',
    country     TEXT NOT NULL DEFAULT '',
    egress_ip   TEXT NOT NULL DEFAULT '',
    transport   TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'env-migration',
    pinned_at   REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pins_host
    ON pins(hostname);
"""

#: Per-minute SLO samples (019 T302/T313).
SLO_SAMPLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS slo_samples (
    sampled_at              REAL PRIMARY KEY,
    stations_client_usable   INTEGER NOT NULL DEFAULT 0,
    warm_pool_live          INTEGER NOT NULL DEFAULT 0,
    ip_200_rate             REAL NOT NULL DEFAULT 0,
    rotate_latency_seconds  REAL,
    app_up                  INTEGER NOT NULL DEFAULT 1,
    code_reload_total       INTEGER NOT NULL DEFAULT 0,
    auth_attempts_total     INTEGER NOT NULL DEFAULT 0,
    incidents_open          INTEGER NOT NULL DEFAULT 0,
    rotation_jobs_orphans   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_slo_samples_time
    ON slo_samples(sampled_at DESC);
"""

#: Shared catalog claims for parallel fresh-IP chains.
CLAIMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    hostname TEXT PRIMARY KEY,
    country TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS claims (
    hostname TEXT PRIMARY KEY,
    station INTEGER NOT NULL,
    status TEXT NOT NULL,
    egress_ip TEXT,
    retry_after REAL NOT NULL DEFAULT 0,
    reset_at REAL NOT NULL DEFAULT 0,
    error TEXT,
    updated_at REAL NOT NULL,
    lease_until REAL NOT NULL,
    fail_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_claims_status
    ON claims(status, reset_at);
CREATE TABLE IF NOT EXISTS rebind_budget (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    window_start REAL NOT NULL,
    count INTEGER NOT NULL
);
"""

__all__ = [
    "CLAIMS_SCHEMA",
    "PINS_SCHEMA",
    "SERVERS_SCHEMA",
    "SLO_SAMPLES_SCHEMA",
]
