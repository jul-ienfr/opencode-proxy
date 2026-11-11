# Contract — Qualification API

**Feature** : `014-nordvpn-exhaustive-rotation`
**Base** : `dashboard/api.py`
**Auth** : les endpoints de lecture sont publics sur le dashboard local ; les
mutations exigent le même contrôle que les autres routes d'administration.

---

## 1. `GET /api/egress/qualification/summary`

Réponse 200 :

```json
{
  "provider": "nordvpn",
  "epoch": {
    "epoch_id": "epoch-20261109",
    "status": "running",
    "started_at": 1794255478.83,
    "finished_at": null,
    "total": 7070,
    "tested": 3200,
    "usable": 42,
    "quota": 2100,
    "infra": 900,
    "dead": 12,
    "skipped": 146,
    "cursor": "se426.nordvpn.com"
  },
  "queue": {
    "total": 7070,
    "pending": 3180,
    "usable": 42,
    "quota": 2100,
    "auth": 8,
    "dead": 12,
    "skipped": 146,
    "claimed": 6
  },
  "coverage": {
    "tested_pct": 45.3,
    "terminal_pct": 45.3,
    "last_attempt_at": 1794255470.0,
    "next_reset_at": 1794257037.6,
    "throughput_per_hour": 180
  },
  "scores": {
    "potential_mean": 61.2,
    "measured_mean": 74.5
  },
  "updated_at": 1794255480.0
}
```

`tested_pct = tested / total * 100`. `terminal_pct` inclut `skipped`.

---

## 2. `GET /api/egress/qualification/queue`

Query :

| Param | Type | Défaut | Description |
|---|---|---|---|
| `status` | string | `pending` | `pending`, `usable`, `quota`, `auth`, `dead`, `skipped`, `claimed` |
| `country` | string | | filtre pays |
| `transport` | string | | `wireguard`, `openvpn-udp`, `openvpn-tcp` |
| `limit` | int | 50 | max 500 |
| `offset` | int | 0 | pagination |
| `order` | string | `potential` | `potential`, `rtt`, `load`, `hostname` |

Réponse 200 :

```json
{
  "items": [
    {
      "hostname": "se426.nordvpn.com",
      "country": "Sweden",
      "city": "Stockholm",
      "load": 12,
      "transports": ["wireguard", "openvpn-udp"],
      "status": "pending",
      "potential_score": 88.1,
      "measured_score": 0,
      "quota_remaining": 500,
      "quota_reset_at": null,
      "rtt_ms": 24.2,
      "attempts": 0,
      "last_attempt_at": null,
      "last_result": "",
      "next_eligible_at": 0,
      "source_id": "nordvpn_api_servers"
    }
  ],
  "total": 3180,
  "limit": 50,
  "offset": 0
}
```

---

## 3. `GET /api/egress/qualification/next?limit=20`

Réponse 200 : même schéma que `queue`, mais uniquement les candidats
sélectionnables (portes dures passées), triés par l'ordre de la file.

```json
{
  "items": [],
  "limit": 20,
  "reason_empty": "all-quota"
}
```

`reason_empty` : `all-quota`, `all-claimed`, `all-auth`, `paused`,
`epoch-complete`.

---

## 4. `GET /api/egress/qualification/epochs`

Réponse 200 :

```json
{
  "items": [
    {
      "epoch_id": "epoch-20261109",
      "status": "running",
      "total": 7070,
      "tested": 3200,
      "usable": 42,
      "quota": 2100,
      "infra": 900,
      "dead": 12,
      "skipped": 146,
      "started_at": 1794255478.83,
      "finished_at": null,
      "duration_s": 3600
    }
  ],
  "total": 1
}
```

## 5. `GET /api/egress/qualification/epochs/{epoch_id}`

Même objet que ci-dessus + `cursor`, `reason`, et les 20 derniers essais.

---

## 6. `GET /api/egress/qualification/attempts`

Query : `hostname`, `status`, `epoch_id`, `station`, `limit` (défaut 50, max
500).

Réponse 200 :

```json
{
  "items": [
    {
      "attempt_id": "att_...",
      "epoch_id": "epoch-20261109",
      "hostname": "se426.nordvpn.com",
      "station": 1,
      "transport": "wireguard",
      "status": "200",
      "egress_ip": "185.68.191.59",
      "latency_ms": 42.3,
      "rtt_ms": 24.2,
      "reset_at": null,
      "error": "",
      "started_at": 1794255470.0,
      "finished_at": 1794255472.0
    }
  ],
  "total": 1
}
```

---

## 7. `GET /api/egress/qualification/quota`

