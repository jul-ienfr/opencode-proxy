# Research — Trois modes d'egress

**Feature**: `006-three-egress-modes` | **Date**: 2026-11-06

## 1. État actuel

- Les valeurs internes sont déjà `vpn`, `socks5`, `direct` dans
  `ip_rotation.proxy_mode`, `dashboard/api.py` et `free/pool.py`.
- La GUI expose déjà trois boutons, mais les libellés sont « VPN (OpenVPN) »,
  « SOCKS5 », « Direct ». Le mot « SOCKS5 » décrit le transport, pas le
  fournisseur : l'opérateur ne voit pas « WARP ».
- Les paramètres communs sont déjà stockés dans `ip_rotation`, mais une partie
  est visuellement regroupée sous « Parallel VPN stations », ce qui suggère à
  tort qu'ils sont NordVPN-only.
- Le backend consomme déjà `free_parallel`, `quality_rotation`,
  `latency_rotation`, `strict_free`, `on_429_action`, `station_bad_ttl_s`,
  les tentatives, `free_exception_fallback` et `retry_until_success` dans les
  deux modes ; les tests récents l'ont vérifié pour WARP.
- `capacity_snapshot()` distingue désormais les endpoints WARP des stations
  Docker ; `/health` ne reste plus bloqué sur « 0/1 stations » en WARP.

## 2. Décisions de nommage

| Valeur interne | Libellé GUI | Backend | Nature |
|---|---|---|---|
| `vpn` | **NordVPN** | Docker/gluetun, stack WireGuard/OpenVPN | fournisseur NordVPN |
| `socks5` | **WARP** | endpoints SOCKS5, conteneurs WARP managés | fournisseur WARP |
| `direct` | **Direct** | aucun tunnel | egress local |

**Décision** : aucune migration de `proxy_mode`. Les libellés changent, les
valeurs restent. L'API continue d'accepter `vpn|socks5|direct` ; la GUI mappe
les libellés.

## 3. Matrice des paramètres

Légende portée : **C** = commun NordVPN/WARP ; **N** = NordVPN-only ;
**W** = WARP-only ; **D** = affichage seulement en Direct.

### 3.1 Mode et cycle de vie

| Contrôle GUI | Clé config | Consommateur | Effet NordVPN | Effet WARP | Portée |
|---|---|---|---|---|---|
| Boutons de mode | `proxy_mode` | `dashboard/api.py`, `opencode.py`, `free/pool.py` | démarre/arrête les managers Docker | sélectionne les endpoints WARP | C |
| Activer les modèles free | `ip_rotation.enabled` | `config/settings.py`, pool | active la jambe free | active la jambe free | C |
| Nombre de stations | `station_count` | `_apply_station_count`, managers | nombre de tunnels Docker | sans objet | N |
| Stack VPN | `vpn_stack` | `vpn/manager.py` | WireGuard/OpenVPN | sans objet | N |
| Sélection serveur | `server_pick_mode` | `vpn/manager.py` | proxy/gluetun | sans objet | N |
| Identifiants/serveurs | `credentials`, `servers` | `VpnServers`, manager | auth NordVPN | sans objet | N |
| Mode Docker | `mode` | `VpnServers`, manager | auto/wsl2/docker/native | sans objet | N |
| Port proxy | `vpn_proxy_port` | manager | port SOCKS local | sans objet | N |
| Switch delay | `switch_delay` | manager | délai de rotation Docker | sans objet | N |
| Liste WARP | `socks5_proxies` | `free/pool.py` | sans objet | endpoints, ports, enabled, managed | W |
| Rotation auto WARP | `socks5_auto_rotate` | `free/pool.py` | sans objet | round-robin/sticky WARP | W |
| WARP fallback | `socks5_fallback_enabled` | `free/pool.py` | utilise WARP si aucune station Docker | utilisé comme uplink de secours | W (actif en N) |

### 3.2 Politique de la jambe free

| Contrôle GUI | Clé config | Consommateur | Effet NordVPN | Effet WARP | Portée |
|---|---|---|---|---|---|
| Mode épuisement | `strict_free` | `effective_free_max_attempts`, opencode | tente toutes les stations | tente tous les endpoints | C |
| Action sur 429 | `on_429_action` | `free/pool.py` | cooldown/rotation Docker | cooldown/rotation WARP | C |
| Cooldown station | `station_bad_ttl_s` | `_bad_ttl`, `_apply_bad_mark` | TTL bad-mark Docker | TTL bad-mark WARP | C |
| Tentatives free | `auto_max_free_attempts`, `max_free_attempts` | `effective_free_max_attempts` | budget d'essais | budget d'essais | C |
| Fallback exceptionnel | `free_exception_fallback` | opencode | station-first/direct | station-first/direct | C |
| Quota par IP | `quota_per_ip` | pool, ledger | quota par IP Docker | quota par IP WARP | C |
| Retry until success | `retry_until_success.*` | middleware streaming | retry global | retry global | C |
| Préchargement | `free_prewarm.*` | `_free_prewarm_once` | garde la session courante | garde la session courante | C |

### 3.3 Routage parallèle

