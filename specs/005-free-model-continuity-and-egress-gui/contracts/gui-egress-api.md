# Contrat — API GUI free-models / free-egress

**Statut**: conception. Aucun code dans cette itération.

## `GET /api/free-models` (extension)

Champs ajoutés à la réponse existante :

```json
{
  "active_default": "deepseek-v4.1-flash-free",
  "default_source": "auto",
  "default_previous": "mimo-v2.6-flash-free",
  "last_switch": {
    "at": "2026-11-06T08:00:00Z",
    "reason": "missing",
    "from": "mimo-v2.6-flash-free",
    "to": "deepseek-v4.1-flash-free"
  },
  "pinned": false,
  "no_free_model": false
}
```

## `PUT /api/free-models/default`

Requête :

```json
{"mode": "auto"}
```

ou

```json
{"mode": "pin", "target": "deepseek-v4.1-flash-free"}
```

Réponse : `ModelSelection` (target, source, previous, reason, changed_at).

Erreurs : `400 invalid_target`, `409 pinned_unavailable`,
`503 catalog_unavailable`.

## `POST /api/free-models/reconcile`

Force la politique ; réponse `ModelSelection` + `changed: bool`.

## `GET /api/free-egress`

```json
{
  "desired": {
    "mode": "warp",
    "station_count": 5,
    "managed": true
  },
  "actual": {
    "mode": "warp",
    "station_count": 5,
    "healthy": 5,
    "provisioning": 0,
    "failed": 0
  },
  "stations": [
    {
      "label": "warp-ipv6-2",
      "mode": "warp",
      "host": "127.0.0.1",
      "port": 11085,
      "egress_ip": "2a09:bac1:27e0:2060::219:14",
      "block": "2a09:bac1:27e0:2060::/64",
      "state": "healthy",
      "last_error": null,
      "last_success_at": "2026-11-06T08:04:00Z",
      "identity_ok": true,
      "profile_id": "oc-2.2.5-cli-13",
      "tls_family": "bun-1.4.4",
      "shared_profile": false
    }
  ],
  "identity_audit": {
    "status": "ok",
    "duplicates": [],
    "leaks": [],
    "shared_vectors": ["tls_ja3", "ua", "client", "bearer"]
  },
  "limits": {
    "vpn_max": 6,
    "warp_max": 10,
    "warp_registration_throttled": false
  }
}
```

## `POST /api/free-egress/plan`

Requête :

```json
{"mode": "warp", "station_count": 6}
```

Réponse :

```json
{
  "create": [{"label": "warp-ipv6-5", "port": 11088}],
  "keep": ["warp-ipv4", "warp-ipv6", "warp-ipv6-2", "warp-ipv6-3", "warp-ipv6-4"],
  "remove": [],
  "warnings": []
}
```

## `PUT /api/free-egress`

Persiste l'état désiré et lance l'apply. Réponse `202` avec `run_id`, ou
`200` si aucun delta.

```json
{"mode": "vpn", "station_count": 5}
```

Erreurs :

| HTTP | Code | Cas |
|---:|---|---|
| 400 | `invalid_mode` / `invalid_count` | mode ou count hors limites |
| 409 | `provisioning_busy` | apply déjà en cours |
| 409 | `provider_limit` | au-delà de la limite VPN/WARP |
| 422 | `plan_invalid` | port occupé, Docker absent |
| 500 | `apply_failed` | échec, rollback effectué |
| 503 | `docker_unavailable` | Docker non joignable |

## `GET /api/free-egress/identity-audit`

```json
{
  "status": "ok",
  "stations": [
    {"label": "warp-ipv6", "project": "…", "session_prefix": "ses_…", "trace_prefix": "…"}
  ],
  "duplicates": [],
  "leaks": [],
  "shared_vectors": ["tls_ja3", "ua", "client", "bearer"],
  "checked_at": "2026-11-06T08:10:00Z"
}
```

## `GET /api/free-egress/profiles`

