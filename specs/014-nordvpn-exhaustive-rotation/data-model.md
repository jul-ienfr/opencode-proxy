# Data Model — qualification exhaustive NordVPN

**Feature** : `014-nordvpn-exhaustive-rotation`
**Autorité** : `logs/registry.db` (constitution III)
**Version de schéma** : à ajouter dans `rotation/infrastructure/db/migrate.py`

---

## 1. Vue d'ensemble

```text
catalog API / cache / embedded
        │
        ▼
qualification_candidates  ──1:N──►  qualification_attempts
        │                                   │
        │                                   ▼
        │                            egress_options (IP observée)
        │                                   │
        ▼                                   ▼
qualification_epochs ◄── sweep_cursor   egress_ips / quota_buckets
```

- **Candidate** : un hostname à qualifier (7 070 lignes).
- **Attempt** : une tentative append-only (200/429/infra/auth/dead/skipped).
- **Option** : une IP observée, unité de quota et de rotation.
- **Epoch** : un passage complet, avec curseur et compteurs.
- **Quota** : ledger par IP, reset fournisseur.

---

## 2. Tables

### 2.1 `qualification_candidates`

Une ligne par hostname. Clé primaire `hostname`.

```sql
CREATE TABLE IF NOT EXISTS qualification_candidates (
    hostname           TEXT PRIMARY KEY,
    provider           TEXT NOT NULL DEFAULT 'nordvpn',
    country            TEXT NOT NULL DEFAULT '',
    city               TEXT NOT NULL DEFAULT '',
    region             TEXT NOT NULL DEFAULT '',
    load               REAL NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'pending',
    transports         TEXT NOT NULL DEFAULT '[]',   -- JSON: ["wireguard","openvpn-udp"]
    source_id          TEXT NOT NULL DEFAULT '',
    stale              INTEGER NOT NULL DEFAULT 0,
    potential_score    REAL NOT NULL DEFAULT 0,
    measured_score     REAL NOT NULL DEFAULT 0,
    rtt_ms             REAL,
    dns_ms             REAL,
    connect_ms         REAL,
    handshake_ms       REAL,
    quota_remaining    INTEGER,
    quota_reset_at     REAL,
    attempts           INTEGER NOT NULL DEFAULT 0,
    infra_failures     INTEGER NOT NULL DEFAULT 0,
    success_count      INTEGER NOT NULL DEFAULT 0,
    fail_count         INTEGER NOT NULL DEFAULT 0,
    last_attempt_at    REAL,
    last_result        TEXT NOT NULL DEFAULT '',
    last_egress_ip     TEXT NOT NULL DEFAULT '',
    transport_cursor   TEXT NOT NULL DEFAULT '',
    transport_round    INTEGER NOT NULL DEFAULT 0,
    attempts_by_transport TEXT NOT NULL DEFAULT '{}',
    last_transport     TEXT NOT NULL DEFAULT '',
    last_transport_result TEXT NOT NULL DEFAULT '',
    next_eligible_at   REAL NOT NULL DEFAULT 0,
    epoch_id           TEXT NOT NULL DEFAULT '',
    skipped_reason     TEXT NOT NULL DEFAULT '',
    first_seen_at      REAL NOT NULL,
    last_seen_at       REAL NOT NULL,
    updated_at         REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_qual_order
    ON qualification_candidates(status, next_eligible_at, potential_score DESC);
CREATE INDEX IF NOT EXISTS idx_qual_country
    ON qualification_candidates(country, status);
CREATE INDEX IF NOT EXISTS idx_qual_transport
    ON qualification_candidates(status, load, rtt_ms);
CREATE INDEX IF NOT EXISTS idx_qual_epoch
    ON qualification_candidates(epoch_id, status);
```

**Statuts** : `pending`, `claimed`, `probing`, `usable`, `quota`, `auth`,
`dead`, `skipped`.