Réponse 200 :

```json
{
  "items": [
    {
      "ip": "185.68.191.59",
      "provider": "nordvpn",
      "used": 120,
      "quota_estimate": 500,
      "remaining": 380,
      "successes": 118,
      "failures": 2,
      "reset_at": null,
      "state": "fresh"
    }
  ],
  "total": 4871
}
```

---

## 8. `GET /api/egress/qualification/stations`

Réponse 200 :

```json
{
  "items": [
    {
      "station": 1,
      "state": "serving",
      "current_egress_ip": "185.68.191.59",
      "current_transport": "wireguard",
      "standby_egress_ip": "87.120.102.85",
      "standby_transport": "wireguard",
      "client_usable": true,
      "last_probe_at": 1794255470.0,
      "last_200_at": 1794255400.0,
      "cutover_ms": 0,
      "availability_1h": 99.4,
      "availability_24h": 99.1,
      "last_reason": ""
    }
  ],
  "six_of_six": true,
  "updated_at": 1794255480.0
}
```

---

## 9. `GET /api/egress/qualification/watchdog`

Réponse 200 :

```json
{
  "heartbeat_at": 1794255478.0,
  "stale": false,
  "stations": [
    {
      "station": 1,
      "state": "usable",
      "bad_probes": 0,
      "last_probe_at": 1794255470.0,
      "last_error": "",
      "repair_attempts": 0
    }
  ],
  "incidents": [
    {
      "id": 162,
      "scope": "station",
      "station": 2,
      "cause": "tunnel_down",
      "started_at": 1794255000.0,
      "ended_at": 1794255030.0,
      "remediation": "rebind",
      "result": "ok"
    }
  ]
}
```

---

## 10. `GET /api/egress/qualification/discovery`

Réponse 200 :

```json
{
  "sources": [
    {
      "source_id": "nordvpn_api_servers",
      "kind": "api",
      "fetched_at": 1794255000.0,
      "stale": false,
      "server_count": 7070,
      "transport_count": 6932,
      "last_ok_at": 1794255000.0,
      "error": ""
    }
  ],
  "runs": [
    {
      "id": "disc_...",
      "source": "nordvpn_api_servers",
      "started_at": 1794255000.0,
      "finished_at": 1794255006.0,
      "status": "ok",
      "server_count": 7070,
      "transport_count": 6932,
      "stale": false,
      "error": ""
    }
  ]
}
```

---

## 11. `GET /api/egress/qualification/repairs`

Réponse 200 :

```json
{
  "items": [
    {
      "id": 1,
      "station": 2,
      "cause": "tunnel_down",
      "status": "running",
      "attempts": 1,
      "started_at": 1794255000.0,
      "finished_at": null,
      "transports_tried": ["wireguard", "openvpn-tcp"],
      "result": "",
      "last_error": ""
    }
  ],
  "total": 1
}
```

---

## 12. Mutations

### `POST /api/egress/qualification/epochs`

Body :

```json
{"epoch_id": "epoch-20261109", "max": 0, "station": 0, "shadow": false}
```

Réponse 202 : `{"epoch_id": "...", "status": "running"}`.

### `POST /api/egress/qualification/epochs/{epoch_id}/pause`

Réponse 200 : `{"epoch_id": "...", "status": "paused"}`.

### `POST /api/egress/qualification/epochs/{epoch_id}/resume`

Réponse 200 : `{"epoch_id": "...", "status": "running", "cursor": "..."}`.

### `POST /api/egress/qualification/rotate`

Body : `{"station": 1, "reason": "manual"}`.
Réponse 200 : `{"station": 1, "option": "...", "egress_ip": "...", "score": 87.2}`.
Toute rotation manuelle est tracée dans `qualification_attempts` avec
`status='manual-rotate'` et l'opérateur.

---

## 13. Erreurs

| Code | Cas |
|---|---|
| 400 | paramètre invalide, limite > 500, epoch inconnue |
| 404 | hostname/epoch absent |
| 409 | époque déjà `running` |
| 429 | trop de rotations manuelles (limite `rebind_limit_per_min`) |
| 503 | store indisponible ; la file précédente reste lisible |

---

## 14. Invariants de contrat

1. Aucun endpoint ne retourne une option `dead` comme sélectionnable.
2. `summary.total` = somme des statuts de la file.
3. `next` ne retourne jamais un hostname avec `reset_at` futur.
4. `quota.remaining` est dérivé du ledger, jamais d'une estimation locale.
5. Les timestamps sont Unix seconds (float).
6. Aucun secret, cookie ou clé n'apparaît dans une réponse.