```json
{
  "schema": 1,
  "updated_at": "2026-11-06T10:00:00Z",
  "profiles": [
    {
      "profile_id": "oc-2.2.5-cli-13",
      "version": "2.2.5",
      "client": "cli",
      "tls_family": "bun-1.4.4",
      "status": "verified",
      "verified_at": "2026-11-06T10:00:00Z",
      "expires_at": "2026-11-13T10:00:00Z",
      "evidence_count": 2,
      "shared_by": []
    }
  ],
  "limits": {
    "tls_families": 2,
    "verified_profiles": 2
  }
}
```

## `POST /api/free-egress/profiles/verify`

```json
{"profile_id": "oc-2.2.5-cli-13"}
```

Réponse `202` avec `run_id`, puis `GET /profiles` montre `verified`,
`candidate` ou `rejected`. Erreurs : `400 unknown_profile`, `409 busy`,
`422 no_fresh_block`, `503 registry_unavailable`.

## GUI

### FreeModels

- bandeau « Modèle free actif » avec `active_default`, `source`,
  `last_switch` ;
- bouton `Reconcile` ;
- sélecteur `Auto` / `Pin` + choix dans la liste `FREE_MODELS`.

### FreeEgress

- radio `VPN (NordVPN)` / `WARP` ;
- nombre de stations 1–10, limites affichées ;
- bouton `Voir le plan` (dry-run) puis `Appliquer` ;
- table des stations : label, port, egress/bloc, état, dernière erreur,
  `identity_ok`, `profile_id`, `tls_family`, `shared_profile` ;
- bandeau audit : `ok` / `duplicate` / `prompt_leak` / `shared_session` ;
- badge profils : `distinct` / `shared` / `stale` / `rejected`.
- pendant l'apply : progression par station, bouton désactivé, `409 busy`
  expliqué.

### Règles UI

- Ne jamais masquer un `provider_limit` : afficher la limite mesurée.
- Ne jamais afficher un modèle comme « actif » s'il est `dead`.
- Ne jamais afficher `direct` comme actif s'il est `enabled: false`.
- Le mode `socks5`/`direct` reste dans une section « avancé ».

## Contrat « option → config → consommateur → effet → test » (constitution XII)

Chaque contrôle de la GUI a un consommateur runtime et un test. Aucune option
inerte.

| Option GUI | Clé config | Consommateur | Effet observable | Test |
|---|---|---|---|---|
| radio `VPN` | `free_egress.mode: vpn` | `FreeEgressController` → `VpnProvisioner` | stations gluetun créées/vérifiées, mode affiché | apply `vpn × 5` → 5 stations |
| radio `WARP` | `free_egress.mode: warp` | `FreeEgressController` → `WarpProvisioner` | conteneurs WARP labellisés, `/64` distincts | apply `warp × 3` → 3 blocs |
| nombre 1–10 | `free_egress.station_count` | `EgressPlanner` + provisioner | delta create/remove, plan cohérent | `0→3`, `3→3`, `5→3` |
| `Voir le plan` | — (dry-run) | `EgressPlanner` | plan create/keep/remove/verify affiché | aucun conteneur modifié après dry-run |
| `Appliquer` | `free_egress.*` persisté | `FreeEgressController.apply` | convergence, `run_id`, progression | échec 3ᵉ station → rollback |
| `Auto` / `Pin` | `free_discovery.pinned_target` | `FreeModelContinuity` | target conservé/remplacé, source affichée | pin `dead` → `pinned_unavailable` |
| `Reconcile` | — (lecture) | `FreeModelContinuity` | `ModelSelection` + `changed` | cible absente → remplaçant `alive` |
| badge audit | — (lecture) | `IdentityAudit` | `ok`/`duplicate`/`prompt_leak`/`shared_session` | 2 stations même project → `duplicate` |
| section avancée `socks5`/`direct` | `free_egress.mode` | `PoolBinder` / config | mode réel affiché, `direct` désactivé jamais sélectionné | `direct enabled: false` → aucun `socks5:direct` |
