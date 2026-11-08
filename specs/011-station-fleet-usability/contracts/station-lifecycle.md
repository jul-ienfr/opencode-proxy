# Contract — Station lifecycle API & CLI

**Feature** : `specs/011-station-fleet-usability/`  
**Base** : API FastAPI existante (`dashboard/api.py`), préfixe `/api`.  
**Auth** : `X-Dashboard-Token` si `dashboard_trust.require_token` ou si
`server.host` n'est pas loopback.  
**Idempotence** : toutes les actions sont idempotentes par `(provider, station,
generation)` ; une action déjà appliquée renvoie `202` avec le même `job_id` ou
`200` avec `noop=true`.

## 1. GET `/api/stations`

Vue agrégée des stations et endpoints.

**Query** : `provider?` (`nordvpn|warp|proxies|direct`), `state?`, `include_disabled?` (bool).

**200** :

```json
{
  "generated_at": "2026-11-07T14:10:00Z",
  "target_slots": 6,
  "reserve_policy": "full",
  "providers": {
    "nordvpn": {
      "configured": 6,
      "created": 6,
      "running": 6,
      "tunnel_up": 6,
      "egress_ok": 6,
      "quota_ok": 6,
      "client_usable": 6,
      "unique_buckets": 6,
      "deficit": 0,
      "status": "ok"
    },
    "warp": {
      "configured": 6,
      "created": 6,
      "running": 6,
      "tunnel_up": 6,
      "egress_ok": 6,
      "quota_ok": 5,
      "client_usable": 5,
      "unique_buckets": 5,
      "deficit": 1,
      "status": "degraded"
    }
  },
  "stations": [
    {
      "provider": "nordvpn",
      "station": 1,
      "container": "opencode-vpn",
      "generation": 7,
      "state": "client_usable",
      "client_usable": true,
      "current_ip": "87.120.102.85",
      "block": "87.120.102.85/32",
      "current_host": "al41.nordvpn.com",
      "quota_state": "ok",
      "reset_at": null,
      "last_probe_at": "2026-11-07T14:09:30Z",
      "last_200_at": "2026-11-07T14:09:30Z",
      "last_evidence": "sha256:…;bytes=1536",
      "reason": null,
      "next_action": "none"
    }
  ],
  "warnings": []
}
```

**Codes** : `200`, `401` (token), `503` (registre indisponible).

## 2. GET `/api/stations/{provider}/{station}`

**200** : objet station détaillé + `desired`, `actual`, `last_repair`,
`last_rotation`, `quota`, `evidence`.

```json
{
  "desired": {
    "provider": "warp",
    "station": 2,
    "generation": 4,
    "stack": "warp",
    "transport": "socks5",
    "port": 11085,
    "container": "warp-test2",
    "ipv6": true,
    "enabled": true
  },
  "actual": {
    "container_state": "running",
    "health": "healthy",
    "egress_ip": "2a09:bac1:27a0:2060::3db:41",
    "block": "2a09:bac1:27a0:2060::/64",
    "tunnel_up": true,
    "dns_ok": true
  },
  "state": "degraded",
  "client_usable": false,
  "reason": "duplicate_block",
  "next_action": "rotate",
  "quota": {
    "bucket": "2a09:bac1:27a0:2060::/64",
    "used": 0,
    "reset_at": null,
    "state": "ok"
  },
  "last_repair": null,
  "last_rotation": {
    "job_id": 42,
    "trigger": "manual",
    "result": "failed",
    "reason": "duplicate_block"
  }
}
```

**Codes** : `200`, `401`, `404`, `503`.

## 3. POST `/api/stations/{provider}/{station}/reconcile`

Applique le desired state à la station.

**Body** :

```json
{
  "force": false,
  "reason": "operator"
}
```

**202** :

```json
{
  "job_id": "rec-20261107-141000-nordvpn-1",
  "accepted": true,
  "noop": false
}
```

**Codes** : `202`, `401`, `404`, `409` (job en cours), `422` (desired invalide), `503`.

## 4. POST `/api/stations/{provider}/{station}/rotate`

Demande une rotation explicite.

**Body** :

```json
{
  "trigger": "manual",
  "force": false,
  "reason": "quota_low"
}
```