**Règles** :
- `transports` est un JSON trié (`wireguard` d'abord) ; un host sans transport
  connu est `skipped` (`unsupported-transport`).
- `potential_score` est recalculé à chaque ingestion et après chaque tentative.
- `measured_score` n'est renseigné qu'après un 200 ; il ne remplace pas
  `potential_score` dans l'ordre de la file.
- `quota_remaining` est `NULL` si l'IP est inconnue ; `0` si `quota_reset_at`
  est futur.
- `next_eligible_at` est la seule porte temporelle ; `reset_at` de l'IP est
  vérifié séparément dans `egress_ips`/`quota_buckets`.

### 2.2 `qualification_attempts`

Append-only. Une ligne par tentative, jamais mise à jour (sauf `finished_at`
si le process crash, par un job de réconciliation).

```sql
CREATE TABLE IF NOT EXISTS qualification_attempts (
    attempt_id        TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL DEFAULT '',
    epoch_id          TEXT NOT NULL DEFAULT '',
    station           INTEGER NOT NULL DEFAULT 0,
    hostname          TEXT NOT NULL,
    transport         TEXT NOT NULL DEFAULT '',
    round             INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,              -- 200,429,no_egress,bind_error,auth,timeout,dead,skipped,interrupted
    egress_ip         TEXT NOT NULL DEFAULT '',
    latency_ms        REAL,
    dns_ms            REAL,
    connect_ms        REAL,
    handshake_ms      REAL,
    rtt_ms            REAL,
    reset_at          REAL,
    retry_after_s     REAL,
    error             TEXT NOT NULL DEFAULT '',
    validation        TEXT NOT NULL DEFAULT '',
    fingerprint       TEXT NOT NULL DEFAULT '',
    started_at        REAL NOT NULL,
    finished_at       REAL,
    provider          TEXT NOT NULL DEFAULT 'nordvpn'
);

CREATE INDEX IF NOT EXISTS idx_attempt_host
    ON qualification_attempts(hostname, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempt_epoch
    ON qualification_attempts(epoch_id, status);
CREATE INDEX IF NOT EXISTS idx_attempt_station
    ON qualification_attempts(station, started_at DESC);
```

**Règles** :
- `status='200'` exige une preuve de contenu (constitution VII) ; un bind sans
  egress n'est jamais 200.
- `status='429'` exige `reset_at` fournisseur ou `retry_after_s` ; jamais
  `dead`.
- `status='no_egress'`/`bind_error` : `error` contient le transport et la
  station ; le hostname n'est pas condamné.
- `provider` n'est jamais vide (corrige l'absence actuelle dans
  `observations`).

### 2.3 `qualification_epochs`

```sql
CREATE TABLE IF NOT EXISTS qualification_epochs (
    epoch_id          TEXT PRIMARY KEY,
    provider          TEXT NOT NULL DEFAULT 'nordvpn',
    status            TEXT NOT NULL DEFAULT 'running', -- running, complete, partial, paused, failed
    total             INTEGER NOT NULL DEFAULT 0,
    tested            INTEGER NOT NULL DEFAULT 0,
    usable            INTEGER NOT NULL DEFAULT 0,
    quota             INTEGER NOT NULL DEFAULT 0,
    infra             INTEGER NOT NULL DEFAULT 0,
    dead              INTEGER NOT NULL DEFAULT 0,
    skipped           INTEGER NOT NULL DEFAULT 0,
    cursor            TEXT NOT NULL DEFAULT '',
    started_at        REAL NOT NULL,
    finished_at       REAL,
    reason            TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_epoch_status
    ON qualification_epochs(status, started_at DESC);
```

**Règles** :
- Une seule époque `running` par provider à la fois.
- `complete` : tous les hostnames éligibles ont un état terminal ou un
  `next_eligible_at` documenté.
- `partial` : budget/temps atteint ; `reason` explique.
- `paused` : action opérateur.
- `failed` : erreur irrécupérable ; la file reste lisible.

