-- Target registry schema for the rotation refactor.
-- SQLite, WAL, foreign keys ON (set by the connection helper).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  REAL NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_sources (
    source_id    TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    fetched_at   REAL,
    etag         TEXT,
    stale        INTEGER NOT NULL DEFAULT 0,
    server_count INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    last_ok_at   REAL
);

CREATE TABLE IF NOT EXISTS servers (
    hostname         TEXT PRIMARY KEY,
    country          TEXT NOT NULL,
    city             TEXT,
    region           TEXT,
    load             REAL NOT NULL DEFAULT 0,
    status           TEXT,
    source_id        TEXT NOT NULL REFERENCES catalog_sources(source_id),
    api_seen_at      REAL,
    manifest_seen_at REAL,
    first_seen_at    REAL NOT NULL,
    last_seen_at     REAL NOT NULL,
    stale            INTEGER NOT NULL DEFAULT 0,
    score_host       REAL NOT NULL DEFAULT 0,
    CHECK (hostname <> ''),
    CHECK (country <> '')
);
CREATE INDEX IF NOT EXISTS idx_servers_country
    ON servers(country, stale, score_host DESC);
CREATE INDEX IF NOT EXISTS idx_servers_source
    ON servers(source_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_servers_score
    ON servers(score_host DESC, stale);

CREATE TABLE IF NOT EXISTS server_transports (
    hostname   TEXT NOT NULL REFERENCES servers(hostname) ON DELETE CASCADE,
    transport  TEXT NOT NULL CHECK (
        transport IN ('wireguard', 'openvpn-udp', 'openvpn-tcp')
    ),
    supported  INTEGER NOT NULL DEFAULT 0,
    wg_pubkey  TEXT,
    last_ok_at REAL,
    last_error TEXT,
    PRIMARY KEY (hostname, transport)
);
CREATE INDEX IF NOT EXISTS idx_transport_supported
    ON server_transports(transport, supported, hostname);

CREATE TABLE IF NOT EXISTS endpoints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname      TEXT NOT NULL REFERENCES servers(hostname) ON DELETE CASCADE,
    transport     TEXT NOT NULL CHECK (
        transport IN ('wireguard', 'openvpn-udp', 'openvpn-tcp')
    ),
    endpoint_ip   TEXT NOT NULL,
    port          INTEGER,
    source        TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    last_rtt_ms   REAL,
    last_ok_at    REAL,
    last_error    TEXT,
    UNIQUE (hostname, transport, endpoint_ip)
);
CREATE INDEX IF NOT EXISTS idx_endpoints_host
    ON endpoints(hostname, transport, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_endpoints_ip ON endpoints(endpoint_ip);

CREATE TABLE IF NOT EXISTS egress_ips (
    ip             TEXT PRIMARY KEY,
    provider       TEXT NOT NULL DEFAULT 'warp',
    block_key      TEXT,
    quota_axis     TEXT,
    asn            TEXT,
    country        TEXT,
    first_seen_at  REAL NOT NULL,
    last_seen_at   REAL NOT NULL,
    last_ok_at     REAL,
    last_429_at    REAL,
    reset_at       REAL,
    successes      INTEGER NOT NULL DEFAULT 0,
    failures       INTEGER NOT NULL DEFAULT 0,
    latency_ms     REAL,
    throughput_tps REAL,
    score_ip       REAL NOT NULL DEFAULT 0,
    state          TEXT NOT NULL CHECK (state IN (
        'fresh', 'candidate', 'assigned', 'draining',
        'exhausted', 'error', 'retired'
    )),
    CHECK (ip <> '')
);
CREATE INDEX IF NOT EXISTS idx_egress_available
    ON egress_ips(state, reset_at, score_ip DESC);
CREATE INDEX IF NOT EXISTS idx_egress_country
    ON egress_ips(country, state, score_ip DESC);
CREATE INDEX IF NOT EXISTS idx_egress_reset
    ON egress_ips(reset_at) WHERE reset_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS ip_server_map (
    ip            TEXT NOT NULL REFERENCES egress_ips(ip) ON DELETE CASCADE,
    hostname      TEXT NOT NULL REFERENCES servers(hostname) ON DELETE CASCADE,
    transport     TEXT NOT NULL CHECK (
        transport IN ('wireguard', 'openvpn-udp', 'openvpn-tcp')
    ),
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    successes     INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ip, hostname, transport)
);
CREATE INDEX IF NOT EXISTS idx_ip_server_host
    ON ip_server_map(hostname, transport, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station       INTEGER,
    hostname      TEXT NOT NULL,
    transport     TEXT NOT NULL CHECK (
        transport IN ('wireguard', 'openvpn-udp', 'openvpn-tcp')
    ),
    ip            TEXT,
    provider      TEXT NOT NULL DEFAULT '',
    model         TEXT,
    status        INTEGER NOT NULL,
    valid         INTEGER NOT NULL DEFAULT 1,
    validation    TEXT,
    retry_after_s REAL NOT NULL DEFAULT 0,
    reset_at      REAL,
    latency_ms    REAL,
    tokens_out    INTEGER,
    error         TEXT,
    tested_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_host_time
    ON observations(hostname, tested_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_ip_time
    ON observations(ip, tested_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_station_time
    ON observations(station, tested_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_status_time
    ON observations(status, tested_at DESC);

CREATE TABLE IF NOT EXISTS quota_buckets (
    ip            TEXT NOT NULL REFERENCES egress_ips(ip) ON DELETE CASCADE,
    provider      TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT 'default',
    day           TEXT NOT NULL,
    bucket        TEXT NOT NULL DEFAULT 'default',
    used          INTEGER NOT NULL DEFAULT 0,
    successes     INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0,
    last_status   INTEGER,
    last_error    TEXT,
    reset_at      REAL,
    source        TEXT NOT NULL DEFAULT 'observed',
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    PRIMARY KEY (ip, model, day, bucket)
);
CREATE INDEX IF NOT EXISTS idx_quota_reset
    ON quota_buckets(reset_at) WHERE reset_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_quota_ip_day ON quota_buckets(ip, day);

CREATE TABLE IF NOT EXISTS leases (
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
    status      TEXT NOT NULL CHECK (
        status IN ('active', 'released', 'expired', 'abandoned')
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_lease_per_station
    ON leases(station) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_lease_per_ip
    ON leases(ip) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_lease_expiry
    ON leases(expires_at) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS warm_pool (
    ip          TEXT PRIMARY KEY REFERENCES egress_ips(ip) ON DELETE CASCADE,
    hostname    TEXT NOT NULL,
    transport   TEXT NOT NULL,
    model       TEXT,
    verified_at REAL NOT NULL,
    expires_at  REAL NOT NULL,
    score_ip    REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_warm_expiry ON warm_pool(expires_at);
CREATE INDEX IF NOT EXISTS idx_warm_score ON warm_pool(score_ip DESC);

CREATE TABLE IF NOT EXISTS sweep_cursor (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         TEXT NOT NULL,
    country          TEXT NOT NULL,
    hostname         TEXT NOT NULL,
    transport        TEXT NOT NULL,
    last_tested_at   REAL,
    next_eligible_at REAL NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    failures         INTEGER NOT NULL DEFAULT 0,
    last_status      TEXT,
    UNIQUE (batch_id, country, hostname, transport)
);
CREATE INDEX IF NOT EXISTS idx_sweep_next
    ON sweep_cursor(batch_id, next_eligible_at);

CREATE TABLE IF NOT EXISTS repair_jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    station          INTEGER NOT NULL,
    cause            TEXT NOT NULL,
    started_at       REAL NOT NULL,
    finished_at      REAL,
    candidates_tried INTEGER NOT NULL DEFAULT 0,
    transports_tried INTEGER NOT NULL DEFAULT 0,
    result           TEXT,
    last_error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_repair_station
    ON repair_jobs(station, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_repair_open
    ON repair_jobs(station) WHERE finished_at IS NULL;

CREATE TABLE IF NOT EXISTS repair_requests (
    station      INTEGER PRIMARY KEY,
    reason       TEXT NOT NULL,
    model        TEXT,
    ip           TEXT,
    hostname     TEXT,
    requested_at REAL NOT NULL,
    expires_at   REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    claimed_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_repair_pending
    ON repair_requests(status, expires_at);

CREATE TABLE IF NOT EXISTS fallback_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dependency  TEXT NOT NULL,
    from_source TEXT,
    to_source   TEXT,
    reason      TEXT,
    station     INTEGER,
    started_at  REAL NOT NULL,
    duration_ms REAL,
    success     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fallback_dep
    ON fallback_events(dependency, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_fallback_station
    ON fallback_events(station, started_at DESC);

CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,
    station     INTEGER,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    cause       TEXT NOT NULL,
    remediation TEXT,
    details     TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_time ON incidents(started_at DESC);

CREATE TABLE IF NOT EXISTS station_state (
    station             INTEGER PRIMARY KEY,
    container           TEXT NOT NULL,
    transport_effective TEXT,
    state               TEXT NOT NULL CHECK (state IN (
        'BOOT', 'CONNECTING', 'VERIFYING', 'USABLE', 'DRAINING',
        'REPAIRING', 'STANDBY', 'BROKEN'
    )),
    country             TEXT,
    current_ip          TEXT,
    current_host        TEXT,
    client_usable       INTEGER NOT NULL DEFAULT 0,
    last_probe_at       REAL,
    last_200_at         REAL,
    reason              TEXT,
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS config_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    source     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    checksum   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_time
    ON config_snapshots(created_at DESC);

CREATE TABLE IF NOT EXISTS score_snapshots (
    kind           TEXT NOT NULL CHECK (kind IN ('ip', 'host')),
    key            TEXT NOT NULL,
    transport      TEXT NOT NULL DEFAULT '',
    score          REAL NOT NULL,
    success_rate   REAL,
    latency_p50    REAL,
    throughput_tps REAL,
    stability      REAL,
    freshness      REAL,
    samples        INTEGER NOT NULL,
    computed_at    REAL NOT NULL,
    PRIMARY KEY (kind, key, transport)
);

-- Per-chain resume state (index, winner, last results). Replaces the
-- fresh_chain_state_*.json files so registry.db is the only runtime source.
CREATE TABLE IF NOT EXISTS chain_state (
    namespace  TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Scan audit trail (free.audit.ScanAudit). Kept next to the observations
-- so a scan can be replayed without a second database file.
CREATE TABLE IF NOT EXISTS scan_runs (
    run_id      TEXT PRIMARY KEY,
    station     INTEGER NOT NULL,
    transport   TEXT,
    reason      TEXT,
    candidates  INTEGER NOT NULL DEFAULT 0,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    outcome     TEXT,
    winner_host TEXT,
    winner_ip   TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_runs_open
    ON scan_runs(ended_at, started_at);

CREATE TABLE IF NOT EXISTS scan_attempts (
    attempt_id  TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    station     INTEGER NOT NULL,
    hostname    TEXT NOT NULL,
    country     TEXT,
    transport   TEXT,
    started_at  REAL NOT NULL,
    finished_at REAL,
    status      TEXT,
    egress_ip   TEXT,
    valid       INTEGER,
    validation  TEXT,
    fingerprint TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_attempts_run
    ON scan_attempts(run_id);
