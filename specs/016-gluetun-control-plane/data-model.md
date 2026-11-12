# Data Model — plan de contrôle Gluetun

**Feature** : `016-gluetun-control-plane`
**Date** : 2026-11-11
**Portée** : entités du fork (agent de tunnel) et relations avec `registry.db`
(autorité côté orchestrateur). Les noms JSON sont ceux du contrat HTTP.

---

## 1. Vue d'ensemble

```text
DesiredState 1 ──── * SwitchJob ──── * EgressObservation
      │                   │
      │                   └──── 1 EgressVerification ──── * EgressObservation
      │                   └──── 1 TunnelSnapshot (avant)
      │                   └──── 1 TunnelSnapshot (après)
      ├──── 1 TunnelSnapshot (courant)
      ├──── 1 DNSState ──── * ResolverTier ──── * ResolverEndpoint
      ├──── 1 TransportState ──── * TransportAttempt
      ├──── 1 TransportPolicy
      └──── 1 IdempotencyIndex (clé → job)

ControlEvent * (append-only, seq monotone)
ServerCandidate * (catalogue provider, lecture seule + stats runtime)
```

Le fork ne possède **pas** : `IpClaim`, `Lease`, `QuotaBucket`, `Score`,
`ClientUsableVerdict`. Ces objets restent dans `registry.db`.

## 2. DesiredState

État persistant, versionné, écrit atomiquement.

```go
type DesiredState struct {
    SchemaVersion int             `json:"schema_version"` // 1
    StationID     string          `json:"station_id"`     // vpn-gluetun-3
    UpdatedAt     time.Time       `json:"updated_at"`
    Checksum      string          `json:"checksum"`       // SHA-256 du canonique
    Mode          string          `json:"mode"`           // state|env|merge

    Desired       TunnelSettings  `json:"desired"`
    LastCommitted *TunnelSnapshot `json:"last_committed,omitempty"`
    Current       *TunnelSnapshot `json:"current,omitempty"`

    Jobs          JobIndex        `json:"jobs"`           // ids récents + curseur
    Events        EventCursor     `json:"events"`         // dernier seq persisté
    Observations  ObservationRing `json:"observations"`   // N dernières, borné
}
```

Règles :

- `Desired` contient uniquement des settings **non secrets** et des références
  d'environnement (`env:VPN_SERVICE_PROVIDER`, etc.).
- Une modification de secret via l'API exige `CONTROL_SECRET_KEY` ; sinon
  `409 secret_persistence_unavailable`.
