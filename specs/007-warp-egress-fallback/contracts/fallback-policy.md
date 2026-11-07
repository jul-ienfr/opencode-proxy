# Contract — FallbackPolicy

**But**: ordonner les replis, limiter leur usage et garantir le retour à WARP
sans bascule payante/direct silencieuse.

## Configuration

```yaml
ip_rotation:
  fallback:
    order:
      - warp
      - warp_alt
      - isp_ipv6
      - oracle
      - proton
      - windscribe
      - vps_ipv6
    max_fallback_stations: 2
    probe_interval_s: 300
    recovery_successes: 2
    cooldown_s: 900
    max_block_s: 0          # 0 = park strict jusqu'à reset_at
    fail_closed: true
    direct_allowed: false
```

## Déclencheurs

| Déclencheur | Seuil | Action |
|---|---|---|
| Quota après rotations WARP | aucun `/64` frais | fallback suivant |
| Échec/throttle WARP | > 10 min | fallback suivant |
| Conteneur WARP down | > 2 min | fallback suivant |
| Bloc épuisé répété | ≥ 3 fois | fallback suivant |
| Override manuel | dashboard | fallback choisi |

## Non-déclencheurs

- `403 FreeTierError` → corriger le wire/geo/auth, aucune rotation.
- `429 RateLimitError` court → attendre la fenêtre, pas de park.
- `5xx` transitoire → retry borné, pas de fallback.
- Erreur d'auth/version → aucune rotation.

## Règles

1. WARP reste le provider majoritaire ; `max_fallback_stations` ≤ 2.
2. Un provider payant (`vps_ipv6`, `mullvad`) n'est utilisé que si
   `enabled: true` et `direct_allowed: false` ; jamais automatique.
3. `fail_closed: true` → pool vide = `degraded` explicite, pas de direct.
4. Retour à WARP après `recovery_successes` probes `200` consécutifs, puis
   `cooldown_s` avant une nouvelle bascule.
5. Un bloc fallback n'est jamais réutilisé par WARP, et inversement.
6. La bascule draine les requêtes en vol avant de changer le provider.

## Observabilité

Le dashboard expose :

```json
{
  "provider": "proton",
  "fallback_active": true,
  "reason": "warp_quota_exhausted",
  "since": "2026-11-07T10:00:00Z",
  "next_probe_at": "2026-11-07T10:05:00Z",
  "max_fallback_stations": 2
}
```

## Tests

- WARP épuisé → 1 station fallback, 10/10 `200`.
- 403 → aucune bascule.
- 429 court → pas de park, pas de fallback.
- 2 probes WARP `200` → retour WARP après cooldown.
- fallback payant désactivé → `degraded`, pas de direct.
- 3 stations épuisées → au plus 2 en fallback.
