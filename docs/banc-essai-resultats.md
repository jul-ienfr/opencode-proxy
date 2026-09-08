# Banc d'essai backends — journal des résultats (2026-09-08)

Historique exhaustif : chaque run horodaté, version pinnée (digest),
chronométrage par phase, verdict. Protocoles :
`docs/plan-banc-essai-backends.md`. Harnais : `scripts/bench_backends.py`.

## Contexte et biais (à lire avant d'interpréter un résultat)

- **Throttle AUTH NordVPN actif toute la journée** (session-limit,
  `AUTH_FAILED` multi-hôtes, ban 10 min qui se prolonge à chaque
  tentative). Tout ÉCHEC AUTH d'un backend OV ne conclut PAS sur le
  backend — re-test après retour au vert (pool ≥ 4/6 stable 10 min).
- Un run WG ne coûte aucune AUTH ; un run OV en coûte ≥ 1.
- Secrets lus depuis `credentials.env` / `vpn_configs/wireguard.env`
  (ignorés git), via fichiers d'env 0600 temporaires, **jamais affichés**.
- **Correction du 2026-09-08** : le run W1 avait été annoncé « ~1 s » —
  relecture des timestamps : **~62 s** (sélection serveur + handshake).
  Chiffre corrigé ci-dessous, leçon : chronométrage par phase exigé.

## T0 — statique (2026-09-08, pulls OK)

| Backend | Digest | Taille | Built | Binaires constatés |
|---|---|---|---|---|
| edgd1er/nordvpn-proxy | `sha256:9d766c11` | ~117 Mo | 06/2025 | openvpn, tinyproxy, unbound, sockd(dante) |
| edgd1er/nordlynx-proxy | `sha256:b4eb4d59` | ~437 Mo | 07/09/26 | nordvpn CLI, tinyproxy, wg |
| bubuntux/nordlynx | `sha256:2e1444be` | ~32 Mo | 11/2025 | wg, wg-quick (AUCUN proxy) |
| binhex/arch-privoxyvpn | `sha256:bcdde4f4` | ~1090 Mo | 06/2026 | openvpn, wg, privoxy, microsocks |
| faeton/vpnsocksify | `sha256:84298156` | ~32 Mo | 16/08/26 | openvpn, wg, sockd (pas de HTTP) |
| titidnh/openvpn_client_proxy | `sha256:233ea704` | ~151 Mo | 07/09/26 | openvpn, privoxy, dnsmasq, unbound (pas de SOCKS5) |
| dperson/openvpn-client | `sha256:d174047b` | ~20 Mo | 2020 | openvpn seul |

## Runs

### W1 — bubuntux/nordlynx, run 1/10 (2026-09-08, 15:40–15:42Z)
- Env : `PRIVATE_KEY` (wireguard.env) + `QUERY=filters[country_id]=153`
  (**crochets ÉCHAPPÉS requis**, sinon `curl: bad range` — 1er essai
  raté pour cette raison exacte, retry après correction).
- T1 : start 15:40:1x → `Connected!` 15:41:17 ≈ **62 s**.
- T2 : egress **186.247.163.40** (NL, conforme au QUERY).
- T3 : DNS OK + transport IP OK. T8 : **0 AUTH**.
- T4 : N/A (aucun proxy natif — sidecar microsocks requis en prod).
- Verdict : **PASS** (transport). Conteneur supprimé, slot libéré.

### E1 — edgd1er/nordlynx-proxy, run 1/10 (2026-09-08, ~15:46–15:48Z)
- Env : `NORDVPN_TOKEN` (credentials.env) + `CONNECT=Netherlands` +
  `GROUP=P2P`, `--privileged` (**requis**, sinon `sysctl disable_ipv6:
  permission denied` → échec, constaté au 1er essai).
- T1 : login TOKEN OK → `You are connected to Netherlands #824` →
  `END: container started` ≈ **100 s** (granularité grossière, à confirmer).
- T2 : **86.104.23.47** identique en direct, SOCKS5 (1191) et HTTP (8991).
- T3 : DNS OK. T4 : ✅ les deux proxys natifs.
- T5 : après `nordvpn disconnect`, SOCKS+HTTP en 000 instantané
  (fail-closed prouvé ; effet de bord : `docker exec` suivant a pendu —
  noté, cause probable : netns + killswitch DROP).
- Verdict : **PASS — CANDIDAT N°1**. Conteneur supprimé, slot libéré.

### E2 — edgd1er/nordlynx-proxy, run 2/10 (2026-09-08, 15:54:59–15:56Z)
- Même config. Login TOKEN OK, puis `connect nl846` → **`telio
  ConnectionError { code: ConnectionLimitReached }`** → retry nl815 →
  même refus → script exit 1. Aucun proxy démarré (normal).
- Verdict : **FAIL compte (session-limit), PAS backend**. Preuve que le
  throttle frappe aussi le flux officiel TOKEN (slots saturés, pas creds).
- Conteneur supprimé.

## Délais par lancement (toutes cibles, 2026-09-08)

Légende granularité : **exact** (inspect/logs au dixième) vs **~approx**
(reconstruit des sleeps, ±30 s — en italique la raison).

