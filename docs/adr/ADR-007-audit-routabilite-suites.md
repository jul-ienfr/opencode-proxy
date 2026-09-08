# ADR-007 — Audit routabilité 2026-09-08 : décisions structurantes

Date: 2026-09-08
Status: accepted (implémenté PC-1→PC-16, suite verte)
Contexte : `docs/audit-routabilite-stations-2026-09-08.md` (1 station
routable sur 6 : AUTH_FAILED, restarts watchdog non bornés, bad-mark
uniforme, pins hors périmètre). Chaque clé config doit être lue ET avoir
un effet observable + 1 test (O1) — outillé par
`scripts/config_coverage.py` + gate `--baseline`
(`scripts/config_coverage.baseline.json`).

## Décisions

1. **C1 strict → garde N-2** (`free/pool.py`) : une station marquée qui
   ferait passer les éligibles sous `max(2, N-2)` reste servie en
   « dégradé » (marqueur posé, dernier recours au tri, log
   `[pool] invariant_n2`) au lieu d'être exclue. Jamais 0 candidat tant
   qu'un tunnel est up. Tests `test_pool_station_set.py` mis à jour.
2. **Fail-closed par défaut préservé** : `free_model_spread: true` livré
   avec `free_model_candidates: {}` (no-op tant que vide) ;
   `enforce_vpn_only: false` (opt-in, mode vpn/socks5 déjà sans repli
   direct) ; `egress_allow_any_country: false` (pins hors périmètre
   refusés + `[POLICY]`).
3. **Image gluetun `:latest` voulu** (pas par négligence) : v3.41.3 ne
   contient pas les hotfixes master post-30/07 (firewall netlink 01/09,
   iptables vs Docker NAT 23/08, crash control-server 31/08, servers
   data v0.2.0) qui touchent directement cette flotte. Re-pinner en
   version dès v3.42.0. `docker compose pull` frais au déploiement.
4. **Schéma SQLite gelé (ADR-006) respecté** : `free-usage` ne gagne
   aucune colonne — la désambiguïsation F9 (`client_ip` vs `egress_ip`)
   est ligne-de-log uniquement.

5. **Flotte mixte O3** (`auto_mixed_stacks`, défaut true) : slots
   déterministes `(n-1) mod 3` (WG / OV-TCP / OV-UDP), assignés au boot
   sans churn ; retours WG réservés aux slots WG (preuve canari) ;
   `wireguard`/`openvpn` restent uniformes stricts. Rollback :
   `auto_mixed_stacks: false`. Canari OV-TCP dédié et rééquilibreur
   actif NON construits (OV prouvé par pin catchup ; remplissage
   opportuniste) — voir réanalyse 2026-09-08.

## Plan de retrait `dual_station` et clés `*_2` (non exécuté)

Lecteurs actuels : `config/loader.py:83` (fallback
`resolved_station_count`), `free/pool.py:364` (propriété),
`dashboard/api.py:698-732` (mapping hot-reload), `vpn/manager.py:3417`
(miroir `get_config`) ; tests verrouillant le legacy :
`test_rotation_n_stations.py`, `test_station_count_config.py`,
`test_vpn_config_http.py`. Étapes : (a) GUI n'écrit plus `dual_station`
ni `*_2` (écrire `station_count`) ; (b) loader : warning de dépréciation
1 release, fallback conservé ; (c) suppression loader + miroirs +
   mapping dashboard + clé `config.yaml`, tests legacy retirés dans le
   même commit. `credentials_file` suit le même sort (source unique :
   `credentials.env`).

## Conséquences

- `test_pool_station_set.py` documente le nouvel invariant (marquée +
  servie) ; l'ancien C1 strict (« jamais marquée ») est abandonné
  explicitement.
- Toute nouvelle clé `config.yaml` livre son test d'effet dans la même
  PR (gate baseline : MORTE hors baseline = échec).