**202** : `{job_id, accepted, slot_preserved: true}`.  
**409** : budget atteint, rotation en cours, ou provider non rotatable.  
**Codes** : `202`, `401`, `404`, `409`, `422`, `503`.

## 5. POST `/api/stations/{provider}/{station}/repair`

Demande une réparation explicite.

**Body** :

```json
{
  "cause": "server_dead",
  "force": false,
  "reason": "operator"
}
```

**202** : `{job_id, accepted, action}`.  
**409** : réparation déjà en cours ou station saine.  
**Codes** : `202`, `401`, `404`, `409`, `422`, `503`.

## 6. GET `/api/egress/capacity` (étendu)

Le contrat 010 est conservé ; champs ajoutés :

```json
{
  "target_slots": 6,
  "reserve_policy": "full",
  "verdict": "degraded",
  "providers": {
    "warp": {
      "configured": 6,
      "enabled": 6,
      "usable": 5,
      "unique_buckets": 5,
      "client_usable": 5,
      "deficit": 1,
      "status": "degraded"
    }
  },
  "total_usable": 11,
  "total_required": 12,
  "unique_buckets": 11,
  "warnings": ["duplicate_block:warp-ipv6-2"],
  "recommendations": ["Tourner warp-ipv6-2 vers un /64 distinct."]
}
```

**Règle** : `usable` d'un provider = `min(endpoints client-utilisables,
buckets uniques)`.

## 7. GET `/api/stations/{provider}/{station}/evidence`

Dernières preuves brutes (hashes, tailles, statuts, timestamps).

**200** :

```json
{
  "station": {"provider": "nordvpn", "station": 1},
  "probes": [
    {
      "at": "2026-11-07T14:09:30Z",
      "kind": "client",
      "status": 200,
      "content_bytes": 1536,
      "content_hash": "sha256:…",
      "model": "deepseek-v4.1-flash-free",
      "error": null
    }
  ]
}
```

**Rétention** : 7 jours pour les preuves brutes, agrégats 30 jours.

## 8. CLI `scripts/station_audit.py`

```text
python scripts/station_audit.py --json
python scripts/station_audit.py --provider warp --fail-on degraded
python scripts/station_audit.py --fix --dry-run
```

**Sortie JSON** :

```json
{
  "ok": false,
  "generated_at": "2026-11-07T14:10:00Z",
  "target_slots": 6,
  "providers": {"nordvpn": {...}, "warp": {...}},
  "duplicates": [
    {"kind": "block", "value": "2a09:bac1:27a0:2060::/64", "labels": ["warp-ipv6-2", "warp-ipv6-4"]}
  ],
  "verdict": "degraded"
}
```

**Exit codes** : `0` ok, `1` degraded/insufficient, `2` erreur d'exécution,
`3` configuration invalide.

## 9. Contrat GUI

- La vue `VpnStations.svelte` affiche une ligne par station : provider, slot,
  état, IP, bucket, quota, dernière preuve, prochaine action.
- Chaque action (reconcile/rotate/repair) ouvre une confirmation et affiche le
  `job_id`.
- Le badge de capacité affiche `client_usable / target_slots` et
  `unique_buckets` ; jamais `configured` seul.
- Une station `connected` mais non `client_usable` a un badge distinct.

## 10. Erreurs et raisons normalisées

| Code | Raison | Sens |
|---|---|---|
| `AUTH_FAILED` | `auth` | Credentials/throttle NordVPN. |
| `SERVER_DEAD` | `server_dead` | Hostname/IP ne répond plus. |
| `DNS_FAILED` | `dns` | Résolution impossible. |
| `TRANSPORT_FAILED` | `transport` | UDP/TCP/WG bloqué. |
| `QUOTA_EXHAUSTED` | `quota_429` | Bucket épuisé, reset connu. |
| `HEALTHCHECK_FAILED` | `healthcheck` | Conteneur vivant, egress mort. |
| `PROXY_DOWN` | `proxy_down` | Proxy local arrêté. |
| `DUPLICATE_BLOCK` | `duplicate_block` | Bucket partagé. |
| `GENERATION_MISMATCH` | `generation` | Conteneur obsolète. |
| `PORT_CONFLICT` | `port_conflict` | Port déjà utilisé. |

**Journalisation** : chaque action écrit `station`, `provider`, `cause`,
`action`, `evidence`, `result`, `duration_ms` dans `registry.db` ; aucun secret.