### 2.4 `sweep_cursor` (existant, réutilisé)

Le schéma existant convient : `batch_id` = `epoch_id`, `hostname`,
`transport`, `last_tested_at`, `next_eligible_at`, `attempts`, `failures`,
`last_status`. Il est vide aujourd'hui ; il sera alimenté par le balayage.

**Ajout recommandé** (migration douce) :
```sql
ALTER TABLE sweep_cursor ADD COLUMN epoch_id TEXT NOT NULL DEFAULT '';
```
ou utiliser `batch_id` comme époque pour éviter une migration.

### 2.5 `server_transports` (existant, à peupler)

```sql
-- déjà présent
hostname TEXT, transport TEXT, supported INTEGER,
wg_pubkey TEXT, last_ok_at REAL, last_error TEXT
```

**Règles** :
- `wireguard` : `supported=1` si `technologies` contient `wireguard_udp`.
- `openvpn-udp` : `openvpn_udp`.
- `openvpn-tcp` : `openvpn_tcp`.
- `last_ok_at` après un 200 sur ce transport ; `last_error` après un échec.
- Un host sans aucun transport supporté est `skipped`.

### 2.6 `egress_options` (existant, à alimenter)

Champs déjà présents : `key`, `provider`, `kind`, `host`, `port`, `station`,
`egress_ip`, `block`, `status`, `first_seen_at`, `last_seen_at`,
`last_checked_at`, `expires_at`, `reset_at`, `success_count`, `fail_count`,
`last_status`, `last_latency_ms`, `source_ids`, `meta_json`, `score`.

**Règles** :
- Une option par `egress_ip` (clé canonique) ; deux hostnames qui rendent la
  même IP fusionnent.
- `status='usable'` seulement après un 200 avec contenu.
- `status='quota'` avec `reset_at` fournisseur.
- `status='auth'` pour une station en quarantaine.
- `status='dead'` seulement pour une IP réellement morte (pas un bind).
- `station` vide pour une option non assignée.

### 2.7 `egress_ips` / `quota_buckets` (existants)

- `egress_ips` : état par IP (`fresh`, `exhausted`), `reset_at`, `successes`,
  `failures`, `latency_ms`, `throughput_tps`, `provider`, `block_key`.
- `quota_buckets` : `(ip, model, day, bucket)` → `used`, `successes`,
  `failures`, `last_status`, `reset_at`, `provider`.

**Règles** :
- `quota_remaining = quota_estimate - used` ; `0` si `reset_at > now`.
- Le reset est à 00:00 UTC pour le bucket `default` ; le `reset_at` fournisseur
  prime.
- Un 429 sans 200 antérieur n'incrémente pas `successes` ; il peut incrémenter
  `failures` et poser un `reset_at` court.

### 2.8 `station_runtime`

Une ligne par station (1–6). C'est l'état d'exécution indépendant, pas une
seconde vérité de l'IP.

```sql
CREATE TABLE IF NOT EXISTS station_runtime (
    station             INTEGER PRIMARY KEY,
    state               TEXT NOT NULL DEFAULT 'idle',
                        -- idle, serving, qualifying, rotating, repairing, paused
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
```

**Règles** :
- `current_*` et `standby_*` sont distincts ; un standby n'est pas une session
  ouverte, c'est une option pré-qualifiée.
- Aucune ligne `station_runtime` ne partage `current_egress_ip` avec une autre.
- `state='serving'` interdit la préemption par le balayage.
- Une panne de station ne modifie pas les autres lignes.
- `availability_*` est calculée depuis les sondes, jamais déclarée.

### 2.9 `station_cutovers`

Append-only. Chaque bascule d'une station, avec sa durée et son résultat.

```sql
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
                     -- pending, committed, rolled-back, failed
    reason           TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_cutover_station
    ON station_cutovers(station, started_at DESC);
```

