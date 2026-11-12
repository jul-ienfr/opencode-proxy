# Contrat — API de contrôle Gluetun

**Version** : `1.0.0`
**Feature** : `016-gluetun-control-plane`
**Base URL** : `http://<station>:8000/v1` (réseau Docker interne uniquement)
**Content-Type** : `application/json; charset=utf-8` (sauf SSE et `PUT`
  `/v1/vpn/settings` en mode legacy)

---

## 1. Conventions

### 1.1 Authentification et scopes

Trois scopes, portés par les rôles existants (`apikey` ou `basic`) :

| Scope | Droits |
|---|---|
| `reader` | Toutes les lectures (`GET`), y compris `/v1/events`. |
| `controller` | `reader` + `switch`, `rotate`, `rollback`, `dns/probe`, `PUT settings`. |
| `admin` | `controller` + `jobs/{id}/cancel`, `servers/refresh`, `transport/reset`, `reveal=true`, `supersede`. |

- En-tête : `Authorization: Bearer <api_key>` ou Basic.
- Scope insuffisant ⇒ `403 forbidden_scope`.
- Route inconnue ⇒ `404 not_found`.
- Méthode non supportée ⇒ `405 method_not_allowed`.

Les routes existantes gardent leurs rôles ; les nouvelles routes sont ajoutées à
`validRoutes` avec leur scope. Aucun scope ne permet de lire un secret sans
`reveal=true` **et** `admin`.

### 1.2 Identifiants et temps

- `job_id` : ULID préfixé `job_` (ex. `job_01J9Z3K4T5`).
- `seq` : entier monotone par station, jamais réutilisé.
- Dates ISO-8601 UTC (`2026-11-11T18:10:06.412Z`).
- Durées en millisecondes (`timings_ms`) ou secondes (`timeout_s`, `ttl_s`).

### 1.3 Enveloppe d'erreur

```json
{
  "error": {
    "code": "unknown_target",
    "message": "hostname nl-9999 not found in provider catalog",
    "details": {"field": "target.hostname"},
    "job_id": null,
    "retryable": false
  }
}
```

| HTTP | `code` | Sens |
|---|---|---|
| 400 | `invalid_request` | Corps mal formé, champ inconnu, type invalide. |
| 400 | `missing_field` | Champ requis absent. |
| 401 | `unauthorized` | Credential absent ou invalide. |
| 403 | `forbidden_scope` | Scope insuffisant. |
| 404 | `not_found` | Route, serveur ou job inconnu. |
| 405 | `method_not_allowed` | Méthode non supportée. |
| 409 | `active_job` | Un job est déjà actif (`active_job_id`). |
| 409 | `idempotency_conflict` | Même clé, corps différent. |
| 409 | `secret_persistence_unavailable` | Secret modifié sans `CONTROL_SECRET_KEY`. |
| 422 | `unknown_target` | Cible absente du catalogue. |
| 422 | `invalid_target` | Combinaison incohérente (pays + hostname, etc.). |
| 422 | `endpoint_ip_mismatch` | IP d'endpoint absente des IPs du hostname. |
| 422 | `insufficient_sources` | Vérification impossible (trop peu de sources). |
| 429 | `rate_limited` | Trop de bascules ; `Retry-After` présent. |
| 500 | `internal_error` | Erreur inattendue, sans secret. |
| 503 | `not_ready` | Tunnel/settings pas encore initialisés. |

`retryable=true` uniquement pour 429 et 503.

### 1.4 Pagination et filtres

- `limit` (défaut 50, max 500), `cursor` opaque.
- Réponse : `{"items":[…],"next_cursor":"…|null","total_estimate":123}`.
- Filtres catalogue : `country`, `region`, `city`, `isp`, `category`, `tier`,
  `q`, `protocol`, `free_only`, `owned_only`, `stream_only`.

### 1.5 Idempotence

- En-tête `Idempotency-Key` (≤ 128 caractères) sur `POST /v1/vpn/switch` et
  `/rotate`.
