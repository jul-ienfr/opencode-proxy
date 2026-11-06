# Measurements — État et tests live du 2026-11-06

**Feature**: `004-repo-parity-audit`  
**Fenêtre**: 2026-11-06, UTC. Les latences sont indicatives (le modèle et le
réseau varient) ; le statut et le corps sont les faits.

## 1. État runtime au moment de l'audit

| Élément | Valeur |
|---|---|
| Date | 2026-11-06 |
| Proxy | `pythonw.exe opencode.py` |
| PID | 19116 |
| Port | `0.0.0.0:4000` |
| Mode | `proxy_mode: direct` |
| Stations | `station_count: 1` (VPN non utilisées) |
| IP directe | `37.69.230.71` |
| WARP | conteneur `warp-test` healthy, SOCKS5 `127.0.0.1:11080` |
| IP WARP | `104.28.230.202` |
| `free.cooldown_cap_s` | 1200 |
| `ip_quota.ledger_max_block_s` | 1200 |
| `free.max_client_tools` | 9 |
| `quota_per_ip` (profil) | 500 |
| `ROTATION_VERSIONS` | `("1.19.5",)` |

Conteneurs VPN présents mais non utilisés en mode direct :
`opencode-vpn`, `opencode-vpn-2` … `opencode-vpn-6`.

## 2. Tests live après correctifs (proxy PID 19116)

| # | Test | Statut | Latence | Réponse / note |
|---|---|---:|---:|---|
| 1 | `ping` | **200** | 31 452 ms | `pong` (démarrage à froid) |
| 2 | `tool_choice: none` + `<env></env>` invalide + `read` | **200** | 9 298 ms | `tool_calls: ["read"]` — garde + retrait de `none` |
| 3 | `tool_choice: none` + `<env>` valide | **200** | 8 517 ms | `Bonjour.` |
| 4 | Vraie demande de code (`mul`) | **200** | 8 724 ms | fonction + test |
| 5 | Stream SSE | **200** | — | `text/event-stream`, `[DONE]`, frame `cost` |

Test 5, queue observée :

```
data: {… "delta":{"content":"OK"} …}
data: {… "finish_reason":"stop", "usage":{…, "cost":0, …} …}
data: {… "choices":[], "usage":{…, "cost":0, …} …}
data: [DONE]
```

La frame `cost` est **avant** `[DONE]` dans notre relais, contrairement à la
capture rianilham où elle est après. Voir `T-407`.

## 3. Tests live de la première passe (même journée, avant le plafond station)

| # | Test | Statut | Latence | Note |
|---|---|---:|---:|---|
| 6 | `ping` | 200 | 5 ms | cache chaud |
| 7 | Outil étranger `read_file` | 200 | 8 867 ms | `tool_calls: ["read_file"]` |
| 8 | **Marqueur officiel sans `<env>` + `read` seul** | 200 | 3 510 ms | tranche le débat 7 outils vs `read` seul |
| 9 | Code `add` | 200 | 9 928 ms | fonction + test |
| 10 | Stream `1..5` | 200 | 4 640 ms | SSE |

Le test 8 est la mesure clé : sur `deepseek-v4.1-flash-free`, le 2026-11-06,
un seul outil `read` + `<env>` suffit. La mesure « 7 outils » de xljiulang
n'est pas reproduite sur cette IP/modèle.

## 4. Clean-room (résultats antérieurs, 2026-11-06)

| Scénario | Client officiel | Replay exact | Verdict |
|---|---|---|---|
| IP directe fraîche `37.69.230.71` | 200 `pong` | 200 | wire conforme |
| IP VPN partagée `64.238.204.228` | 429 | 429 | quota d'IP |
| Proxy local direct | — | 200 `pong`, 200 code, 200 stream | jambe opérationnelle |

Conclusion : le client officiel et le replay exact ont le **même verdict sur la
même IP**. Le 429 VPN n'est pas un défaut de wire.

## 5. WARP (2026-11-06)

| Test | Egress | Résultat |
|---|---|---|
| Première connexion WARP | `104.28.230.202` | **200**, 5 584 ms, premier chunk SSE |
| Redémarrage du conteneur | `104.28.230.202` | même IP |
| `registration delete` + `new` ×3 | `104.28.230.202` | même IP |
| Deuxième conteneur simultané | `104.28.230.202` | même IP |
| Override vers 5 endpoints Cloudflare | — | tunnel non connecté |

Conclusion : WARP fournit **une** IP utilisable par hôte/réseau, pas six.

## 6. IP VPN historiques

Les 16 hôtes historiques testés le 2026-11-06 (WireGuard, une session) :

`al46`, `al73`, `al37`, `al36`, `al41`, `al45`, `us12806`, `pl296`, `al74`,
`al117`, `uk2433`, `us13473`, `au1140`, `nl-onion9`, `lt40`, `au1101`.