- `Checksum` couvre le JSON canonique (clés triées, pas d'espaces).
- Écriture : `state.json.tmp` → `fsync` → `rename` → `fsync(dir)`, mode 0600.
- Si le checksum échoue : `state.invalid`, fallback selon `CONTROL_STATE_MODE`,
  l'ancien fichier n'est pas écrasé.

### Modes de réconciliation

| Mode | Comportement au démarrage |
|---|---|
| `state` (défaut si fichier valide) | Le state gagne ; `state.recovered` si différent de l'env. |
| `env` | L'environnement gagne ; `state.drift` si le state diffère. |
| `merge` | Le state gagne pour les champs qu'il a explicitement modifiés, l'env pour le reste ; `state.merged`. |

## 3. TunnelTarget

Cible d'une bascule ou d'un candidat de rotation.

```json
{
  "provider": "nordvpn",
  "vpn": "wireguard",
  "countries": ["Netherlands"],
  "regions": [],
  "cities": ["Amsterdam"],
  "isps": [],
  "hostnames": ["nl-1234"],
  "numbers": [],
  "categories": ["standard"],
  "tiers": ["free"],
  "hostname": "nl-1234.nordvpn.com",
  "endpoint_ip": "203.0.113.10",
  "expected_egress_ip": "198.51.100.7",
  "constrained": {
    "require_new_ip": true,
    "require_new_subnet": false,
    "exclude_hostnames": ["nl-9999"],
    "exclude_ips": ["198.51.100.9"],
    "exclude_subnets": ["198.51.100.0/24"]
  }
}
```

Champs :

| Champ | Type | Rôle |
|---|---|---|
| `provider` | string | Provider (nordvpn pour la flotte). |
| `vpn` | `openvpn\|wireguard` | Transport. |
| `countries/regions/cities/isps/categories/tiers` | []string | Filtres catalogue. |
| `hostnames/numbers/names` | []string/[]uint16 | Sélection explicite. |
| `hostname` | string | Cible unique (switch). |
| `endpoint_ip` | IP | IP du **serveur VPN**, validée contre le hostname. |
| `expected_egress_ip` | IP | IP de **sortie** attendue. |
| `constrained` | objet | Portes de la rotation. |

> `endpoint_ip` ≠ `expected_egress_ip` : le premier est l'entrée du tunnel, le
> second la sortie observée. Cette distinction est un invariant de contrat.

## 4. ServerCandidate

Vue lecture seule du catalogue provider + statistiques runtime du fork.

```json
{
  "hostname": "nl-1234.nordvpn.com",
  "name": "Netherlands #1234",
  "number": 1234,
  "country": "Netherlands",
  "region": "North Holland",
  "city": "Amsterdam",
  "isp": "M247",
  "categories": ["standard"],
  "tier": "free",
  "ips": ["203.0.113.10"],
  "protocols": ["openvpn", "wireguard"],
  "wireguard_public_key": "...",
  "owned": false,
  "stream": false,
  "multihop": false,
  "port_forward": false,
  "secure_core": false,
  "tor": false,
  "stats": {
    "attempts": 12,
    "successes": 9,
    "failures": 3,
    "last_error": "auth_failed",
    "last_used_at": "2026-11-11T18:00:00Z",
    "latency_ewma_ms": 420,
    "cooldown_until": "2026-11-11T18:05:00Z"
  }
}
```

Les `stats` sont **consultatives** : elles n'ordonnent pas la liste reçue et ne
constituent pas un score. Elles servent au diagnostic et aux facettes.

## 5. TunnelSnapshot

Photographie complète et restaurable d'une station.

```go
type TunnelSnapshot struct {
    SchemaVersion int             `json:"schema_version"`
    StationID     string          `json:"station_id"`
    TakenAt       time.Time       `json:"taken_at"`
    Trigger       string          `json:"trigger"` // switch|rotate|rollback|recovery

    Settings      TunnelSettings  `json:"settings"` // non secrets + refs
    Server        *ServerCandidate `json:"server,omitempty"`
    EndpointIP    netip.Addr      `json:"endpoint_ip,omitempty"`
    Transport     string          `json:"transport"` // openvpn|wireguard
    Interface     string          `json:"interface,omitempty"`
    VPNStatus     string          `json:"vpn_status"` // running|stopped|unknown
    Egress        *EgressVerification `json:"egress,omitempty"`
    DNS           *DNSState       `json:"dns,omitempty"`
    ProvenAt      *time.Time      `json:"proven_at,omitempty"`
}
```

Un snapshot est **immuable** une fois écrit. Le rollback réapplique
`Settings`/`Server`, puis re-vérifie l'egress ; il ne réutilise pas la preuve
passée comme preuve présente.

## 6. SwitchJob

Unité transactionnelle. `kind` ∈ `switch|rotate|rollback|recover`.

```json
{
  "id": "job_01J9Z3K4T5",
  "kind": "switch",
  "state": "verifying_egress",
  "station_id": "vpn-gluetun-2",
  "created_at": "2026-11-11T18:10:00Z",
  "started_at": "2026-11-11T18:10:00.120Z",
  "updated_at": "2026-11-11T18:10:06.400Z",
  "finished_at": null,
  "target": { "...": "TunnelTarget" },
  "candidates": ["nl-1234.nordvpn.com", "nl-5678.nordvpn.com"],
  "attempts": [
    {
      "hostname": "nl-1234.nordvpn.com",
      "phase": "verifying_egress",
      "started_at": "2026-11-11T18:10:00.200Z",
      "finished_at": "2026-11-11T18:10:06.100Z",
      "result": "rejected",
      "reason": "egress_mismatch",
      "observed_ip": "198.51.100.9",
      "expected_ip": "198.51.100.7"
    }
  ],
  "snapshot_before": { "...": "TunnelSnapshot" },
  "snapshot_after": null,
  "verification": { "...": "EgressVerification" },
  "timings_ms": {
    "validating": 20,
    "preflight": 180,
    "draining": 0,
    "applying": 3400,
    "awaiting_tunnel": 2100,
    "verifying_egress": 900
  },
  "error": null,
  "idempotency_key": "rotate-s2-1731",
  "requested_by": "orchestrator"
}
```

### États

```text
                    ┌──────────┐
                    │ queued   │
                    └────┬─────┘
                         ▼
                    ┌──────────┐   cible inconnue / dry_run
                    │validating├──────────────► rejected (422)
                    └────┬─────┘
                         ▼
                    ┌──────────┐   préflight échoué
                    │preflight ├──────────────► rejected (candidat suivant)
                    └────┬─────┘
                         ▼
                    ┌──────────┐   timeout
                    │ draining ├──────────────► drain.timeout (continue borné)
                    └────┬─────┘
                         ▼
                    ┌──────────┐   erreur d'application
                    │ applying ├──────────────► rolling_back
                    └────┬─────┘
                         ▼
                 ┌───────────────┐  timeout tunnel
                 │awaiting_tunnel├──────────────► rolling_back
                 └───────┬───────┘
                         ▼
                 ┌────────────────┐ preuve insuffisante / gate
                 │verifying_egress├──────────────► rolling_back
                 └───────┬────────┘
                         ▼
                    ┌──────────┐
                    │committed │
                    └──────────┘

rolling_back ── succès ──► rolled_back
             ── échec  ──► failed_degraded
queued/validating/preflight/applying/... ── cancel ──► cancelled
crash au restart ─────────────────────────────────► interrupted
rotation budget épuisé ───────────────────────────► exhausted
```

### Règles de transition

| De | Vers | Condition |
|---|---|---|
| `queued` | `validating` | Job accepté (scope, pas d'autre job actif). |
| `validating` | `rejected` | Cible inconnue, credentials absents, transport invalide. |
| `validating` | `preflight` | Cible résolue. |
| `preflight` | `rejected` | Résolution/IP/TCP/UDP échoue ; **aucune coupure**. |
| `preflight` | `draining` | Préflight OK. |
| `draining` | `applying` | Drain terminé ou timeout borné. |
| `applying` | `awaiting_tunnel` | `SetSettings` accepté. |
| `awaiting_tunnel` | `verifying_egress` | Statut running + interface + handshake. |
| `verifying_egress` | `committed` | Consensus + gates + canary OK. |
| `verifying_egress` | `rolling_back` | Preuve/gate échoue. |
| `rolling_back` | `rolled_back` | Ancien snapshot réappliqué et re-vérifié. |
| `rolling_back` | `failed_degraded` | Ancien serveur impossible à restaurer. |
| tout état actif | `cancelled` | Annulation admin. |
| tout état actif au restart | `interrupted` | Pas de heartbeat ; résolution ensuite. |

Invariants :

- Un seul job dans un état actif par station (`INV-JOB-01`).
- Aucune transition ne saute `validating`/`preflight` si `require_verified=true`
  (`INV-JOB-02`).
- `committed` exige une `EgressVerification.verified=true` (`INV-JOB-03`).
- `rolled_back` exige une nouvelle vérification de l'ancienne IP
  (`INV-JOB-04`).
- `failed_degraded` est terminal et émet un événement critique ; aucune boucle
  (`INV-JOB-05`).
- `exhausted` n'écrit jamais d'état global et ne désactive pas la station
  (`INV-JOB-06`).

## 7. EgressObservation

```json
{
  "source": "http:ipify",
  "kind": "http|dns|publicip|canary",
  "ip": "198.51.100.7",
  "country": "Netherlands",
  "region": "North Holland",
  "city": "Amsterdam",
  "asn": 9009,
  "organization": "M247",
  "latency_ms": 210,
  "observed_at": "2026-11-11T18:10:06Z",
  "accepted": true,
  "rejection_reason": null
}
```

Rejets durs : `private`, `loopback`, `link_local`, `cgnat`, `multicast`,
`unspecified`, `host_ip`, `forbidden_ip`, `malformed`, `wrong_family`.

## 8. EgressVerification

```json
{
  "verified": true,
  "ip": "198.51.100.7",
  "family": 4,
  "quorum": 2,
  "source_count": 3,
  "confidence": "high",
  "dissenters": ["dns:whoami"],
  "observations": [ "...": "EgressObservation[]" ],
  "canary": {
    "url": "https://canary.invalid/health",
    "status": 200,
    "body_bytes": 128,
    "body_sha256": "…",
    "latency_ms": 190,
    "ok": true,
    "failure": null
  },
  "cached": false,
  "verified_at": "2026-11-11T18:10:06Z",
  "ttl_s": 10
}
```

Règles :

- `verified=true` ⇒ `quorum ≥ CONTROL_EGRESS_QUORUM` (défaut 2) sur la même
  valeur d'IP, `source_count ≥ 3` par défaut, et canary OK si activé.
- `allow_single_source=false` par défaut : une seule source ⇒
  `insufficient_sources`.
- Les observations rejetées ne contribuent pas au quorum.
- `confidence` : `high` (toutes les sources), `medium` (quorum + 1 dissident),
  `low` (quorum exact avec plusieurs dissidents).
- Cache 10 s ; `fresh=true` force une nouvelle collecte ; single-flight par
  station.

## 9. DNSState, ResolverTier, ResolverEndpoint

```json
{
  "ladder": ["dot", "plain"],
  "fail_closed": true,
  "active_tier": "dot",
  "updated_at": "2026-11-11T18:10:00Z",
  "tiers": [
    {
      "type": "dot",
      "state": "up",
      "listener": "127.0.0.1:53",
      "successes": 120,
      "failures": 2,
      "latency_ewma_ms": 38,
      "cooldown_until": null,
      "last_error": null,
      "endpoints": [
        {
          "address": "1.1.1.1:853",
          "provider": "cloudflare",
          "state": "up",
          "successes": 60,
          "failures": 1,
          "latency_ewma_ms": 35,
          "cooldown_until": null,
          "last_error": null
        }
      ]
    },
    {
      "type": "plain",
      "state": "standby",
      "listener": "127.0.0.1:53",
      "successes": 0,
      "failures": 0,
      "latency_ewma_ms": 0,
      "cooldown_until": null,
      "endpoints": []
    }
  ]
}
```

Règles :

- `tiers` est ordonné ; `active_tier` est le premier étage `up`.
- Un étage est `up|degraded|down|standby`.
- `down` après `DNS_HEALTH_FAILURES` échecs consécutifs ; `cooldown` avec
  backoff ; récupération après `DNS_HEALTH_SUCCESSES` succès consécutifs.
- `fail_closed=true` si tous les étages sont `down` ; aucune requête vers les
  résolveurs de l'hôte.
- `NXDOMAIN` est une réponse valide et ne change pas la santé.
- Cache/filter/localdns restent au niveau du listener, pas par étage.

## 10. ControlEvent

```json
{
  "seq": 1042,
  "ts": "2026-11-11T18:10:06.412Z",
  "type": "job.committed",
  "severity": "info",
  "station_id": "vpn-gluetun-2",
  "job_id": "job_01J9Z3K4T5",
  "data": {
    "hostname": "nl-5678.nordvpn.com",
    "egress_ip": "198.51.100.7",
    "quorum": 2,
    "latency_ms": 900
  }
}
```

Types normalisés :

```text
state.recovered, state.drift, state.invalid, state.merged
job.queued, job.validating, job.preflight_failed, job.draining,
job.applying, job.awaiting_tunnel, job.verifying,
job.committed, job.rolled_back, job.failed_degraded,
job.exhausted, job.cancelled, job.interrupted
egress.observed, egress.rejected, egress.verified, egress.canary_failed
dns.tier_up, dns.tier_down, dns.tier_recovered, dns.ladder_exhausted
switch.noop, drain.timeout, secret.rejected
```

`seq` est monotone par station, persisté dans `DesiredState.Events`, et exposé
pour `Last-Event-ID`.

## 11. IdempotencyIndex

```json
{
  "key": "rotate-s2-1731",
  "job_id": "job_01J9Z3K4T5",
  "created_at": "2026-11-11T18:10:00Z",
  "expires_at": "2026-11-12T18:10:00Z",
  "request_sha256": "…"
}
```

- Même clé + même corps ⇒ retourne le job existant (`200`).
- Même clé + corps différent ⇒ `409 idempotency_conflict`.
- Fenêtre 24 h, purge au démarrage et périodique.

## 12. Capabilities

```json
{
  "control_plane_version": "1.0.0",
  "upstream_version": "v3.41.3",
  "features": {
    "switch": true,
    "rotate": true,
    "rollback": true,
    "dry_run": true,
    "drain": true,
    "dns_ladder": true,
    "egress_consensus": true,
    "transport_ladder": true,
    "events_sse": true,
    "state_persistence": true,
    "secret_reveal": true
  },
  "limits": {
    "max_body_bytes": 1048576,
    "max_candidates": 20,
    "max_attempts": 10,
    "max_switch_per_minute": 6,
    "default_timeout_s": 45,
    "quorum": 2,
    "min_sources": 3,
    "egress_cache_ttl_s": 10,
    "transport_attempt_timeout_s": {"wireguard": 30, "openvpn-udp": 30, "openvpn-tcp": 45},
    "transport_max_attempts": 2,
    "transport_breaker_threshold": 3,
    "transport_backoff_base_s": 30,
    "transport_backoff_max_s": 300,
    "idempotency_window_h": 24
  },
  "state_mode": "state",
  "dns_default_ladder": ["dot", "plain"],
  "transport_default_ladder": ["wireguard", "openvpn-udp", "openvpn-tcp"]
}
```

## 12bis. TransportPolicy, TransportAttempt, TransportState

### TransportPolicy

```json
{
  "ladder": ["wireguard", "openvpn-udp", "openvpn-tcp"],
  "attempt_timeout_s": {"wireguard": 30, "openvpn-udp": 30, "openvpn-tcp": 45},
  "max_attempts_per_transport": 2,
  "breaker_threshold": 3,
  "breaker_cooldown_s": 120,
  "backoff_base_s": 30,
  "backoff_max_s": 300,
  "max_cycles": 3,
  "jitter": 0.2
}
```

### TransportAttempt

```json
{
  "attempt_id": "ta_01J9Z5M2",
  "transport": "openvpn-udp",
  "server_hostname": "nl-5678.nordvpn.com",
  "started_at": "2026-11-11T18:10:04Z",
  "ended_at": "2026-11-11T18:10:31Z",
  "result": "failed",
  "reason": "udp_blocked",
  "latency_ms": 27000,
  "next_retry_at": "2026-11-11T18:12:31Z"
}
```

### TransportState

```json
{
  "active_transport": "openvpn-tcp",
  "policy": { "ladder": ["wireguard", "openvpn-udp", "openvpn-tcp"] },
  "phase": "committed",
  "cycle": 1,
  "breaker": {
    "wireguard":   {"state": "open",      "failures": 3, "opened_at": "…", "retry_at": "…"},
    "openvpn-udp": {"state": "open",      "failures": 3, "opened_at": "…", "retry_at": "…"},
    "openvpn-tcp": {"state": "closed",    "failures": 0}
  },
  "attempts": ["ta_01J9Z5M2", "ta_01J9Z5M3"],
  "next_attempt_at": null,
  "updated_at": "2026-11-11T18:10:40Z"
}
```

Règles :

- `breaker.state ∈ {closed, open, half_open}` ; `open` ignore le transport
  jusqu'à `retry_at`, puis une sonde `half_open` décide.
- `active_transport` est unique ; il est `null` pendant un teardown ou un cycle
  sans tunnel.
- `cycle` compte les passages complets sur `policy.ladder` ; à `max_cycles`,
  `transport.cycle_exhausted` puis arrêt (pas de boucle infinie).
- `attempts` est borné (N derniers) ; le journal d'événements garde l'historique.
- Aucun champ serveur/IP n'est modifié par le ladder : `server_hostname` est
  seulement recopié pour la traçabilité.

## 13. Invariants globaux

| ID | Invariant |
|---|---|
| INV-01 | Un seul job actif par station. |
| INV-02 | Aucune coupure de tunnel avant un préflight réussi (sauf `dry_run`). |
| INV-03 | Tout `committed` porte une preuve d'egress fraîche (`verified=true`). |
| INV-04 | Tout `rolled_back` porte une re-vérification de l'ancienne IP. |
| INV-05 | Aucun état global « aucune IP disponible ». |
| INV-06 | Le fork ne re-score jamais une liste ordonnée. |
| INV-07 | `endpoint_ip` et `expected_egress_ip` sont distincts et validés séparément. |
| INV-08 | Les IP privées/loopback/link-local/CGNAT/multicast/host/forbidden sont rejetées. |
| INV-09 | Une observation rejetée ne contribue pas au quorum. |
| INV-10 | Le state est écrit atomiquement et son checksum est vérifié. |
| INV-11 | Les secrets ne sont jamais sérialisés sans `reveal=true` admin. |
| INV-12 | `fail_closed=true` interdit tout resolver de l'hôte. |
| INV-13 | `NXDOMAIN` n'est jamais un fallback. |
| INV-14 | Une seule vérité par cycle de vie : tunnel au fork, IP/quota/SLO au registre. |
| INV-15 | Aucune création de station/session supplémentaire. |
| INV-16 | `failed_degraded` est terminal et sans boucle. |
| INV-17 | Les événements sont append-only et `seq` est monotone. |
| INV-18 | L'idempotence ne rejoue jamais une bascule déjà committée. |
| INV-19 | Le canary exige 200 + corps non vide. |
| INV-20 | Un redémarrage laisse un journal rejouable ou un état explicite. |
| INV-21 | Un seul transport/tunnel actif à tout instant ; le précédent est arrêté avant le suivant. |
| INV-22 | Le ladder ne change jamais de serveur, d'IP ou de hostname. |
| INV-23 | Un transport `open` est ignoré jusqu'à son cooldown ; `half_open` ne fait qu'une sonde. |
| INV-24 | Après un cycle épuisé, le ladder revient à `policy[0]` après backoff, jamais en boucle serrée. |
| INV-25 | `auth` n'est jamais retenté indéfiniment sur le même transport. |
| INV-26 | Chaque tentative a une raison typée et est exposée par `GET /v1/vpn/transport`. |
| INV-27 | Firewall et MTU/PMTUD sont réappliqués avant de déclarer un transport `up`. |

## 14. Relations avec `registry.db`

| Objet fork | Objet registre | Sens |
|---|---|---|
| `TunnelSnapshot.egress.ip` | `observations.ip`, `egress_ips` | Le fork fournit un fait ; le registre décide du claim. |
| `SwitchJob.id` | `observations.job_id` | Traçabilité, pas d'autorité. |
| `ControlEvent` | `incidents`, `observations` | Ingestion append-only. |
| `ServerCandidate.stats` | `servers` (score) | Le score reste au registre ; les stats sont consultatives. |
| `DesiredState` | `stations.desired_*` | Le registre est autoritaire ; le state est l'exécution locale. |
| `EgressVerification` | `egress_verifications` (si présente) | Preuve brute conservée pour audit. |

Aucune clé étrangère inter-bases : la synchronisation est événementielle et
idempotente côté Python.