**Règles** :
- `committed_at` n'est renseigné qu'après un 200 de la nouvelle IP.
- `duration_ms` mesure la fenêtre sans service ; 0 si second tunnel.
- Un `rolled-back` restaure l'ancienne IP et ne libère pas son lease.
- Aucun cutover n'est planifié sans standby validé, sauf décision opérateur
  tracée.

### 2.10 `transport_health`

Agrégat par transport et par station (station 0 = global).

```sql
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
```

**Règles** :
- `degraded=1` si `attempts >= transport_health_min_attempts` et
  `successes=0`.
- Un transport dégradé reste visible ; il n'est pas retenté comme sain.
- Le diagnostic OpenVPN (bind/AUTH/probe/DNS) est dans `last_error` et les
  tentatives.

### 2.11 `station_state` (existant, à peupler)

Une ligne par station. C'est l'état de disponibilité, pas l'état du worker.

```sql
-- déjà présent dans registry.db
station INTEGER PRIMARY KEY,
container TEXT, transport_effective TEXT, state TEXT, country TEXT,
current_ip TEXT, current_host TEXT, client_usable INTEGER,
last_probe_at REAL, last_200_at REAL, reason TEXT, updated_at REAL
```

**Règles** :
- `client_usable=1` seulement si le probe client a produit une réponse valide
  (constitution VII) ; `connected` ne suffit pas.
- `state` reflète la machine `usable`, `suspect`, `repairing`, `probing`,
  `parked`, `disabled`.
- Une panne de station ne modifie que sa ligne.
- `last_probe_at` et `last_200_at` sont la preuve ; pas de valeur déclarée.

### 2.12 `incidents` (existant, à étendre)

```sql
-- existant
id INTEGER PRIMARY KEY, scope TEXT, station INTEGER,
started_at REAL, ended_at REAL, cause TEXT, remediation TEXT, details TEXT
-- migration proposée
ALTER TABLE incidents ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE incidents ADD COLUMN action TEXT NOT NULL DEFAULT '';
ALTER TABLE incidents ADD COLUMN result TEXT NOT NULL DEFAULT '';
```

**Règles** :
- `scope='station'` pour une panne locale ; `scope='fleet'` uniquement pour un
  vrai problème de flotte.
- `cause` est un code stable (`tunnel_down`, `dns_broken`, `no_egress`,
  `auth_failed`, `quota_429`, `client_unusable`, `watchdog_stale`).
- `details`/`evidence_json` contient la preuve brute (probe, erreur, latence).
- Un incident de station ne doit jamais être agrégé en `fleet`.

### 2.13 `repair_jobs` / `repair_requests` (existants, à unifier)

```sql
-- repair_jobs existant
id INTEGER PRIMARY KEY, station INTEGER, cause TEXT,
started_at REAL, finished_at REAL, candidates_tried INTEGER,
transports_tried TEXT, result TEXT, last_error TEXT
-- migration proposée
ALTER TABLE repair_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE repair_jobs ADD COLUMN next_retry_at REAL NOT NULL DEFAULT 0;
ALTER TABLE repair_jobs ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE repair_jobs ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}';
```

**Règles** :
- Une seule réparation active par station.
- `status` : `pending`, `claimed`, `running`, `succeeded`, `failed`,
  `escalated`, `cancelled`.
- `attempts >= max_repair_attempts` → `escalated`, pas de boucle.
- `logs/repair_queue.db` n'est plus une autorité ; il peut rester un export
  mort.
- Le superviseur est le seul à écrire `repair_jobs`.

### 2.14 `discovery_runs`

```sql
CREATE TABLE IF NOT EXISTS discovery_runs (
    id                TEXT PRIMARY KEY,
    source            TEXT NOT NULL DEFAULT '',
    started_at        REAL NOT NULL,
    finished_at       REAL,
    status            TEXT NOT NULL DEFAULT 'running',
    server_count      INTEGER NOT NULL DEFAULT 0,
    transport_count   INTEGER NOT NULL DEFAULT 0,
    stale             INTEGER NOT NULL DEFAULT 0,
    catalog_fetched_at REAL,
    error             TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_discovery_started
    ON discovery_runs(started_at DESC);
```

