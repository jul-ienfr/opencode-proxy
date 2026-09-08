# Session 2026-09-08 — audit routabilité : implémentation + crise prod + banc backends

Journal exhaustif de la session : code, opérations live, diagnostics avec
preuves, décisions, incidents de session, reste à faire. Heures UTC sauf
mention (locale = UTC+2).

## 1. Contexte d'entrée

- Doc d'audit `docs/audit-routabilite-stations-2026-09-08.md` (423 l.) :
  1 station routable/6, hypothèses H1–H10, findings F1–F11, 16 items PC-1–PC-16.
- Arbre git **déjà dirty** avant la session (refonte phases 5-9 en cours,
  fichiers `dashboard/api.py`, `ops/supervisor.py`, `static/*`,
  `observability/metrics.py`, `tests/test_vpn_freshness.py`,
  `docs/runbook-docker.md` modifiés ; non-trackés pré-existants :
  `tests/test_graceful_aurora.py`, `tests/test_upstream_event.py`,
  `pool_snapshot.json`, `docs/PLAN_COMPRESSION_HERMES.md`). Rien de tout
  cela n'est de cette session — ne pas l'attribuer, ne pas le committer
  en bloc.

## 2. Implémentations (toutes testées, suite verte ~1240 tests + mypy + ruff)

| Lot | Contenu | Fichiers |
|---|---|---|
| PC-1/PC-2/PC-3 | Budget restarts toutes voies (`_ensure_container`, `restart()` public), décision watchdog unique (`_decide_watchdog_action`, fin lignes F2) | `vpn/manager.py`, `tests/test_audit_routabilite_pc.py` (48 tests au total) |
| PC-4/PC-8/PC-15 | `boot_stagger_s: 30`, refresh liste serveurs 6h, heartbeat 5 min | `vpn/manager.py`, `opencode.py`, `config.yaml` |
| PC-5 | `free_model_spread` + `free_model_candidates` + compteur 429/modèle | `opencode.py`, `config.yaml` |
| PC-9/PC-10 | Grâce rotation (connecting servie), bad-mark par cause + garde N-2 + tri dégradé-dernier | `free/pool.py` |
| PC-6/PC-12 | Healthcheck compose = egress réelle ; image `:latest` prouvée SHA (revision `0fef7b2`, digest `sha256:89e3cb…` en commentaire) | `docker-compose.yml` |
| PC-7 | Canari bring-up raté = INDÉTERMINÉ (flip maintenu, pas de freeze) | `vpn/manager.py` |
| PC-11 | Périmètre pays refusé+loggé, `assigned_country`, `egress_allow_any_country` | `vpn/manager.py`, `config.yaml` |
| PC-13 | 4 clés mortes câblées (`display_lines`, `log_lines_max`, `flush_interval`, `quota_fetch_interval`), attach idempotent, test ratio 1.0 | `dashboard/display.py`, `dashboard/quota.py` |
| PC-14 | `scripts/config_coverage.py` + gate `--baseline` (243 clés : 148 consommées, 94 partielles, 1 morte légitime) | `scripts/`, `scripts/config_coverage.baseline.json` |
| PC-16 | `[free-usage]` → `client_ip` + `egress_ip` (ContextVar), `enforce_vpn_only` | `opencode.py`, `config.yaml` |
| O3-mixte | Slots `(n-1)%3` (WG/OV-TCP/OV-UDP), assignation au boot sans churn, retours WG réservés aux slots WG, `auto_mixed_stacks` (+rollback), `assigned_stack` en status | `vpn/manager.py`, `opencode.py`, `config.yaml` |
| Observabilité | `pool_station_usable`, `pool_usable_stations/floor`, `free_429_by_model` dans `/metrics` | `observability/metrics.py`, `opencode.py` |
| Détecteur externe | `_is_mutating_docker_op` + marquage ops propres + alerte `[extern] sN … HORS proxy` (StartedAt vs fenêtre 120 s) | `vpn/manager.py`, runbook |
| Sonde SOCKS | Double-passe 3 s → 8 s (faux « socks down » sous charge : handshakes froids mesurés 1-2,2 s) | `vpn/manager.py` |
| Docs | ADR-007 (+§ O3-mixte), runbook patterns 1/N + `[extern]`, CHANGELOG [Unreleased] | `docs/adr/`, `docs/runbook-docker.md`, `CHANGELOG.md` |

Fichiers créés par la session : `tests/test_audit_routabilite_pc.py`,
`scripts/config_coverage(.py,.baseline.json)`, `scripts/bench_backends.py`,
`docs/plan-banc-essai-backends.md`, `docs/banc-essai-resultats.md`,
`docs/adr/ADR-007-audit-routabilite-suites.md`, ce fichier.

## 3. Crise prod — chronologie vérifiée (UTC)

- Matin : 1/6 routable (snapshot audit), AUTH 310/h, 42 restarts/h, proxy instable.
- 13:28–13:46 : flotte (re)démarrée (6 conteneurs).
- 14:19:55 : proxy (re)démarré par l'opérateur (`opencode.py --gui`).
- 14:20–14:31 : 4 recreates manuels stagés (st1/st6/st3/st4) + wipe caches + pull `:latest` (build 01/09, hotfixes firewall/DNS).
- 14:35–14:40 : **vague de recreates inexpliquée** (6 stations, restarts=0, silence logs proxy).
- 14:4x–15:0x : throttle AUTH maximal (167/30min), pool 3/6 → 1/6.
- ~16:45 : **cause racine prouvée** (conteneur vierge) : UDP/53 mort au niveau Docker Desktop, hôte OK, TCP/443 OK (DoH).
- 17:0x–17:12 : plan B (DoT `cloudflare,google` sur 3 blocs compose), volumes purgés, remontée stagée 60-70 s → **6/6**, 0 AUTH rejetée.
- 17:15:52 : boot proxy avec flotte mixte active (slots loggés s1..s6).
- 17:39:42 : recreate s6 non attribué (ni proxy d'après logs, ni opérateur d'après lui) → détecteur `[extern]` construit en réponse.
- État final : **6/6 stables**.