Tous les egress obtenus répondent **429** `FreeUsageLimitError`, avec un
`Retry-After` de ~66 900–68 000 s (≈ distance au prochain 00:00 UTC). Trois
hôtes hors historique (`nz123`, `ie257`, `mx173`) : `mx173` → `187.13.205.237`
→ 429 ; les deux autres sans egress.

## 7. Suite de tests

| Commande | Résultat |
|---|---|
| `pytest tests -q -k "free or impersonation or official or wire or identity or quota or pool or trial_dead"` | **exit 0**, 148 tests collectés |
| `pytest tests/test_free_body_conformite.py tests/test_free_official_payload.py tests/test_official_wire_e2e.py` | **exit 0** |
| `pytest tests/test_quota_ledger.py tests/test_quota_ledger_sqlite.py tests/test_quota_bucket_pool.py` | **exit 0** |
| `pytest tests/test_trial_dead_quota.py tests/test_free_multi_attempt.py` | **exit 0** |

## 8. Consommation de quota

Environ **20 requêtes** envoyées à l'IP directe pendant l'audit (tests,
clean-room, probes), soit < 10 % du bucket ~200 req/5 h. L'IP reste fraîche à
la fin de l'audit.

## 9. Diagnostic des stations VPN (2026-11-06, après l'audit)

Les six stations n'étaient pas seulement à quota : elles bouclaient sur un
échec DNS gluetun.

| Constat | Preuve |
|---|---|
| `DNS_UPSTREAM_RESOLVER_TYPE=plain` → `127.0.0.1:53 server misbehaving` | logs `opencode-vpn-2` |
| L'override généré (`logs/compose-hostname-*.yml`) force `plain` après 2 échecs DoT | `fresh_chain.py` `DnsFallback` |
| Un conteneur témoin en **DoT** est healthy et sort sur `187.40.243.35` | `oc-dns-test`, logs + `api.ipify.org` |
| OpenVPN échoue `AUTH_FAILED` ; WireGuard authentifie | station 1 vs témoin |
| Le recipe WireGuard + DoT + MTU 1419 + `HEALTH_VPN_DURATION_INITIAL=60s` est healthy | `oc-dns-test` |
| Deux stations déjà saines : `opencode-vpn-2` → `186.247.77.36`, `opencode-vpn-3` → `186.247.77.6` | `docker exec` |
| Ces deux IP répondent **429** `FreeUsageLimitError`, RA ~64 200–64 700 s | probe SOCKS5 `1081`/`1082` |
| Le superviseur relancé avec `fast_discovery_transport: wireguard` réécrit quand même l'override en `VPN_TYPE=openvpn` (`us10469`) | `logs/compose-hostname-1.yml` après relance |
| Trois stations saines après recreation (`-2`, `-3`, `-5`), deux unhealthy (`-4`, `-6`) | `docker ps` |
| `opencode-vpn-5` → `186.247.77.120` ; probe SOCKS5 `1084` → **429**, RA 61 570 s | dernier probe |
| Le superviseur et ses enfants ont été arrêtés pour ne pas brûler de sessions | `Get-CimInstance` vide |

Conclusion : le blocage restant est bien le **quota par IP**, pas le tunnel.
Le recipe DNS/MTU est validé ; il doit être appliqué aux stations qui seront
recréées. Les IP saines testées restent épuisées jusqu'au reset serveur.

## 10. Deux stations SOCKS5 : WARP + direct (2026-11-06)

Le pool est passé en `proxy_mode: socks5` avec deux entrées :

| Entrée | URL | Egress | État |
|---|---|---|---|
| WARP | `127.0.0.1:11080` | `104.28.230.202` | healthy |
| Pont direct | `127.0.0.1:11084` | `37.69.230.71` | listening (PID 24704) |

Rotation observée sur quatre requêtes distinctes, toutes **200** :

| Heure UTC | Egress |
|---|---|
| 07:04:45 | `socks5:127.0.0.1:11080` |
| 07:05:05 | `socks5:127.0.0.1:11084` |
| 07:05:26 | `socks5:127.0.0.1:11080` |
| 07:05:46 | `socks5:127.0.0.1:11084` |

Stream SSE : **200**, `text/event-stream`, `[DONE]`.

Proxy : PID 10356, port 4000. Sauvegarde config :
`config.yaml.bak-socks5-20261106`.

## 11. Contraintes d'egress — deuxième lot (2026-11-06)