**Règles** :
- Un run par cycle de découverte (API, cache, embedded, manifeste).
- `server_count` et `transport_count` sont les compteurs réels ; pas 0 par
  défaut après un run réussi.
- `stale=1` si la source est un fallback ou si l'API a échoué.
- `error` est borné ; un run en échec ne vide jamais le catalogue.

### 2.15 Entités de chantier (hors `registry.db`)

`ModuleBoundary` et `ExtractionSlice` (US12) sont des objets de **pilotage du
démantèlement**, pas des tables runtime : ils vivent dans
`docs/adr/ADR-010-nordvpn-control-plane.md`, `debt.md` et
`implementation-log.md`. Aucune table `registry.db` ne doit être créée pour
suivre une extraction.

---

## 3. Machines à états

### 3.1 Candidate

```text
pending ──claim──► claimed ──preflight──► probing ──200──► usable
   ▲                    │                     │
   │                    │                     ├──429──► quota ──reset──► pending
   │                    │                     ├──infra─► pending (backoff)
   │                    │                     ├──auth──► pending (station repair)
   │                    │                     ├──dead──► dead
   │                    │                     └──skip──► skipped
   │                    └──lease expiré──────────────► pending
   └────────────────── reset / repair / new epoch ────┘
```

- `usable` peut redevenir `quota` sur 429 ; il n'est jamais re-testé par le
  balayage tant qu'il sert.
- `quota` redevient `pending` à `reset_at + jitter`.
- `dead` est terminal pour l'époque ; réévalué à la prochaine ingestion si le
  host réapparaît.
- `skipped` est terminal pour l'époque ; `removed-from-catalog` peut redevenir
  `pending` si le host réapparaît.

### 3.2 Attempt

```text
started ──► 200 | 429 | no_egress | bind_error | auth | timeout | dead | skipped | interrupted
```

Une tentative terminée n'est jamais réécrite ; une tentative orpheline
(`finished_at IS NULL` après un crash) est close par
`status='interrupted'` au redémarrage.

### 3.3 Station / session

- `max_active_stations=6` ; une station = un worker.
- `claimed` par station ; lease `lease_until` ; renouvellement pendant le bind.
- `AUTH_FAILED` → station `quarantined` ; hostnames `pending`.
- Une station ne prend jamais une IP déjà `usable` par une autre.

### 3.4 Transport cycle

```text
auto:
  order  = [wireguard, openvpn-udp, openvpn-tcp] ∩ supported(hostname)
  cursor = 0 ; round = 0

  attempt(order[cursor])
    ├── 200            → usable, stop
    ├── 429            → quota(transport), cursor += 1 (ne saute pas les autres)
    ├── no_egress      → infra, cursor += 1
    ├── bind_error     → infra, cursor += 1
    ├── auth           → station quarantined, host pending (pas dead)
    └── unsupported    → skip, cursor += 1

  si cursor == len(order):
      round += 1
      si round >= transport_retry_rounds:
          host = pending avec backoff (ou skipped(max-transport-rounds))
      sinon:
          cursor = 0
          next_eligible_at = now + transport_retry_backoff_s + jitter
```

**Règles** :
- `manual` : `order = [transport choisi]` ; pas de cycle.
- Un hostname sans transport supporté est `skipped` immédiatement.
- Le curseur et le round sont persistés ; une reprise ne rejoue pas le round.
- Un `429` sur un transport n'arrête pas le cycle ; il est enregistré par
  transport.
- Tous les transports en `429` → host `quota` avec le plus tôt des resets.

### 3.5 Station health