## 4. Diagnostics prouvés (mécanismes, pas opinions)

1. **Throttle AUTH compte** : rejets multi-hôtes (fr969, nl1260, nl1070, nl1207) post-TLS OK, mêmes creds OK 10 min avant ; support NordVPN : ban 10 min qui se prolonge à chaque tentative ; plafond 10 sessions ( NordVPN > Mullvad/AirVPN à 5 — raison de rester).
2. **DNS Docker mort** : UDP/53 timeout conteneur (1.1.1.1, 8.8.8.8, forwarder 192.168.65.7), hôte OK, DoH/TCP-443 OK. Tout boot sans DNS = restart-loops → AUTH storms.
3. **Double pilote** : healthcheck interne gluetun (restart aveugle) vs watchdog manager. Non résolu : `HEALTH_RESTART_VPN=off` proposé, jamais appliqué (demande un recreate).
4. **Faux « socks down »** : budget sonde 3 s < handshakes froids 1-2,2 s mesurés → flag collant. Corrigé (double-passe), vérifié live (6/6 `tunnel_up_full` après restart proxy).
5. **Canari WG** : verdicts SANS EGRESS mesurés (90-100 s) toute la journée SAUF **PASS 30 s le 2026-09-08 14:59:18Z** → flip s6→WG appliqué. Chemin WG peer-dépendant (5 s à 110 s d'egress mesurés).
6. **F4 drift** : egress BR sur vieux conteneurs (historiques 187.14/15/40.x), résorbé par recreates.
7. **Verdicts canari potentiellement biaisés DNS** : la sonde canari résout un hostname — DNS mort = SANS EGRESS même sur tunnel sain. Correctif prévu : sonde IP littérale (NON FAIT, voir §6).
8. **bifrost** : mort seul (SQLite `unable to open database file`, 13:14Z), `restart=no`. Supprimé sur ordre (`docker rm -f`) ; image `maximhq/bifrost` (config non capturée — lacune).
9. **Deux pythonw** : proxy (4000) + webview GUI (pas un doublon — vérifié par ligne de commande).

## 5. Opérations docker exécutées (session)

pull `:latest` (+6 autres images de bench) · `rmi` tags v3.41.3/v3.40.0 (~90 Mo) · `rm -f opencode-wg-test` (canari en fuite, 4 j) · wipe caches `/gluetun/servers*` (exec + volumes alpine ×6) · recreates stagés st1/st6/st3/st4 (+ flotte complète au rebuild) · kills/starts proxy (PIDs 43004/32772, 37548, 13036, 20628) · `rm -f bifrost` (sur ordre) · Stop/rm complets pré-rebuild · bench containers (créés, testés, supprimés, slots libérés à chaque fois).

## 6. RESTE À FAIRE (engagements ouverts)

1. **Sonde canari en IP littérale** (promis, non fait) — faux SANS EGRESS possibles par DNS.
2. **`HEALTH_RESTART_VPN=off`** (proposé ×2, jamais appliqué — demande un recreate) — un seul pilote de tunnel.
3. **Banc OV** (edgd1er/binhex/faeton/titidnh/dperson : 0/10 chacun) — gate throttle dans le harnais, attendre pool ≥ 5/6 stable.
4. **T6/T7** (rotation hot-switch, stabilité 15 min) par backend.
5. **alle** : install + flow TOKEN non-interactif à défricher (bloqué : recherche, pas token).
6. **GUI multi-backend** (`backend` par station, multi-sélection — spec au plan, impl après résultats).
7. **PR retrait dual_station** (plan ADR-007).
8. **Re-pin v3.42** à sa sortie (pas sortie le 2026-09-08).
9. **Rotation TOKEN** : secret apparu en transcript (voir §7).

## 7. Incidents de session (à traiter)
- ** fuite secret** : valeurs de `vpn_configs/credentials.txt` affichées par une commande d'inventaire dans le transcript. **Rotation des credentials de service recommandée** (dashboard NordVPN → manual setup). Le TOKEN transmis ensuite est stocké uniquement dans `credentials.env` (ignoré git, jamais ré-affiché).
- Fichiers temporaires hors repo (`AppData\Local\Temp\opencode\*.py`, pool_summary) : pas de secrets dedans (vérifié : bench_nordlynx.env shreddé, store_token.py supprimé).
- `bench_pilot.jsonl` → `logs/bench_runs.jsonl` + règle `.gitignore` ajoutée.

## 8. Faux échec commit (résolu) : test ox-alpha dépendant du .env

`test_dead_identity_falls_through_to_live_free` échouait sur checkout
vierge mais passait ici. Cause prouvée (sondes croisées) : `DISABLE_MAPPING`
vaut True uniquement avec le `.env` opérateur (gitignoré) — sans lui,
`_route_for("ox-alpha-free")` prend une autre branche (MODELS/FREE/POOL
identiques par ailleurs). Ni le commit ni l'audit en cause (logique de
routage intacte). Fix : pin `DISABLE_MAPPING=True` dans le test (hermétique, docstring
du code l'autorise) + `oc._route_cache.clear()` (un test antérieur peut
y avoir figé une résolution sous un autre mapping — prouvé par
empoisonnement volontaire : `ox-alpha-free` servi du cache, test vert
quand même après le fix). Commit `4872447`, suite
complète re-vérifiée verte sur worktree vierge.
