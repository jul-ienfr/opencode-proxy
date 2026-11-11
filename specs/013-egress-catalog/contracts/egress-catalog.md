# Contract — Catalogue d'egress

## C1 — Configuration

```yaml
egress_catalog:
  enabled: true
  update_jitter_pct: 15
  retention_days: 7
  denylist:
    - 103.26.10.23/32
  threat_feeds:
    enabled: true
    refresh_interval_s: 21600
    urls:
      - id: abuse_feodo
        url: https://feodotracker.abuse.ch/downloads/ipblocklist.txt
        enabled: true
  providers:
    public:
      enabled: true
      discovery_interval_s: 21600
      revalidate_interval_s: 1800
      max_active: 64
      max_catalog: 20000
      concurrency: 24
      ttl_usable_s: 43200
      ttl_quota_max_s: 86400
      ttl_dead_s: 604800
      sources_file: config/egress_sources.yaml
    warp:
      enabled: true
      discovery_interval_s: 1800
      max_options: 24
      ttl_usable_s: 43200
      ttl_quota_max_s: 86400
    nordvpn:
      enabled: true
      discovery_interval_s: 1800
      max_options: 10
      max_active_stations: 6
      ttl_usable_s: 43200
      ttl_quota_max_s: 86400
```

Chaque clé a un consommateur runtime et un test (constitution XII).

## C2 — API

### `GET /api/egress/catalog`

```json
{
  "enabled": true,
  "providers": {
    "public": {"known": 64, "usable": 12, "quota": 40, "dead": 12, "unique_blocks": 12, "last_run_at": 1794220000},
    "warp": {"known": 12, "usable": 12, "quota": 0, "dead": 0, "unique_blocks": 12, "last_run_at": 1794220000},
    "nordvpn": {"known": 6, "usable": 2, "quota": 4, "auth": 0, "dead": 0, "unique_blocks": 6, "last_run_at": 1794220000}
  },
  "last_run": {"id": "run_...", "status": "ok", "started_at": 1794220000, "finished_at": 1794220030}
}
```

### `GET /api/egress/catalog/options?provider=public&status=usable&limit=100`

```json
{
  "options": [
    {
      "key": "public|https|1.2.3.4:443|1.2.3.4",
      "provider": "public",
      "kind": "https",
      "host": "1.2.3.4",
      "port": 443,
      "egress_ip": "1.2.3.4",
      "block": "1.2.3.0/24",
      "status": "usable",
      "last_checked_at": 1794220000,
      "expires_at": 1794263200,
      "reset_at": 0,
      "last_latency_ms": 1200
    }
  ]
}
```

### `POST /api/egress/catalog/threat-feeds/refresh`

Protégé par `X-Dashboard-Token`. Recharge les flux threat-intel, met à jour
`egress_threat_intel` et invalide le cache mémoire. Réponse :

```json
{"ok": true, "counts": {"abuse_feodo": 5, "spamhaus_drop": 1763}, "errors": [], "stats": {"exact": 136055, "networks": 6400}}
```

### `POST /api/egress/catalog/refresh`

Protégé par `X-Dashboard-Token` quand configuré.

```json
{"provider": "public"}
```

Réponse `202` :

```json
{"ok": true, "job_id": "refresh_public_1794220000", "status": "running"}
```

### `GET /api/egress/catalog/runs?limit=20`

Liste des cycles, avec compteurs et erreurs bornées.

### `POST /api/egress/catalog/block`

Protégé par token. Ajoute une IP/CIDR à `egress_catalog.denylist`, retire les
options correspondantes du catalogue et resynchronise le pool public.

```json
{"ip": "103.26.10.23"}
```

### `POST /api/egress/catalog/sources`

Crée/met à jour une source. Mêmes champs que la config ; validation du type et
de l'URL.

## C3 — Gates durs

Une option n'est sélectionnable que si :

1. `provider` demandé ;
2. `status == usable` ;
3. `expires_at > now` ;
4. `reset_at <= now` ;
5. `egress_ip`/`block` non vide ;
6. aucun claim d'IP actif sur une autre station (constitution V) ;
7. cap du provider non dépassé.

Le score (constitution IX) ordonne ensuite les options éligibles.

## C4 — Promotion

- `public` : `200` free avec contenu → `usable` ; `429` → `quota` avec
  `reset_at` ; timeout/403/5xx → `dead`.
- `warp` : conteneur healthy + egress IPv6 + `200` → `/64` `usable` ; un
  nouveau `/64` remplace l'ancien (expiration) ; 429 → `quota`.
- `nordvpn` : station connectée + IP + `200` → `usable` ; 429 → `quota` ;
  AUTH_FAILED → toutes les IP de la station `auth`.

## C5 — Sécurité

- Aucune sonde publique ne traverse le processus principal : worker isolé.
- Aucun cookie/clé/token dans les observations ou les options.
- Les URLs de worker avec token ne sont jamais exposées par l'API/UI.
- Un 429 ne doit jamais être converti en `dead` ni en `200`.

## C6 — Observabilité

- `catalog_runs` conserve le résultat de chaque cycle.
- `egress_observations` conserve la preuve brute (statut, latence, message
  borné).
- L'API ne retourne jamais une option sans `last_checked_at`/`expires_at`.
- Une option expirée est visible avec `status=expired` mais jamais sélectionnée.