| Run | Backend | Début UTC | t_login | t_connect | t_egress | Total | Egress | Verdict |
|---|---|---|---|---|---|---|---|---|
| W1 manuel | nordlynx-key | ~15:40:15 | n/a (clé) | ~62 s | ~65 s | ~70 s | 186.247.163.40 NL | PASS |
| H-pilote 1 | nordlynx-key | 15:59:11 exact | n/a | 5,2 s | — (sonde unique) | 16,0 s | — | FAIL (sonde précoce, défaut harnais corrigé) |
| H-pilote 2 | nordlynx-key | 16:00:56 exact | n/a | 5,1 s | 110,1 s | 116,1 s | 186.247.163.192 NL | PASS |
| E1 manuel | nordlynx-proxy | ~15:46:1x | ~90 s ±30 | ~101 s ±30 | ~101 s | ~109 s ±30 | 86.104.23.47 | PASS |
| E2 manuel | nordlynx-proxy | 15:54:59 exact | < 28 s (login OK) | — | — | ~35 s | — | FAIL-compte (LimitReached nl846+nl815) |

Lecture : le `t_connect` WG varie de 5 s à 110 s selon le peer
(W1 : sélection serveur lente + handshake rapide ; H-2 : sélection
rapide + handshake lent) — d'où poll_egress 9×10 s et l'exigence des
10 runs. Le TOKEN-login (E1) coûte à lui seul ~90 s (daemon + API).

## Cause racine n°1 (2026-09-08 ~16:45Z) : DNS Docker Desktop mort

Preuves (conteneur jetable, zéro VPN) :
- `nslookup X 192.168.65.7` (forwarder embarqué) : réponse vide.
- `nslookup X 1.1.1.1` et `8.8.8.8` : `connection timed out`.
- Hôte : même requête via 103.86.96.100 = OK.
Verdict : **l'UDP/53 sortant des conteneurs est mort au niveau Docker
Desktop, l'hôte est sain**. Tout conteneur (re)créé depuis la panne :
pas de DNS → updater gluetun KO, healthchecks KO (github/cloudflare),
sondes KO → restart-loops → tempêtes AUTH → throttle compte → effondrement.
Les vieux conteneurs ont survécu tant que leur état établi tenait.
Correctif : restart Docker Desktop + remontée stagée (jamais 6 AUTH groupées).

### W1b — bubuntux/nordlynx via harnais (2026-09-08, 16:00:56Z)
- 1er run harnais : FAIL `no-egress` en 16 s — **défaut du harnais**
  (sonde unique trop précoce), pas du backend. Corrigé : `poll_egress`
  (9×10 s) + capture serveur + `t_egress` (harnais R2).
- 2e run : **PASS en 116 s** — `t_connect` 5,1 s mais `t_egress`
  110,1 s (handshake/peer lent), egress **186.247.163.192** (NL), DNS OK.
- Leçon : le temps d'egress WG est très variable selon le peer
  (5 s → 110 s observés) — d'où l'exigence des 10 runs (distribution,
  pas point unique). Données brutes : `logs/bench_runs.jsonl` (ignoré git).

| Backend | Runs | PASS | FAIL-compte | FAIL-backend | À confirmer |
|---|---|---|---|---|---|
| bubuntux/nordlynx | 3/10 | 2 | 0 | 1 (méthodo, corrigé) | T5/T6/T7 |
| edgd1er/nordlynx-proxy | 2/10 | 1 | 1 | 0 | T6/T7 |
| edgd1er/nordvpn-proxy | 0/10 | — | — | — | tout (après cooldown) |
| binhex OV | 0/10 | — | — | — | tout (après cooldown) |
| binhex WG | 0/10 | — | — | — | conf d'abord (pubkey serveur ?) |
| faeton OV | 0/10 | — | — | — | tout (après cooldown) |
| titidnh OV | 0/10 | — | — | — | tout (après cooldown) |
| dperson OV | 0/10 | — | — | — | baseline T1-T3 |
| linuxserver WG | 0/10 | — | — | — | AJOURNÉ sauf conf obtenable |
| alle | 0/10 | — | — | — | TOKEN + install + flow non-interactif |
| bubuntux/nordvpn CLI | 0/10 | — | — | — | TOKEN OK (testable comme E1) |
| gluetun OV/WG | réf. continue | prod | prod | prod | flotte live = banc permanent |

## Récupération 6/6 (2026-09-08, ~17:12Z)

Cause racine : DNS UDP/53 mort au niveau Docker Desktop (prouvé conteneur
vierge vs hôte). Correctif : `DNS_UPSTREAM_RESOLVER_TYPE: dot` (+
`cloudflare,google`) sur les 3 blocs compose (ancre + st1/st2 + canari),
volumes serveurs purgés, remontée stagée 60-70 s (6 AUTH espacées, 0 rejet).
Proxy redémarré avec `auto_mixed_stacks: false` temporaire (flotte saine
existante à préserver — pas de réassignation au boot).
Résultat : **6/6 connected, full, 0 bad-mark, 0 cooling** (s6 en
WireGuard avec egress FR confirmée). Première fois de la journée.
À surveiller 15 min (réapparition AUTH = throttle résiduel, sinon clos).