- Fenêtre 24 h ; même clé + même corps ⇒ `200` avec le job existant.
- Même clé + corps différent ⇒ `409 idempotency_conflict`.
- Absence de clé ⇒ comportement non idempotent documenté.

### 1.6 Mode synchrone/asynchrone

- Défaut asynchrone : `202 Accepted` + `Location: /v1/vpn/jobs/{id}`.
- `wait=true` (ou `?wait=true`) : bloque jusqu'à l'état terminal ou
  `timeout_s` (défaut 45 s), puis `200` (terminal) ou `202` (encore actif).
- `dry_run=true` : validation + préflight + plan, aucune coupure ; retourne
  `200` avec `state=planned`.

## 2. Capacités

### `GET /v1/control/capabilities` — reader

```json
{
  "control_plane_version": "1.0.0",
  "upstream_version": "v3.41.3",
  "station_id": "vpn-gluetun-2",
  "features": {
    "switch": true, "rotate": true, "rollback": true, "dry_run": true,
    "drain": true, "dns_ladder": true, "egress_consensus": true,
    "transport_ladder": true,
    "events_sse": true, "state_persistence": true, "secret_reveal": true
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
    "transport_backoff_max_s": 300,
    "idempotency_window_h": 24
  },
  "state_mode": "state",
  "dns_default_ladder": ["dot", "plain"],
  "transport_default_ladder": ["wireguard", "openvpn-udp", "openvpn-tcp"]
}
```

## 3. État et settings

### `GET /v1/vpn/state` — reader

```json
{
  "station_id": "vpn-gluetun-2",
  "vpn_status": "running",
  "transport": "wireguard",
  "server": {"hostname": "nl-1234.nordvpn.com", "country": "Netherlands", "city": "Amsterdam"},
  "endpoint_ip": "203.0.113.10",
  "egress": {"verified": true, "ip": "198.51.100.7", "quorum": 2, "verified_at": "…"},
  "dns": {"ladder": ["dot", "plain"], "active_tier": "dot", "fail_closed": false},
  "desired": {"hostname": "nl-1234.nordvpn.com", "transport": "wireguard"},
  "last_committed": {"job_id": "job_01J9…", "at": "…"},
  "active_job_id": null,
  "state_mode": "state",
  "updated_at": "2026-11-11T18:10:06Z"
}
```

`GET /v1/vpn/state` ne lit jamais le disque ; il sert l'état en mémoire
(p95 < 100 ms).

### `GET /v1/vpn/settings` — reader

Réponse : settings **masqués** par défaut.

```json
{
  "vpn": {"type": "wireguard", "provider": "nordvpn"},
  "server_selection": {"countries": ["Netherlands"], "hostnames": []},
  "dns": {"upstream_type": "dot", "providers": ["cloudflare"]},
  "secrets": {
    "wireguard_private_key": "***",
    "openvpn_password": "***",
    "provider_token": "***"
  },
  "revealed": false
}
```

`?reveal=true` (admin) retourne les valeurs réelles ; l'appel est journalisé
(`secret.revealed`) sans jamais écrire la valeur dans les logs.

### `PUT /v1/vpn/settings` — controller (compatibilité)

Deux modes :

1. **Legacy** (aucun champ nouveau) : comportement upstream conservé ; réponse
   `text/plain` (`outcome`), stop/start.
2. **Contrôlé** (`dry_run`, `wait`, `require_verified` ou `target` présents) :
   le corps est converti en `TunnelTarget` et routé vers le SwitchJob.

```json
{
  "target": {"hostname": "nl-1234.nordvpn.com"},
  "dry_run": false,
  "wait": true,
  "require_verified": true,
  "timeout_s": 45
}
```

Réponse en mode contrôlé : `SwitchJob` (voir §4). Un client legacy qui envoie un
`settings.VPN` brut continue de fonctionner.

## 4. Switch