| Contrôle GUI | Clé config | Consommateur | Effet NordVPN | Effet WARP | Portée |
|---|---|---|---|---|---|
| Parallélisation | `free_parallel.enabled` | `free/pool.py`, opencode | plusieurs stations Docker | plusieurs endpoints WARP | C |
| Routing | `free_parallel.routing` | `pick_candidates`, `_socks5_spread_enabled` | round-robin/failover | round-robin/failover | C |
| Mode | `free_parallel.mode` | `pick_candidates`, hedge | load-balance/strict/hedge | load-balance/strict/hedge | C |
| Hedge delay | `free_parallel.hedge_delay_ms` | `_hedged_fetch`, stream hedge | course de stations | course d'endpoints | C |
| Hedge max | `free_parallel.hedge_max_attempts` | `_hedged_fetch` | borne la course | borne la course | C |

### 3.4 Qualité et latence

| Contrôle GUI | Clé config | Consommateur | Effet NordVPN | Effet WARP | Portée |
|---|---|---|---|---|---|
| Qualité présélection | `quality_preselection.enabled` | `_rotation_threshold` | seuil de rotation par qualité | seuil de rotation par qualité | C |
| Fast/slow TPS | `quality_preselection.fast_tps/slow_tps` | `_rotation_threshold`, manager | tiers de quota | tiers de quota | C |
| Max API load | `quality_preselection.max_load` | `vpn/manager.py` host selection | filtre de charge serveur | sans objet (WARP n'expose pas la charge) | N |
| Tier B/C quota | `quality_preselection.tier_b_quota/tier_c_quota` | `_rotation_threshold` | quota réduit par tier | quota réduit par tier | C |
| Rotation qualité | `quality_rotation.*` | `on_quality_completion` | `_launch_rotation` Docker | `_request_warp_rotation` managé | C |
| Latence | `latency_rotation.*` | `latency_engine`, `_station_usable` | hard/soft cool | hard/soft cool | C |
| Probe post-rotation | `post_rotation_probe.*` | `WarpRotator._apply` | sans objet (pas de probe Docker) | probe WARP | W |

### 3.5 Statut et capacité

| Contrôle/affichage | Source | Effet NordVPN | Effet WARP | Portée |
|---|---|---|---|---|
| Capacité | `capacity_snapshot()` | stations Docker/gluetun | endpoints WARP | C |
| Liste des stations | `get_status()` | stations Docker | endpoints WARP | C |
| Rotation proactive | `rotation_planner.enabled` | planificateur Docker | rotation WARP via watchdog | N (le planner Docker ne pilote pas WARP) |
| HTTPS uplink | `https_proxies`, `https_fallback_enabled` | fallback NordVPN sans Docker | sans objet | N |
| Test endpoint | `/api/vpn/socks5/test` | sans objet | probe label + IPv6 | W |

## 4. Décisions de portée

1. **`post_rotation_probe` reste WARP-only** : le probe est branché sur
   `WarpRotator._apply`. Un probe équivalent pour la rotation Docker n'existe
   pas. Le libellé doit dire « Sonde egress post-rotation WARP ».
2. **`quality_preselection.max_load` reste NordVPN-only** : WARP n'expose pas
   de charge API par station. Le contrôle doit être déplacé dans la section
   NordVPN.
3. **`rotation_planner` reste NordVPN-only** : le planificateur Docker ne
   pilote pas les rotations WARP (celles-ci passent par `WarpRotator`).
4. **`socks5_fallback_enabled` est WARP-asset mais actif en mode NordVPN** :
   il reste dans la section WARP avec une mention « utilisé si aucune station
   NordVPN n'est disponible ».
5. **Direct** : la section commune reste visible mais affiche un état
   « non applicable au direct » ; les valeurs restent persistées.
6. **`free_parallel`/qualité/latence** : communs, sans branche NordVPN-only.

## 5. Risques et mitigations

| Risque | Mitigation |
|---|---|
| Deux contrôles communs finissent dans deux sections et divergent | Une seule section commune, une seule clé, un seul handler |
| Un paramètre reste inerte en WARP | Matrice `research.md` + test backend par clé |
| Le changement de mode casse une requête en vol | Le mode est lu au début de la requête ; l'egress courant reste attaché à la requête |
| La capacité mélange Docker/WARP | `capacity_snapshot()` par mode + contrat `status-capacity.md` |
| Les anciennes configs deviennent illisibles | Mapping de libellés uniquement, valeurs internes inchangées |
| Un contrôle WARP-only est appliqué en NordVPN | Sections conditionnelles + tests de non-application |

## 6. Preuves existantes à réutiliser

- `tests/test_socks5_affinity.py` : routage/affinité WARP.
- `tests/test_socks5_failover_guard.py` : timeouts/plancher WARP.
- `tests/test_warp_rotation.py` : rotation, blocs distincts, probe.
- `tests/test_free_attempt_tuning.py` : budget d'essais commun.
- `tests/test_vpn_endpoints.py` : API mode/proxy/test.
- `tests/test_free_station_reliability.py` : capacité honnête.
- `tests/test_vpn_alerts_capacity.py` : alertes de capacité.
