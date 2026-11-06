# Cinq blocs WARP actifs — direct désactivé

**Date**: 2026-11-06  
**Statut**: implémenté, testé live **5 egresses WARP distinctes, 5 × 200**,
direct désactivé  
**Spec Kit**: `specs/004-repo-parity-audit/` (T-416, T-420, T-421, T-422)  
**Contrat**: `specs/004-repo-parity-audit/contracts/egress.md`

## 1. Résultat

Le pool free expose **cinq entrées WARP actives**, toutes à 200 :

| # | Label | Egress réel | Type |
|---|---|---|---|
| 1 | `warp-ipv4` | `104.28.230.202` | WARP IPv4, NAT partagée `/32` |
| 2 | `warp-ipv6` | `2a09:bac5:32d4:be::13:23e` | WARP IPv6 `/64` |
| 3 | `warp-ipv6-2` | `2a09:bac1:27e0:2060::219:14` | WARP IPv6 `/64` |
| 4 | `warp-ipv6-3` | `2a09:bac5:32d2:1c82::2d7:cf` | WARP IPv6 `/64` |
| 5 | `warp-ipv6-4` | `2a09:bac5:32d4:28b4::40e:36` | WARP IPv6 `/64` |

L'entrée `direct` (`37.69.230.71`) est **désactivée** (`enabled: false`) et le
pont `direct_socks5.py` est arrêté. Aucun trafic free ne sort par l'IP
résidentielle.

## 2. Preuve

| Test | Résultat |
|---|---|
| `cloudflare.com/cdn-cgi/trace` via chaque conteneur | 4 adresses IPv6 distinctes, `warp=on` |
| Probe `/inference` par bloc, wire exact, `IPRESOLVE=6` | **4 × 200** |
| Cinq requêtes via le pool après désactivation du direct | **5 × 200** |
| `requests.db` | uniquement `2a09:bac5:32d4:be::13:23e`, `2a09:bac1:27e0:2060::219:14`, `2a09:bac5:32d2:1c82::2d7:cf`, `2a09:bac5:32d4:28b4::40e:36`, `socks5:warp-ipv4` |
| `socks5:direct` | **absent** des nouvelles lignes |

## 3. Implémentation

### 3.1 `free/pool.py` — `Socks5Endpoint`

| Champ | Rôle |
|---|---|
| `label` | pid/state distinct ; indispensable quand plusieurs entrées partagent `host:port` |
| `ipv6` | force `CURL_IPRESOLVE_V6=2` |
| `egress_ip` | IP réelle configurée (optionnelle) ; sinon sonde one-shot |

`pid = label or "host:port"`, `current_ip = egress_ip or "socks5:<pid>"`.
`refresh_egress()` force IPv6 via `Session(curl_options={CurlOpt.IPRESOLVE: 2})`
et interroge `api64.ipify.org` (repli `cloudflare.com/cdn-cgi/trace`).

`set_socks5_proxies()` / `_rebuild_static_eps()` lisent et propagent
`label`, `ipv6`, `egress_ip`, `enabled`. Une entrée `enabled: false` est
ignorée par `_socks5_enabled_eps()`.

`on_request()` lance une sonde IPv6 one-shot si `egress_ip` n'est pas
configuré.

### 3.2 `opencode.py` — session curl

- `_get_pooled_curl_session()` accepte `curl_options` et les fusionne avec les
  options proxy TLS ;
- `_do_free_request_curl_cffi()` pose `CurlOpt.IPRESOLVE=2` et un variant
  `-ipv6` pour isoler la session de l'IPv4 ;
- `curl_options` n'est passé que s'il existe (doubles de test compatibles).

### 3.3 `config.yaml`

```yaml
ip_rotation:
  proxy_mode: socks5
  socks5_auto_rotate: true
  socks5_proxies:
  - {host: 127.0.0.1, port: 11080, label: warp-ipv4}
  - {host: 127.0.0.1, port: 11080, label: warp-ipv6, ipv6: true}
  - {host: 127.0.0.1, port: 11085, label: warp-ipv6-2, ipv6: true}
  - {host: 127.0.0.1, port: 11086, label: warp-ipv6-3, ipv6: true}
  - {host: 127.0.0.1, port: 11087, label: warp-ipv6-4, ipv6: true}
  - {host: 127.0.0.1, port: 11084, label: direct, enabled: false}
```

Le `label` est obligatoire pour les entrées qui partagent `host:port`
(`warp-ipv4` / `warp-ipv6`).

## 4. Conteneurs WARP

| Conteneur | Port hôte | `/64` |
|---|---|---|
| `warp-test` | 11080 | `2a09:bac5:32d4:be::/64` |
| `warp-test2` | 11085 | `2a09:bac1:27e0:2060::/64` |
| `warp-test3` | 11086 | `2a09:bac5:32d2:1c82::/64` |
| `warp-test4` | 11087 | `2a09:bac5:32d4:28b4::/64` |

Les quatre conteneurs ont `--restart unless-stopped`.

## 5. Réactiver le direct

1. `config.yaml` : passer l'entrée `direct` à `enabled: true` ;
2. démarrer le pont : `powershell -ExecutionPolicy Bypass -File scripts\start_direct_socks5.ps1` ;
3. redémarrer le proxy ;
4. vérifier `requests.db` : `socks5:direct` réapparaît.

Sauvegarde de l'état actuel : `config.yaml.bak-nodirect-20261106` ; état six
blocs : `config.yaml.bak-6blocks-20261106`.

## 6. Rollback

- Retirer les entrées `warp-ipv6-*` et réactiver `direct` ;
- `docker rm -f warp-test2 warp-test3 warp-test4` ;
- restaurer une sauvegarde et redémarrer le proxy.

## 7. Limites

- WARP IPv4 reste une **NAT partagée** ; les quatre IPv6 sont des `/64`
  distincts.
- Chaque conteneur WARP consomme ~50 Mo et une registration Cloudflare ;
  quatre fonctionnent, un throttle est possible au-delà.
- Le quota reste **fini** : cinq blocs = cinq budgets, pas l'infini.
- L'adresse IPv6 d'un conteneur change s'il est recréé ; la sonde one-shot
  met le ledger à jour.