### `POST /v1/vpn/switch` — controller

Requête :

```json
{
  "target": {
    "hostname": "nl-1234.nordvpn.com",
    "endpoint_ip": "203.0.113.10",
    "expected_egress_ip": "198.51.100.7"
  },
  "constraints": {
    "require_new_ip": true,
    "require_new_subnet": false,
    "require_verified": true,
    "exclude_ips": [],
    "exclude_subnets": [],
    "forbidden_ips": []
  },
  "options": {
    "dry_run": false,
    "wait": false,
    "timeout_s": 45,
    "transport_policy": "auto",
    "drain": {"enabled": false, "timeout_s": 5},
    "supersede": false,
    "verify": {"quorum": 2, "min_sources": 3, "canary": true}
  },
  "idempotency_key": "switch-s2-1731"
}
```

Réponses :

| Cas | HTTP | Corps |
|---|---|---|
| Accepté (async) | 202 | `SwitchJob` + `Location` |
| Terminé (`wait=true`) | 200 | `SwitchJob` terminal |
| Cible identique, `require_new_ip=false` | 200 | `{"state":"noop","job":null}` |
| Cible inconnue | 422 | erreur `unknown_target` |
| Job actif | 409 | erreur `active_job` + `active_job_id` |
| `dry_run` | 200 | `{"state":"planned","plan":{…}}` |

`dry_run` retourne :

```json
{
  "state": "planned",
  "plan": {
    "target": {"hostname": "nl-1234.nordvpn.com", "endpoint_ip": "203.0.113.10"},
    "resolved_ips": ["203.0.113.10"],
    "transport": "wireguard",
    "transport_policy": "auto",
    "estimated_phases_ms": {"validating": 20, "preflight": 250, "applying": 4000},
    "warnings": []
  },
  "would_cut_tunnel": true
}
```

`options.transport_policy` accepte `auto` (défaut, ladder `TRANSPORT_LADDER`),
un transport unique (`wireguard`, `openvpn-udp`, `openvpn-tcp`) ou une liste
ordonnée. Il ne change jamais la cible serveur/IP. `VPN_TYPE` explicite dans les
settings reste prioritaire et désactive le ladder.

### `POST /v1/vpn/rollback` — controller

```json
{"to": "last_committed", "wait": true, "timeout_s": 45}
```

`to` ∈ `last_committed` (défaut) ou un `snapshot_id` explicite (admin). Réponse :
`SwitchJob` avec `kind=rollback`. Si aucun snapshot : `409 no_snapshot`.

### `POST /v1/vpn/jobs/{id}/cancel` — admin

Réponse `200` : job avec `state=cancelled` ou `cancel_requested=true` si la phase
en cours n'est pas interruptible (applying/rolling_back). Aucune annulation ne
laisse le tunnel sans état : le rollback se poursuit.

### `GET /v1/vpn/jobs` — reader

Paramètres : `state`, `kind`, `since`, `limit`, `cursor`. Tri : plus récent
d'abord.

### `GET /v1/vpn/jobs/{id}` — reader

Retourne le `SwitchJob` complet (candidats, tentatives, timings, preuves,
snapshot avant/après). Les secrets ne sont jamais inclus.

## 5. Rotation

### `POST /v1/vpn/rotate` — controller

```json
{
  "candidates": [
    {"hostname": "nl-1234.nordvpn.com"},
    {"hostname": "nl-5678.nordvpn.com"},
    {"hostname": "nl-9012.nordvpn.com"}
  ],
  "policy": null,
  "constraints": {
    "require_new_ip": true,
    "require_new_subnet": true,
    "exclude_hostnames": ["nl-9999.nordvpn.com"],
    "exclude_ips": ["198.51.100.9"],
    "exclude_subnets": ["198.51.100.0/24"],
    "max_candidates": 20,
    "max_attempts": 10
  },
  "options": {
    "wait": false,
    "timeout_s": 120,
    "transport_policy": ["wireguard", "openvpn-udp", "openvpn-tcp"],
    "verify": {"quorum": 2, "min_sources": 3, "canary": true}
  },
  "idempotency_key": "rotate-s2-1731"
}
```