| Constat | Preuve |
|---|---|
| L'hôte n'a **aucune IPv6 globale** | `Get-NetIPAddress` ne rend que `fe80::8a78:…` (link-local) |
| La voie WARP `/64` est donc indisponible ici | nécessite `network_mode: host` + préfixe routé, Linux |
| Le quota est par **bloc d'egress** (`/64` IPv6, `/32` IPv4), reset 00:00 UTC | `ebouther/docs/free-tier-gates.md` (2026-10-27) |
| WARP IPv4 est une NAT partagée | même doc ; notre WARP reste une 2ᵉ IP, pas un multiplicateur |
| `server_ip_key` tronque déjà l'IPv6 au `/64` | `free/quota_ledger.py` |
| Le mint `ses_` est canonique (`…ffe`) | `tests/test_session_canonical.py` (2 passed) |
| Le gate est `read` seul, pas 7 outils | `ebouther` (une variable à la fois) + notre test live |

Conséquence : pour six blocs distincts, il faut un hôte Linux avec un `/64`
routé + WARP `stack: ipv6`, ou six blocs IPv4 distincts. Sur ce Windows, le
pool reste à deux stations (WARP + direct), et le levier est le nombre de
blocs, pas le nombre de sessions NordVPN.

## 12. Balayage GitHub et tests IPv6 (2026-11-06)

| Test | Résultat |
|---|---|
| `cloudflare.com/cdn-cgi/trace` via WARP | `ip=2a09:bac5:32d4:be::13:23e`, `warp=on`, `loc=FR` |
| Probe `/inference` via WARP, `IPRESOLVE=6` | **200**, 26 744 ms, SSE |
| Probe `/inference` via WARP, `IPRESOLVE=4` | **200**, 23 714 ms, SSE |
| 3 sessions fraîches sur `186.247.77.36` (`1081`) | **3 × 429**, RA ~61 905 s |
| `warp-minimal` v0.3.0 Windows | démarre, `/health` OK, registration `v0a2158/reg` → timeout |
| Balayage GitHub | 12 requêtes, **365 dépôts uniques**, tri `pushed_at` |

Le WARP existant fournit donc **deux familles d'egress** : IPv4
`104.28.230.202` (NAT partagée) et IPv6 `2a09:bac5:32d4:be::/64`. Le probe
IPv6 avec notre wire exact rend 200. La rotation de session sur une IP déjà
429 ne rend rien : le quota est par bloc.

## 13. Station IPv6 WARP (T-421, 2026-11-06)

Six requêtes distinctes après redémarrage (PID 33732), toutes **200** :

| # | Egress (`requests.db`) | Statut |
|---|---|---|
| 1 | `2a09:bac5:32d4:be::13:23e` | 200 |
| 2 | `socks5:direct` | 200 |
| 3 | `socks5:warp-ipv4` | 200 |
| 4 | `2a09:bac5:32d4:be::13:23e` | 200 |
| 5 | `socks5:direct` | 200 |
| 6 | `socks5:warp-ipv4` | 200 |

Rotation : **IPv6 → direct → IPv4**, répétée. Latences 13,7–28,2 s.
La sonde IPv6 one-shot a résolu l'adresse réelle, et le ledger l'a keyée au
`/64`.

Tests :
- `tests/test_socks5_ipv6_station.py` → 3 passed ;
- suite `free|pool|quota|official|identity|wire|impersonation` → exit 0.

Après le redémarrage final (PID 16200), contrôle de 3 requêtes :
`07:54:36` → IPv6, `07:54:57` → direct, `07:55:19` → `warp-ipv4`, **3 × 200**.

Sauvegarde config : `config.yaml.bak-ipv6-20261106`.

## 14. Direct désactivé (2026-11-06)

Sur demande opérateur, l'entrée `direct` est passée à `enabled: false` et le
pont `direct_socks5.py` a été arrêté (port 11084 fermé). Le pool ne sert plus
que les cinq blocs WARP.

| # | Egress (`requests.db`) | Statut |
|---|---|---|
| 1 | `2a09:bac5:32d4:be::13:23e` | 200 |
| 2 | `socks5:warp-ipv4` | 200 |
| 3 | `2a09:bac5:32d2:1c82::2d7:cf` | 200 |
| 4 | `2a09:bac5:32d4:28b4::40e:36` | 200 |
| 5 | `2a09:bac1:27e0:2060::219:14` | 200 |

`socks5:direct` est **absent** des nouvelles lignes. Proxy PID 6544.
Sauvegarde config : `config.yaml.bak-nodirect-20261106`. Pour réactiver :
`enabled: true`, relancer `scripts/start_direct_socks5.ps1`, redémarrer.

## 15. Ce qui n'a pas été mesuré

- Le shadow-ban de session (test destructif, réservé à une IP sacrifiable).
- La limite exacte de sessions NordVPN (six stations + test).
- Le comportement d'un client OpenAI strict sur la frame `cost`.
- Le keep-alive après 429.
- Les modèles `muse-*`, `spark-*`, `jev-*` (endpoint Responses/SystemOne).
- La parité UA 2.2.5 (le proxy envoie encore l'UA 1.19.5).
