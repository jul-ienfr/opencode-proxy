# Contract — Status API

## `/api/vpn-status`

Ajouts :

```json
{
  "proxy_mode": "auto_egress",
  "selected_providers": ["warp", "nordvpn"],
  "derived_mode": "auto_egress",
  "auto_egress": {
    "enabled": true,
    "degraded": false,
    "reason": null,
    "providers": [
      {
        "name": "warp",
        "enabled": true,
        "available": true,
        "endpoints": 6,
        "inflight": 2,
        "max_concurrent": 4,
        "weight": 60,
        "success_rate_1h": 0.97,
        "latency_p50_ms": 850,
        "last_429_at": null,
        "last_403_at": null,
        "blocks": ["2a09:bac5:32d3:14f0::/64"]
      }
    ]
  }
}
```

## `/api/free-health`

Ajouts :

- `providers` : santé/quota par provider ;
- `attribution` : dernières requêtes (provider, endpoint, bloc, statut,
  latence) ;
- `degraded` : vrai si moins de deux providers sains.

## `/api/free-egress/diag`

Ajouts :

- `auto_egress` : mode, poids, caps, providers exclus ;
- `selection` : dernière décision et alternatives ;
- `duplicates` : blocs partagés entre providers.

## Règles

- Aucun secret (clé, token, mot de passe) dans les réponses.
- Les champs sont additifs ; les clients existants ne cassent pas.
- Un provider `unknown` est affiché comme tel, pas comme `up`.
- Les timestamps sont ISO 8601 UTC.
- L'attribution est bornée (dernières N requêtes) pour éviter les fuites.