- `candidates` **ordonnés** : le fork ne les re-trie pas et ne les re-score pas.
- `policy` (alternatif) : `countries/cities/categories/tiers/mode` ; le fork
  résout une liste **dans l'ordre du catalogue**, sans score.
- Si `candidates` et `policy` sont fournis : `422 invalid_target`.

Réponse : `SwitchJob` `kind=rotate`. États terminaux : `committed` (premier
candidat prouvé), `exhausted` (budget épuisé), `cancelled`, `failed_degraded`
(rollback impossible).

Exemple `exhausted` :

```json
{
  "id": "job_01J9…",
  "kind": "rotate",
  "state": "exhausted",
  "attempts": [
    {"hostname": "nl-1234.nordvpn.com", "result": "rejected", "reason": "preflight_failed"},
    {"hostname": "nl-5678.nordvpn.com", "result": "rejected", "reason": "egress_mismatch"},
    {"hostname": "nl-9012.nordvpn.com", "result": "rejected", "reason": "same_subnet"}
  ],
  "station_preserved": true,
  "global_state_written": false
}
```

## 6. Serveurs

### `GET /v1/servers` — reader

Filtres §1.4. Chaque item : `ServerCandidate` (sans clé privée).

### `GET /v1/servers/{hostname}` — reader

`ServerCandidate` complet + `stats` + `available_transports`.

### `GET /v1/servers/facets` — reader

```json
{
  "countries": [{"value": "Netherlands", "count": 420}],
  "regions": [{"value": "North Holland", "count": 30}],
  "cities": [{"value": "Amsterdam", "count": 12}],
  "isps": [{"value": "M247", "count": 40}],
  "categories": [{"value": "standard", "count": 380}],
  "tiers": [{"value": "free", "count": 90}]
}
```

### `POST /v1/servers/refresh` — admin

```json
{"provider": "nordvpn", "force": false}
```

Rate-limité (1/min). Réponse `202` + `{"refreshed_at": "…", "servers": 5432}`.

## 7. Egress

### `GET /v1/egress` — reader

Dernière `EgressVerification` (cache). `?fresh=true` force une collecte.

### `GET /v1/egress/verify` — reader

Paramètres : `fresh`, `quorum`, `min_sources`, `canary`, `timeout_s`. Réponse :
`EgressVerification` (`200`), `422 insufficient_sources` si la collecte est
impossible.

### `GET /v1/egress/canary` — reader

```json
{
  "url": "https://canary.invalid/health",
  "status": 200,
  "ok": true,
  "body_bytes": 128,
  "body_sha256": "…",
  "latency_ms": 190,
  "failure": null,
  "checked_at": "2026-11-11T18:10:06Z"
}
```

`200` avec corps vide ⇒ `ok=false`, `failure="empty_body"`.

### `GET /v1/egress/history` — reader

`limit` (défaut 50, max 500), `since`. Items : `EgressVerification` sans
observations brutes complètes (résumé + dissidents).

## 8. DNS

### `GET /v1/dns/state` — reader

```json
{
  "ladder": ["dot", "plain"],
  "active_tier": "dot",
  "fail_closed": false,
  "tiers": [
    {"type": "dot", "state": "up", "latency_ewma_ms": 38},
    {"type": "plain", "state": "standby", "latency_ewma_ms": 0}
  ],
  "updated_at": "…"
}
```

### `GET /v1/dns/tiers` — reader

Détail complet `ResolverTier[]` + `ResolverEndpoint[]`.

### `POST /v1/dns/probe` — controller

```json
{"tier": "dot", "domain": "example.com", "timeout_s": 3}
```

