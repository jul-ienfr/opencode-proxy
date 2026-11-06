# Contrat — Cinq egress WARP (direct désactivé)

**Config**: `ip_rotation.proxy_mode: socks5`,
`ip_rotation.socks5_proxies` (cinq entrées actives + `direct` désactivé),
`socks5_auto_rotate: true`.  
**WARP**: quatre conteneurs `caomingjun/warp`, ports 11080/11085/11086/11087.

## Topologie

```
                          ┌─ warp-ipv4  :11080 → 104.28.230.202 (/32 partagé)
proxy :4000 ──────────────┼─ warp-ipv6  :11080 → 2a09:bac5:32d4:be::/64
                          ├─ warp-ipv6-2:11085 → 2a09:bac1:27e0:2060::/64
                          ├─ warp-ipv6-3:11086 → 2a09:bac5:32d2:1c82::/64
                          └─ warp-ipv6-4:11087 → 2a09:bac5:32d4:28b4::/64

direct :11084 → 37.69.230.71   [désactivé, pont arrêté]
```

Le pool `socks5` fait un round-robin sur les entrées **actives**. Chaque
entrée est une station duck-typée (`Socks5Endpoint`) avec son propre id
négatif, son quota et son bad-mark. L'entrée `direct` garde `enabled: false`
et n'est jamais sélectionnée.

## Rotation observée (2026-11-06)

Cinq requêtes distinctes après désactivation du direct, toutes **200** :

| Egress (`requests.db`) |
|---|
| `2a09:bac5:32d4:be::13:23e` |
| `socks5:warp-ipv4` |
| `2a09:bac5:32d2:1c82::2d7:cf` |
| `2a09:bac5:32d4:28b4::40e:36` |
| `2a09:bac1:27e0:2060::219:14` |

`socks5:direct` est absent des nouvelles lignes.

## Invariants

- **E-01** Chaque entrée active est indépendante : un 429 sur l'une bad-mark
  cette entrée, pas les autres.
- **E-02** `direct` est désactivé : aucun trafic free ne sort par l'IP
  résidentielle tant que `enabled: false`.
- **E-03** Si aucune entrée n'est utilisable, `on_request` renvoie
  `(None, None)` et l'appelant retombe sur le direct **uniquement** si
  l'entrée est réactivée ; sinon la requête échoue proprement.
- **E-04** Le ledger clé par l'IP réelle quand elle est connue
  (`egress_ip` configuré ou sonde `refresh_egress`) ; sinon par pid logique.
- **E-05** Une entrée `ipv6: true` force `CURL_IPRESOLVE_V6=2` au niveau
  **session** curl_cffi ; le variant `-ipv6` isole la session de l'IPv4.
- **E-06** `label` est obligatoire quand deux entrées partagent `host:port`.
- **E-07** Le ledger tronque l'IPv6 au `/64` (`server_ip_key`).
- **E-08** Quatre conteneurs WARP = quatre devices = quatre `/64` distincts.

## Démarrage

```powershell
# Les quatre conteneurs WARP
docker start warp-test warp-test2 warp-test3 warp-test4

# Vérifier un bloc IPv6
curl.exe -6 -s --socks5-hostname 127.0.0.1:11085 https://cloudflare.com/cdn-cgi/trace

# Le proxy
pythonw.exe opencode.py
```

## Réactivation du direct

1. `direct` → `enabled: true` ;
2. `scripts/start_direct_socks5.ps1` ;
3. redémarrer le proxy.

## Limites

- WARP IPv4 est une NAT partagée ; les quatre IPv6 sont des `/64` distincts.
- Chaque conteneur ~50 Mo ; un throttle de registration Cloudflare est
  possible au-delà de quatre.
- Le quota reste fini : cinq blocs = cinq budgets, pas l'infini.
- L'adresse IPv6 d'un conteneur change s'il est recréé ; la sonde one-shot
  met le ledger à jour.
