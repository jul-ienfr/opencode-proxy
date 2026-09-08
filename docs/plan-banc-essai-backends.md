# Plan de banc d'essai backends VPN — résultats réels (2026-09-08)

But : tester TOUS les projets candidats (aucun exclu pour jeunesse),
chacun dans sa techno, et mesurer. La rotation par restart de station
est ACCEPTÉE (pas d'exigence d'API hot-switch). Les résultats alimentent
la sélection multi-backend du GUI (1 station = 1 backend, multi-sélection).

## Règle d'or (throttle NordVPN actif le 2026-09-08)

- **0 AUTH** : pulls, inspections, tests WireGuard à clé (handshake, pas d'AUTH).
- **1 AUTH espacé** : chaque test OpenVPN (attendre ≥ 5 min entre deux).
- **BLOQUÉ (attente opérateur)** : tout ce qui exige le TOKEN NordVPN
  (`bubuntux/nordvpn`, `edgd1er/nordlynx-proxy` si login requis, `alle`).
- Secrets : lus depuis `credentials.env` / `vpn_configs/wireguard.env`
  via fichiers d'env temporaires 0600, **jamais affichés ni loggés**.
- Ports de bench (hors flotte 1080-1085/8888-8893, canari 1090) :
  bench-1 SOCKS 1191 / HTTP 8991, bench-2 SOCKS 1192 / HTTP 8992.

## Matrice T0 — statique, mesuré le 2026-09-08 (pulls OK tous)

| Backend | Image (digest court) | Taille | Built | Binaires constatés |
|---|---|---|---|---|
| gluetun (référence) | `qmcgaw/gluetun@sha256:89e3cbe2` | 54,7 Mo | 01/09/26 | ovpn+wg, socks5+http, control API, updater |
| edgd1er/nordvpn-proxy | `sha256:9d766c11` | ~117 Mo | 06/2025 | openvpn, tinyproxy, unbound, **sockd (dante)** |
| edgd1er/nordlynx-proxy | `sha256:b4eb4d59` | ~437 Mo | **07/09/26** | nordvpn CLI, tinyproxy, wg |
| bubuntux/nordlynx | `sha256:2e1444be` | ~32 Mo | 11/2025 | wg, wg-quick (AUCUN proxy — sidecar requis) |
| binhex/arch-privoxyvpn | `sha256:bcdde4f4` | ~1090 Mo | 06/2026 | openvpn, wg, privoxy, microsocks |
| faeton/vpnsocksify | `sha256:84298156` | ~32 Mo | 16/08/26 | openvpn, wg, **sockd** (pas de HTTP) |
| titidnh/openvpn_client_proxy | `sha256:233ea704` | ~151 Mo | **07/09/26** | openvpn, privoxy, dnsmasq, unbound (pas de SOCKS5) |
| dperson/openvpn-client | `sha256:d174047b` | ~20 Mo | **2020** | openvpn seul (pas de proxy) |

Notes T0 : `nordvpn-proxy` 06/2025 = le plus ancien des maintenus (à surveiller) ;
`binhex` 1 Go (Arch full) ; `dperson` 2020 = fossile (baseline dumb uniquement) ;
`titidnh`/`nordlynx-proxy` build d'hier = maintenance active.

## Critères live (par backend)

- **T1** boot→healthy (ou raison d'échec exacte)
- **T2** egress IP + pays conformes à la demande
- **T3** DNS via tunnel OK
- **T4** SOCKS5 répond / HTTP répond (natif ou sidecar microsocks/tinyproxy)
- **T5** fail-closed (tunnel coupé → sondes bloquées, pas de fuite)
- **T6** rotation (restart / re-pick → nouvelle IP + délai)
- **T7** stabilité 15 min (egress OK chaque minute)
- **T8** coût AUTH observé (lignes AUTH_FAILED pendant le test)

## Protocoles (commandes exactes)

### W1 — bubuntux/nordlynx (WG, clé, 0 AUTH)
Fichier d'env temporaire (0600) : `PRIVATE_KEY=<wireguard.env>`,
`QUERY=filters[country_id]=153` (NL=153, DE=81, FR=74, SE=208, CH=209),
`NET_LOCAL=192.168.0.0/16,10.0.0.0/8,172.16.0.0/12`.
`docker run -d --name nordlynx-bench-1 --cap-add=NET_ADMIN --env-file <tmp> ghcr.io/bubuntux/nordlynx:latest`
Vérif : logs (IP connectée), `docker exec wget http://api.ipify.org` (attendu : IP NL),
`wget http://1.1.1.1` (transport seul). `docker rm -f` après (libère le slot).

### W2 — edgd1er/nordlynx-proxy (WG, client officiel)
Bloqué si login TOKEN requis — d'abord essayer sans identifiants ?
Non : le client nordvpn exige login. **En attente TOKEN.**

### O1 — edgd1er/nordvpn-proxy (OV, 1 AUTH)
Env : `NORDVPN_COUNTRY=Netherlands`, `NORDVPN_PROTOCOL=tcp`,
`NORDVPN_USER/PASS` (service creds), `TINYPORT`, ports bench-1.
Vérif T1-T4 + `AUTH_FAILED ?` (T8) + rotation (`NORDVPN_SERVER=<autre>` + recreate → T6).

### O2 — binhex/arch-privoxyvpn OV custom (OV, 1 AUTH)
`.ovpn` NordVPN (de1223/de1227) + `VPN_PROV=custom`, `VPN_CLIENT=openvpn`,
`VPN_USER/PASS` (service), `ENABLE_SOCKS=yes`, `NAME_SERVERS=1.1.1.1`.
Vérif T1-T4 (8118/9118 mappés sur bench).

### O3 — faeton/vpnsocksify OV (OV, 1 AUTH)
Même `.ovpn` + `VPN_USER/PASS`, `SOCKS_PORT` bench. T1-T4 (pas de HTTP natif).

### O4 — titidnh OV (OV, 1 AUTH)
`vpn.conf` (.ovpn renommé), T1-T4 (HTTP 3128 mappé bench, pas de SOCKS5).

### O5 — dperson OV (OV, 1 AUTH, baseline)
`.ovpn` + creds, volume `/vpn`. T1-T3 seulement (pas de proxy).

### B1 — binhex WG (WG, 0 AUTH, `privileged`)
`wg0.conf` à construire (endpoint issu API publique + clé existante ;
pubkey serveur requise — si introuvable : AJOURNÉ avec raison).
Uniquement si W1 échoue (même classe, plus lourd).

### B2 — linuxserver/wireguard client-mode
UNIQUEMENT si conf statique obtenable (cf. B1) — sinon AJOURNÉ documenté.

### Bloqués TOKEN (opérateur) : bubuntux/nordvpn, edgd1er/nordlynx-proxy (si login), alle
`alle` (v0.1.13, 2★, WG-only) : installation bac à sable + `providers add nordvpn`
dès TOKEN dispo — canaux WG multi-pays, tests T1-T7 via son REST.

## Biais connu : throttle AUTH du 2026-09-08

Tout ÉCHEC AUTH d'un backend OV pendant le throttle ne conclut PAS sur
la qualité du backend (le compte rejette tout). Règle : un OV qui échoue
par AUTH_FAILED est re-testé après retour au vert (pool ≥ 4/6 stable
10 min). Seuls les échecs NON-AUTH (config, binaire, DNS interne,
killswitch) concluent immédiatement.

## Suite GUI (après résultats)

Sélection multi-backend par station : champ `backend` par station
(`gluetun` | `nordvpn-proxy` | `nordlynx-proxy` | `nordlynx-key` |
`binhex-ov` | `binhex-wg` | `vpnsocksify` | `titidnh` | `dperson` |
`alle`) — multi-sélection = flotte panachée pilotée par le manager
(rotations via chemins existants). Seuls les backends VALIDÉS ici
apparaissent dans la liste.

## Résultats

| Backend | T0 | T1 | T2 | T3 | T4 | T5 | T6 | T7 | T8 | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| gluetun OV (réf. prod) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ churn interne | AUTH storms | référence |
| gluetun WG (réf. prod) | ✅ | ⚠️ peers silents | partiel | partiel | ✅ | ✅ | ✅ | ⚠️ | 0 | référence |
| nordlynx-proxy (edgd1er) | ✅ pull | ✅ NL#824 en ~2min (TOKEN ; `--privileged` requis sinon `sysctl disable_ipv6: permission denied`) | ✅ 86.104.23.47 (SOCKS+HTTP : même IP) | ✅ | ✅ dante+tproxy natifs | ✅ fail-closed (000 instantané après disconnect) | ⬜ (`connect <pays>` = rotation sans recreate) | ⬜ 15min | **0 AUTH_FAILED, login TOKEN OK** | **CANDIDAT N°1 CONFIRMÉ 2026-09-08** |
| nordlynx-key (bubuntux) | ✅ pull | ✅ connecté en ~1s (QUERY **échappée** `filters\[country_id\]=153` requise — crochets nus = `curl: bad range`) | ✅ 186.247.163.40 (NL) | ✅ | ❌ aucun proxy natif (sidecar requis) | ✅ firewall up au boot (à tester en coupure) | ⬜ | ⬜ | **0 AUTH** | **CANDIDAT WG n°1** |
| nordvpn-proxy (edgd1er) | ✅ pull | ⬜ après cooldown | | | | | | | | |
| binhex OV | ✅ pull | ⬜ après cooldown | | | | | | | | |
| binhex WG | ✅ pull | ⬜ si W1 échoue | | | | | | | | |
| vpnsocksify | ✅ pull | ⬜ après cooldown | | | | | | | | |
| titidnh | ✅ pull | ⬜ après cooldown | | | | | | | | |
| dperson | ✅ pull | ⬜ baseline | | | | | | | | |
| linuxserver WG | — | ⬜ si conf obtenable | | | | | | | | |
| alle | — | ⏳ TOKEN | | | | | | | | |
| bubuntux/nordvpn | — | ⏳ TOKEN | | | | | | | | |

Légende : ✅ mesuré OK · ⚠️ partiel · ❌ échec (motif) · ⬜ planifié · ⏳ bloqué opérateur.