Réponse : `{"tier":"dot","ok":true,"latency_ms":42,"rcode":"NOERROR","answers":1}`.
Une sonde ne modifie pas la santé si `dry_run=true`.

## 8bis. Ladder de transports

### `GET /v1/vpn/transport` — reader

```json
{
  "active_transport": "openvpn-tcp",
  "policy": {"ladder": ["wireguard", "openvpn-udp", "openvpn-tcp"]},
  "phase": "committed",
  "cycle": 1,
  "breaker": {
    "wireguard": {"state": "open", "failures": 3, "retry_at": "2026-11-11T18:12:31Z"},
    "openvpn-udp": {"state": "open", "failures": 3, "retry_at": "2026-11-11T18:12:31Z"},
    "openvpn-tcp": {"state": "closed", "failures": 0}
  },
  "attempts": [
    {"transport": "wireguard", "result": "failed", "reason": "handshake_timeout"},
    {"transport": "openvpn-udp", "result": "failed", "reason": "udp_blocked"},
    {"transport": "openvpn-tcp", "result": "success", "reason": ""}
  ],
  "next_attempt_at": null,
  "updated_at": "2026-11-11T18:10:40Z"
}
```

### `POST /v1/vpn/transport/reset` — admin

```json
{"transport": "wireguard"}
```

Réponse : `{"transport":"wireguard","breaker":"closed","reset":true}`.
`transport` absent ⇒ réarme tous les breakers. Ne coupe pas le tunnel actif.

Contrat détaillé : `contracts/transport-ladder.md`.

## 9. Événements

### `GET /v1/events` — reader

Paramètres : `since` (seq), `type`, `limit`. Réponse :

```json
{"items":[{"seq":1042,"ts":"…","type":"job.committed","severity":"info","job_id":"job_01J9…","data":{}}],"next_seq":1043}
```

### `GET /v1/events/stream` — reader (SSE)

- `Content-Type: text/event-stream`.
- En-tête `Last-Event-ID` supporté ; rejoue depuis `seq+1`.
- Heartbeat `: keepalive` toutes les 15 s.
- Format :

```text
id: 1042
event: job.committed
data: {"seq":1042,"ts":"…","type":"job.committed","data":{…}}
```

- Le flux ne contient jamais de secret.
- Déconnexion : le client reprend avec `Last-Event-ID`.

## 10. Compatibilité

| Route existante | Comportement 016 |
|---|---|
| `GET /version` | Inchangé. |
| `GET /v1/vpn/status` | Inchangé. |
| `PUT /v1/vpn/status` | Inchangé ; un job actif ⇒ `409 active_job`. |
| `GET /v1/vpn/settings` | Masqué par défaut ; `reveal=true` admin. |
| `PUT /v1/vpn/settings` | Legacy si aucun champ nouveau ; sinon SwitchJob. |
| `GET/PUT /v1/dns/status` | Inchangé ; le ladder est une couche au-dessus. |
| `GET /v1/vpn/transport` | Nouveau ; état du ladder de transports. |
| `POST /v1/vpn/transport/reset` | Nouveau ; admin, réarme un breaker. |
| `GET /v1/publicip/ip` | Inchangé ; l'egress vérifié est exposé séparément. |
| `/v1/openvpn/*`, `/v1/updater/*`, `/v1/portforward/*` | Inchangés. |

Les tests de contrat upstream doivent passer avant et après.

## 11. Limites et sécurité

- Corps ≤ 1 MiB ; au-delà `413 payload_too_large`.
- `POST /v1/vpn/switch` et `/rotate` : ≤ 6/min/station ; au-delà `429`.
- Timeouts : validation 5 s, préflight 10 s, application 30 s, tunnel 30 s,
  vérification 10 s, total borné par `timeout_s`.
- Aucun log de credential, clé API, token ou corps de settings révélé.
- Toute réponse d'erreur est assainie (pas de stack trace).
- Les routes ne sont exposées que sur le réseau Docker interne ; le port public
  reste le proxy, pas le control server.
