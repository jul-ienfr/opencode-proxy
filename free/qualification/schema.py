"""Schema SQL du store de qualification.

[2026-11-20] Extrait de `store.py` : le bloc faisait 239 lignes et poussait
le module a 1 535, au-dessus de la limite constitutionnelle XIV (1 500).
Le contenu est INCHANGE — seul le domicile change. Voir aussi
`rotation/infrastructure/db/schema.sql` (schema canonique du registre) :
les DDL de `servers` et `server_transports` doivent y rester ALIGNEES.
"""

from __future__ import annotations

from free.sql_schemas import PINS_SCHEMA, SERVERS_SCHEMA, SLO_SAMPLES_SCHEMA

SCHEMA_SQL = SERVERS_SCHEMA + """
CREATE TABLE IF NOT EXISTS qualification_candidates (
    hostname            TEXT PRIMARY KEY,
    provider            TEXT NOT NULL DEFAULT 'nordvpn',
    country             TEXT NOT NULL DEFAULT '',
    city                TEXT NOT NULL DEFAULT '',
    load                REAL NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'pending',
    transports          TEXT NOT NULL DEFAULT '[]',
    transport_cursor    TEXT NOT NULL DEFAULT '',
    transport_round     INTEGER NOT NULL DEFAULT 0,
    potential_score     REAL NOT NULL DEFAULT 0,
    measured_score      REAL NOT NULL DEFAULT 0,
    rtt_ms              REAL,
    quota_remaining     INTEGER,
    quota_reset_at      REAL,
    attempts            INTEGER NOT NULL DEFAULT 0,
    infra_failures      INTEGER NOT NULL DEFAULT 0,
    success_count       INTEGER NOT NULL DEFAULT 0,
    fail_count          INTEGER NOT NULL DEFAULT 0,
    last_attempt_at     REAL,
    last_result         TEXT NOT NULL DEFAULT '',
    last_egress_ip      TEXT NOT NULL DEFAULT '',
    last_transport      TEXT NOT NULL DEFAULT '',
    next_eligible_at    REAL NOT NULL DEFAULT 0,
    epoch_id            TEXT NOT NULL DEFAULT '',
    lease_until         REAL NOT NULL DEFAULT 0,
    skipped_reason      TEXT NOT NULL DEFAULT '',
    source_id           TEXT NOT NULL DEFAULT '',
    first_seen_at       REAL NOT NULL,
    last_seen_at        REAL NOT NULL,
    updated_at          REAL NOT NULL,
    meta_json           TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_qual_order
    ON qualification_candidates(status, next_eligible_at, potential_score DESC);
CREATE INDEX IF NOT EXISTS idx_qual_country
    ON qualification_candidates(country, status);
CREATE INDEX IF NOT EXISTS idx_qual_epoch
    ON qualification_candidates(epoch_id, status);

CREATE TABLE IF NOT EXISTS qualification_attempts (
    attempt_id      TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL DEFAULT '',
    epoch_id        TEXT NOT NULL DEFAULT '',
    station         INTEGER NOT NULL DEFAULT 0,
    hostname        TEXT NOT NULL,
    transport       TEXT NOT NULL DEFAULT '',
    round           INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    egress_ip       TEXT NOT NULL DEFAULT '',
    latency_ms      REAL NOT NULL DEFAULT 0,
    rtt_ms          REAL NOT NULL DEFAULT 0,
    dns_ms          REAL NOT NULL DEFAULT 0,
    connect_ms      REAL NOT NULL DEFAULT 0,
    handshake_ms    REAL NOT NULL DEFAULT 0,
    endpoint_ip     TEXT NOT NULL DEFAULT '',
    reset_at        REAL NOT NULL DEFAULT 0,
    retry_after_s   REAL NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    validation      TEXT NOT NULL DEFAULT '',
    started_at      REAL NOT NULL,
    finished_at     REAL NOT NULL,
    provider        TEXT NOT NULL DEFAULT 'nordvpn'
);

CREATE INDEX IF NOT EXISTS idx_attempt_host
    ON qualification_attempts(hostname, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempt_epoch
    ON qualification_attempts(epoch_id, status);
CREATE INDEX IF NOT EXISTS idx_attempt_station
    ON qualification_attempts(station, started_at DESC);

CREATE TABLE IF NOT EXISTS qualification_epochs (
    epoch_id        TEXT PRIMARY KEY,
    provider        TEXT NOT NULL DEFAULT 'nordvpn',
    status          TEXT NOT NULL DEFAULT 'running',
    total           INTEGER NOT NULL DEFAULT 0,
    tested          INTEGER NOT NULL DEFAULT 0,
    usable          INTEGER NOT NULL DEFAULT 0,
    quota           INTEGER NOT NULL DEFAULT 0,
    infra           INTEGER NOT NULL DEFAULT 0,
    dead            INTEGER NOT NULL DEFAULT 0,
    skipped         INTEGER NOT NULL DEFAULT 0,
    cursor          TEXT NOT NULL DEFAULT '',
    started_at      REAL NOT NULL,
    finished_at     REAL,
    reason          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_epoch_status
    ON qualification_epochs(status, started_at DESC);

-- [P0 2026-11-20] DDL ALIGNEE sur `rotation/infrastructure/db/schema.sql`.
-- Avant : ni cle etrangere ni CHECK, alors que `schema.sql` les porte — selon
-- l'ORDRE D'INITIALISATION la table existait avec ou sans contraintes
-- (`IF NOT EXISTS` = divergence silencieuse). Mesure : **15 179 violations**
-- (`server_transports.hostname -> servers.hostname`) dues a
-- `upsert_server_transports` qui n'ecrivait pas la ligne parente.
CREATE TABLE IF NOT EXISTS server_transports (
    hostname TEXT NOT NULL REFERENCES servers(hostname) ON DELETE CASCADE,
    transport TEXT NOT NULL CHECK (transport IN ('wireguard', 'openvpn-udp', 'openvpn-tcp')),
    supported INTEGER NOT NULL DEFAULT 1, wg_pubkey TEXT,
    last_ok_at REAL, last_error TEXT, PRIMARY KEY (hostname, transport)
);

CREATE INDEX IF NOT EXISTS idx_transport_supported
    ON server_transports(supported);

CREATE TABLE IF NOT EXISTS transport_health (
    transport       TEXT NOT NULL,
    station         INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    successes       INTEGER NOT NULL DEFAULT 0,
    quota           INTEGER NOT NULL DEFAULT 0,
    infra           INTEGER NOT NULL DEFAULT 0,
    latency_p50_ms  REAL,
    success_rate    REAL NOT NULL DEFAULT 0,
    degraded        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT NOT NULL DEFAULT '',
    updated_at      REAL NOT NULL,
    PRIMARY KEY (transport, station)
);

CREATE INDEX IF NOT EXISTS idx_transport_health_degraded
    ON transport_health(degraded, transport);

CREATE TABLE IF NOT EXISTS discovery_runs (
    id                  TEXT PRIMARY KEY,
    source              TEXT NOT NULL DEFAULT '',
    started_at          REAL NOT NULL,
    finished_at         REAL,
    status              TEXT NOT NULL DEFAULT 'running',
    server_count        INTEGER NOT NULL DEFAULT 0,
    transport_count     INTEGER NOT NULL DEFAULT 0,
    stale               INTEGER NOT NULL DEFAULT 0,
    catalog_fetched_at  REAL,
    error               TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_discovery_started
    ON discovery_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS station_state (
    station             INTEGER PRIMARY KEY,
    container           TEXT NOT NULL DEFAULT '',
    transport_effective TEXT NOT NULL DEFAULT '',
    state               TEXT NOT NULL DEFAULT '',
    country             TEXT NOT NULL DEFAULT '',
    current_ip          TEXT NOT NULL DEFAULT '',
    current_host        TEXT NOT NULL DEFAULT '',
    client_usable       INTEGER NOT NULL DEFAULT 0,
    last_probe_at       REAL NOT NULL DEFAULT 0,
    last_200_at         REAL,
    reason              TEXT NOT NULL DEFAULT '',
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL DEFAULT 'station',
    station     INTEGER,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    cause       TEXT NOT NULL DEFAULT '',
    remediation TEXT NOT NULL DEFAULT '',
    details     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_incidents_open
    ON incidents(ended_at, station, cause);

CREATE TABLE IF NOT EXISTS repair_jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    station          INTEGER NOT NULL,
    cause            TEXT NOT NULL DEFAULT '',
    started_at       REAL NOT NULL,
    finished_at      REAL,
    candidates_tried INTEGER NOT NULL DEFAULT 0,
    transports_tried TEXT NOT NULL DEFAULT '',
    result           TEXT NOT NULL DEFAULT 'running',
    last_error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_repair_jobs_station
    ON repair_jobs(station, started_at DESC);

CREATE TABLE IF NOT EXISTS station_cutovers (
    cutover_id       TEXT PRIMARY KEY,
    station          INTEGER NOT NULL,
    from_option_key  TEXT NOT NULL DEFAULT '',
    from_egress_ip   TEXT NOT NULL DEFAULT '',
    to_option_key    TEXT NOT NULL DEFAULT '',
    to_egress_ip     TEXT NOT NULL DEFAULT '',
    transport        TEXT NOT NULL DEFAULT '',
    started_at       REAL NOT NULL,
    committed_at     REAL,
    finished_at      REAL,
    duration_ms      REAL,
    result           TEXT NOT NULL DEFAULT 'pending',
    reason           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cutover_station
    ON station_cutovers(station, started_at DESC);

CREATE TABLE IF NOT EXISTS station_runtime (
    station             INTEGER PRIMARY KEY,
    state               TEXT NOT NULL DEFAULT 'idle',
    current_option_key  TEXT NOT NULL DEFAULT '',
    current_egress_ip   TEXT NOT NULL DEFAULT '',
    current_transport   TEXT NOT NULL DEFAULT '',
    standby_option_key  TEXT NOT NULL DEFAULT '',
    standby_egress_ip   TEXT NOT NULL DEFAULT '',
    standby_transport   TEXT NOT NULL DEFAULT '',
    lease_until         REAL NOT NULL DEFAULT 0,
    last_cutover_at     REAL,
    cutover_ms          REAL,
    availability_1h     REAL NOT NULL DEFAULT 0,
    availability_24h    REAL NOT NULL DEFAULT 0,
    last_reason         TEXT NOT NULL DEFAULT '',
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS watchdog_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    heartbeat_at REAL NOT NULL DEFAULT 0,
    last_run_at  REAL,
    status       TEXT NOT NULL DEFAULT 'idle',
    error        TEXT NOT NULL DEFAULT ''
);

""" + PINS_SCHEMA + SLO_SAMPLES_SCHEMA
