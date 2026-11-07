# Contract — Auto-Egress Policy

## Mode dérivé

La sélection est une **liste de providers cochés** ; le mode est dérivé :

```text
["nordvpn"]              → vpn
["warp"]                 → socks5
["direct"]               → direct
["nordvpn","warp"]       → auto_egress
["warp","proxies"]       → auto_egress
[]                       → refus (au moins un provider)
```

`auto_egress` n'est jamais un fallback silencieux : il n'est actif que si
plusieurs providers sont cochés.

## Configuration

```yaml
ip_rotation:
  egress:
    selected_providers: [warp]   # liste cochée
    derived_mode: socks5         # calculé, jamais saisi
  auto_egress:
    enabled: false
    providers: [warp, nordvpn, proxies]
    weights:
      warp: 60
      nordvpn: 30
      proxies: 10
    max_concurrent:
      warp: 4
      nordvpn: 2
      proxies: 2
    hedge_delay_ms: 1500
    hedge_max_attempts: 1
    allow_direct: false
    fail_closed: true
    diversity: true
    sticky: true
    sticky_ttl_s: 1800
```

## Inclusion des providers

- `warp` : endpoints `managed=true` + `ipv6=true` (ou `provider=warp`).
- `nordvpn` : stations du manager NordVPN (`proxy_mode=vpn` ou provider).
- `proxies` : `socks5_proxies` + `https_proxies` statiques activés.
- `direct` : uniquement si `allow_direct=true` et aucun provider sain.
- Un provider désactivé dans le GUI est exclu sans erreur.

## Règles

1. Free-first : WARP poids 60 par défaut, jamais exclu s'il est sain.
2. Aucun nouvel abonnement : seuls les providers configurés sont utilisés.
3. `fail_closed=true` + `allow_direct=false` → `degraded`, pas de direct.
4. Un provider down/épuisé est retiré ; les autres continuent.
5. Un `403` ne retire pas un provider ; il ne touche pas le ledger.
6. Un `429` quota parke/rotate le provider concerné seulement.
7. Changement de mode : drain des requêtes en vol, puis bascule.
8. Le mode est persisté dans `config.yaml` et restauré au boot.

## Tests

- mode visible/persistant ;
- provider désactivé exclu ;
- direct interdit sans `allow_direct` ;
- changement de mode sans interruption ;
- WARP + NordVPN + proxy → au moins deux providers servent.