```text
usable ──1 mauvais probe──► suspect ──2e mauvais──► repairing
   ▲                                                  │
   │                                                  ├── repair ok ──► probing ──2 bons──► usable
   │                                                  ├── max attempts ──► parked / escalated
   │                                                  └── cause locale ──► incident station
   └────────────────── 2 bons probes / reset ─────────┘
parked ──timer──► repairing
disabled ──manuel──► (hors circulation)
```

**Règles** :
- Un seul probe ne retire jamais une station ; deux mauvais probes la passent
  en `repairing` (`free/station_health.py`).
- Un `repairing` ne revient jamais directement `usable` : il repasse par
  `probing`.
- `parked` a un `parked_until` ; le retry repasse par `repairing`.
- Chaque transition écrit `station_state` + `incidents` (scope station).
- Aucune transition d'une station ne modifie une autre station.

### 3.6 Discovery / repair

```text
discovery: running ──► ok | stale | failed
  ok     : API fraîche, server_count > 0
  stale  : cache/embedded/manifeste, server_count > 0, stale=1
  failed : erreur bornée, catalogue précédent conservé

repair: pending ──► claimed ──► running ──► succeeded
                                  │
                                  ├── failed ──► pending (backoff) ──► ...
                                  └── attempts >= max ──► escalated
```

**Règles** :
- Un run de découverte `failed` ne vide jamais `qualification_candidates`.
- Une réparation `escalated` reste visible ; elle ne boucle pas.
- Un seul `repair_job` actif par station.

---

## 4. Scores

### 4.1 Potentiel (avant mesure)

```text
reliability = (200 + 1) / (200 + 429 + 2)          # Laplace ; infra exclu
latency     = 1 - min(1, rtt_ms / latency_budget_ms)
quota       = quota_remaining / quota_estimate      # 0.5 si inconnu
load        = 1 - min(1, api_load / 100)
freshness   = exp(-age_h / 24)

potential = 100 * (
    0.30 * reliability +
    0.30 * latency +
    0.20 * quota +
    0.15 * load +
    0.05 * freshness
)
```

- `rtt_ms` absent → `latency_budget_ms` (neutre, pas 0).
- `quota_estimate` par défaut 500 (`ip_quota.QuotaProfile`), ou
  `quota_per_ip_by_model`.
- Poids configurables ; normalisés à 1.

### 4.2 Mesuré (après 200)

```text
measured = 100 * (
    0.40 * success_rate +
    0.25 * latency_score +
    0.20 * throughput_score +
    0.10 * stability +
    0.05 * freshness
)
```

Identique à `free/egress_catalog.score_option` (constitution IX). La latence
utilise `last_latency_ms` through-tunnel ; le débit utilise
`throughput_score` ; la stabilité `1 - fail/tests`.

### 4.3 Ordre

```sql
ORDER BY potential_score DESC,
         COALESCE(quota_remaining, 0) DESC,
         COALESCE(rtt_ms, latency_budget_ms) ASC,
         load ASC,
         hostname ASC
```

---

## 5. Invariants

1. `qualification_candidates.hostname` est unique ; une ligne par hostname.
2. `qualification_attempts` est append-only ; aucune suppression avant
   rétention.
3. Une IP d'egress n'est jamais `usable` par deux stations (index unique sur
   `egress_options.egress_ip` quand `status='usable'`).
4. `reset_at` d'une option `quota` vient du fournisseur ; jamais d'estimation
   locale.
5. Un `429` n'est jamais `dead` ; un `no_egress` n'est jamais `dead`.
6. `country` n'apparaît dans aucune clause `WHERE` de porte dure.
7. Un hostname `skipped` a toujours `skipped_reason` non vide.
8. Une époque `complete` a `tested + skipped = total` et `cursor` vide.
9. Aucun hostname n'est claimé par deux stations à la même seconde.
10. `provider` est non vide dans chaque tentative et chaque observation.
11. `station_state` a exactement une ligne par station ; `client_usable=1`
    exige une preuve de réponse client, pas seulement `connected`.
12. Une station a au plus une IP courante et un standby ; aucune IP n'est
    partagée entre deux stations.
13. Un incident de station a `scope='station'` ; un incident `fleet` ne
    remplace jamais un incident local.
14. Un seul `repair_job` actif par station ; `registry.db` est l'autorité, pas
    `logs/repair_queue.db`.
15. Un run de découverte `failed` ne vide jamais la file ; `stale=1` est
    explicite.
16. Le watchdog a un heartbeat ; un heartbeat stale redémarre le watchdog seul.
17. Le cycle de transport est persisté ; aucun doublon
    `(hostname, transport, round)`.
18. Aucune pause globale : un `429`/`AUTH` n'affecte que la station/IP
    concernée.
19. Aucun fork de `vpn/manager.py`, `ops/lifecycle.py`, `ops/reconciler.py` ou
    `ops/supervisor.py`.

---

## 6. Migration

1. Backup `VACUUM INTO logs/registry.db.bak-20261109-before-014`.
2. `CREATE TABLE IF NOT EXISTS` pour `qualification_candidates`,
   `qualification_attempts`, `qualification_epochs`, `station_runtime`,
   `station_cutovers`, `transport_health`, `discovery_runs` ; index.
3. `ALTER TABLE sweep_cursor ADD COLUMN epoch_id` (ou utiliser `batch_id`).
4. Peupler `server_transports` depuis `logs/nordvpn_api.json`.
5. Peupler `qualification_candidates` depuis l'API (7 070) ; ne pas importer
   les 9 788 lignes legacy comme candidates actives.
6. Créer/étendre `station_state`, `incidents`, `repair_jobs` ; migrer
   `logs/repair_queue.db` vers `repair_jobs` puis le laisser en lecture seule.
7. Brancher le watchdog et la découverte en shadow ; vérifier `station_state`,
   `incidents` et `discovery_runs`.
8. Shadow : calculer les scores, ne pas binder.
9. Canary : une station, 100 hostnames.
10. Rollback : restore backup ; les nouvelles tables sont optionnelles.

---

## 7. Rétention

- `qualification_attempts` : 7 jours (configurable), purge quotidienne.
- `qualification_epochs` : 30 jours.
- `qualification_candidates` : tant que le host est dans le catalogue + 7
  jours ; `last_seen_at` fait foi.
- `egress_options` : TTL existant (`ttl_usable_s`, `ttl_quota_max_s`,
  `ttl_dead_s`).
- `quota_buckets` : 30 jours ; `reset_at` conservé.
- `sweep_cursor` : une ligne par `(batch_id, hostname, transport)` ; purge
  avec l'époque.
- `station_cutovers` : 30 jours.
- `incidents` : 90 jours (preuve d'exploitation).
- `repair_jobs` : 30 jours ; les jobs ouverts ne sont jamais purgés.
- `discovery_runs` : 30 jours.
- `transport_health` : agrégat courant, pas d'historique lourd.

---

## 8. Volumétrie estimée

| Table | Lignes | Taille |
|---|---:|---:|
| `qualification_candidates` | 7 070 | ~5 Mo |
| `qualification_attempts` | 7 070 × 3 × 30 j ≈ 636 k | ~150 Mo |
| `qualification_epochs` | 30 | négligeable |
| `server_transports` | 7 070 × 2–3 ≈ 18 k | ~3 Mo |
| `egress_options` | 7 026 existant + nouvelles IP | ~10 Mo |
| `station_runtime` | 6 | négligeable |
| `station_cutovers` | quelques milliers | ~2 Mo |
| `incidents` | quelques milliers | ~5 Mo |
| `repair_jobs` | quelques milliers | ~3 Mo |
| `discovery_runs` | 4/j × 30 j ≈ 120 | négligeable |
| `transport_health` | 3 × 6 = 18 | négligeable |

La rétention 7 jours ramène `qualification_attempts` à ~150 k lignes.
