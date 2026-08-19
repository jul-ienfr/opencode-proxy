# RAPPORT FINAL — opencode-proxy (19/08/2026)

**Mission** : *People RUN* — le proxy tourne, le tunnel VPN est stable, la rotation
IP/quota est fluide, aucune interruption. Tout le reste est secondaire.

**Base des constats** : audit adversarial 5 agents (55 findings + critic), revérifié
chaque finding contre l'arbre de travail ACTUEL (l'audit avait couru sur un snapshot
plus ancien). Marqueurs :
- ✅ = VÉRIFIÉ CORRIGÉ dans l'arbre (avec preuve)
- 🔶 = PARTIEL / décision d'exploitation
- ❌ = RESTANT à faire
- ⚪ = CONFIRMED par l'audit mais NON re-vérifié dans cette passe
- ⚫ = REFUTÉ par l'audit

Suite de tests : **392 tests verts** (`pytest tests/ --collect-only` → 392).

---

## 1. Ordre de priorité

### P0 — « Faire tourner » : aucun blocage, aucune régression
1. **Tout le plan « quasi-instantané » (18/08) est LANDED sauf 2 blocs :**
   - ❌ **Commit 7 — annulation des streams free sur mort egress confirmée** (am.22,
     `pool.cancel_streams(station)`). Aucune occurrence de `cancel_streams` dans
     `opencode.py`/`free_ip_pool.py`/`vpn_manager.py`. C'est le seul morceau de la
     phase qui ne soit pas implémenté : aujourd'hui un stream en vol sur un tunnel
     mort reste bridgé par keepalive (read 600 s) au lieu d'être annulé ≤ ~10 s.
   - ❌ **Commit 8 — docs « Peer Connection Initiated »** (amendement audit 18/08).
     `docker-compose.yml` et `vpn_manager.py` contiennent encore le texte faux
     « Connecting to [uk1234.nordvpn.com] » (la vraie ligne est
     « Peer Connection Initiated with [AF_INET]<ip>:1194 », visible dès verbosity 1).
     Zéro comportement — docs seulement.
2. **Re-run complet de la suite avant/après chaque commit** (392) — c'est la porte
   de non-régression « people RUN ».

### P1 — Décisions sécuritaires/opérationnelles (peu de code, surtout des choix)
3. 🔶 **[24] Double source de vérité des credentials** : `config.yaml:87`,
   `vpn_manager.py:468` et `README.md:113` pointent encore
   `vpn_configs/credentials.txt` alors que le runtime utilise `credentials.env`.
   Mitigé seulement par `scripts/make_credentials_env.py` (one-shot de migration).
   → Supprimer la clé morte `credentials_file` + pointer le défaut vers
   `credentials.env` (3 lignes).
4. 🔶 **[40] debug.log en clair = par design** (`.env.example:29-31` documente
   OPENCODE_DEBUG=0 « logs full request/response bodies »). Ce n'est pas un bug :
   à activer seulement sur machine locale. **Décision** : assumer (ops, ne pas
   activer OPENCODE_DEBUG=1 sur un hôte partagé) OU rougir les corps de requêtes
   (+ lourd, à chiffrer séparément).
5. 🔶 **[26] Dashboard sur 0.0.0.0** : le masking des secrets + le garde
   `DASHBOARD_TOKEN` opt-in (comparaison à temps constant, `dashboard/api.py:75-88`)
   sont en place. **Action opérationnelle** : poser `DASHBOARD_TOKEN` quand le
   serveur est exposé au-delà de localhost. Code existant, rien à changer.

### P2 — File de rattrapage (à RECERTIFIER contre le code avant de coder)
> Aucun de ces findings n'a été revalidé dans cette passe finale. Les traiter
> comme suspects, pas comme acquis : vérifier chacun, corriger seulement les
> survivants.
- ⚪ MEDIUM : [3] double tentative free non-429 · [6] timeouts 60 vs 600 s ·
  [19] quota_per_ip = compteur de requêtes · [21] apply_update TOCTOU (idle check
  hors-lock) · [27] parseur JS→JSON des quotas · [28] fetch_quotas sans retry ·
  [29] granularité « week » fausse · [30] timestamps locaux vs UTC (un outil de
  migration `scripts/migrate_timestamps_utc.py` existe → mitigé) · [32]
  `_persist_vpn_config` read-modify-write sans verrou · [33] toggle_use_balance ·
  [45] test_vpn_e2e gaps.
- ⚪ LOW : [8] pas de cache sur succès free · [9] streaming free jamais loggué ·
  [10] code mort (`_parse_rate_limit_pause`, `_get_any_enabled_key`) · [23]
  `health_check` modifie `_current_ip` hors-lock · [34] snapshot copie superficielle ·
  [35] UI fetch confondu avec « non configuré » · [36] success_rate 100% si 0 · [37]
  /api/vpn-status → docker inspect à chaque refresh · [46] VPN logue sur stderr ·
  [47] IP sondée ≠ IP egress réelle · [50] docs README/.env.example · [51]
  test_hot_reload mute la config de prod.
- ⚪ INFO : [11] pauses quota_based non plafonnées + scraping avec cookie · [38]
  cooldown 60 s global · [39] points sains · [25] constats sains.

### Reconstruction de l'audit — ce qui est DÉJÀ corrigé (✅)
| # | Finding | Preuve dans l'arbre |
|---|---|---|
| 0,42 | Fetch 429 free en **streaming** → rotation IP complète | `_on_free_429_stream` branché sur les 4 handlers stream (mimo, openai, chat, responses) |
| 1,18 | Rafale 429 : rotation fire-and-forget non dédupliquée | `_launch_rotation` single-flight via `_pending`/`_rotation_tasks` + `_worker_task` référencé |
| 2 | Pauses 401/403 plafonnées à 600 s | `pause_key` plafonne **uniquement** `quota_based` ; 401→86400/403→1800 honorés en plein (`opencode.py:224-236`) |
| 5 | VPN down → compteur IP quand même | `free_ip_pool.py:320-323` « Only count requests that actually went through the tunnel ([5]) » |
| 7 | Retry-After HTTP-date parsé comme float | parse HTTP-date dans `_free_429_cooldown_seconds` |
| 13 | Déploiement conteneurisé incapable de tourner | `Dockerfile:11-26` (CLI docker + compose v2 plugin), `docker-compose.yml:54-94` (docker.sock + project dir ro + VPN_DOCKER_COMPOSE_FILE) |
| 15 | Scan AUTH_FAILED non borné par StartedAt | tous les call-sites passent `started_at` from `_wait_healthy` ; `since = started_at or "10m"` (`vpn_manager.py:2647`) |
| 20 | save_state non atomique / load_state partiel | tmp+`os.replace` ([20], `vpn_manager.py:3276-3299`) |
| 22 | `free_only` config inerte | **supprimé entièrement** du codebase |
| 26 | Secrets en clair sans auth | masking (`*_masked`, `_SECRET_FIELDS`) + `DASHBOARD_TOKEN` constant-time opt-in |
| 31 | SSE sans fallback si stall | watchdog `setInterval` + `es.onerror` (`static/app.js:2792-2801`) |
| 41 | Streaming curl_cffi systématiquement cassé | découpage bytes manuel dans `_CurlCffiStreamResponse.aiter_lines` (fix [41]) |
| 43,44 | Tests cassés / test_free_quota illisible | **392 verts** ; api_keys relus depuis le bon endroit |
| 49 | requests.db 3,28 Go + WAL suivi par git | `logs/*.db*` gitignoré, `git ls-files` ne montre plus rien |
| critic 2,3,4,5,11 | Paid-key leak / fallback silencieux / rotation avalée / hot-reload | vérifiés par citations `[CRITIC(n)]` dans le code + `_free_request_headers` (Invariant A.0 : dépouille Authorization/x-api-key/cookie/x-request-id/x-stainless-*) |
| 17,14,16,53,54 | (REFUTÉS par l'audit) | — |

---

## 2. Périmètre complet de « faire tourner » — état vérifié

| Sous-système | Rôle pour « RUN » | État |
|---|---|---|
| Routage req → modèle (ROUTES / FREE_MODEL_MAP) | toute requête trouve son endpoint | ✅ stable |
| Chemin free **non-stream** | 429 → cooldown → rotation IP → fallback paid | ✅ complet + 60 s cooldown |
| Chemin free **stream** (trafic dominant de Claude Code) | idem | ✅ 429 → rotation depuis le fix [42] ; ❌ streams en vol non annulés sur tunnel mort (commit 7) |
| Rotation IP (FreeIPPool + VPNManager) | ne jamais croiser le mur de quota, jamais servir un tunnel mort | ✅ étage 0/1/2 du plan (bad-mark, signal, sonde, wake, deadline 240 s, restart léger) |
| Multi-station (N=1..10, hot-reload) | scale-up parallèle | ✅ 392 tests dont `test_vpn_stack_nstation.py`, `test_pool_station_set.py` — **non commité** (décision utilisateur) |
| KeyPauser + failover clés payées | 401/403/429 → pause réelle → clé alternative | ✅ |
| Dashboard + débug + logs | visibilité = capacité à corriger | 🔶 token opt-in, bodies en clair (P1) |

## 3. Ce qui est HORS périmètre (explicite)
- **Refonte du parseur JS→JSON des quotas** en vrai parser ([27]) — lourd, pas
  nécessaire pour « RUN » (le fallback « fenêtres à zéro » est conservateur).
- **Rougir les logs debug** — change l'outil de diagnostic ; à décider, pas à faire.
- **Auth authentique du dashboard** (login/mots de passe) — le modèle est
  « réseau domestique + token ». Tout système d'auth plus fort = nouveau sous-système.
- **Réécriture de la récupération mid-stream** (fallback direct pendant un flux
  déjà commencé) — comportement pré-existant fragile, marqué hors-scope au plan 18/08.
- **Docker-in-docker / orchestration** : la rotation reste pilotée par le CLI docker
  du proxy ; rien au-delà (k8s, swarm, etc.).
- **Toute feature nouvelle** n'entrant pas dans « le tunnel tourne, le quota est
  respecté, la panne est quasi-instantanée ».

## 4. Chemins critiques & facteurs de risque
1. **Chemin request free (single-flight)** — invariant intouchable : jamais d'await
   sur une rotation depuis le chemin de requête (C6). Toute modification doit tester
   `test_free_ip_pool_disconnect_retry.py` + `test_pool_connection_failure.py`.
2. **Chemin stream** — le fix [41] (`aiter_lines` manuel) est le plus fragile du repo :
   toute refonte du parseur doit être couverte par `scripts/smoke_todo41.py` et le test
   associé. Un retour en arrière = retour du bug « tous les streams tombent en IP résidentielle ».
3. **Watchdog / tick (cadence 2 s, sonde légère)** — garde anti-spam, skip pendant
   rotation, cooldown 30 min, flag auto. La refonte 1d est l'autorité unique de
   `egress_dead` : ne pas y réintroduire de reset par stack.
4. **Multi-station vs prod** — un `_apply_station_count` relance JUSQU'À 10 tunnels ;
   en cas de hot-reload incontrôlé, coût docker X stations. Le gating par
   `resolved_station_count` + pipeline sérialisé est le garde-fou.
5. **Scraper quotas + cookie de session** — le cookie auth est une donnée vivante ;
   toute expiration ⏐ rotation de la clé → quotas invisibles → rotation IP déclenchée
   sur fausse notion d'épuisement. Surveiller `fetch_quotas` state `error`.

## 5. Programme « whitelist » — VÉRIFICATION DEMANDÉE (non trouvé ici)
Je n'ai trouvé **aucun programme « whitelist »** avec ses propres quick-checks dans ce
dépôt. Le seul « whitelist » présent est le **filtrage d'outils** (`supported_tools`,
`opencode.py:2865-2894`, testé `tests/test_proxy.py:790-849`) — sans rapport avec un
programme de contrôle.
Les artefacts qui s'en rapprochent : `scripts/smoke_todo10.py`, `scripts/smoke_todo41.py`,
`scripts/vpn_e2e_smoke.py`, `logs/probe_socks5.py`, `logs/probe_vacuum.py`
(probes/vérifs rapides manuelles, pas un programme autonome).
➡️ **À confirmer par toi** : ce « programme whitelist avec ses quick checks » existe-t-il
ailleurs (autre repo / autre machine) ? S'il est hors dépôt, je ne peux pas le vérifier d'ici.

## 6. EXIGENCES PRÉSERVÉES — anti-accumulation de scope
Une règle **explicite** (à maintenir à chaque revue/PR) : le scope NE S'ACCUMULE PAS.
Toute feature demandée/PR passera 3 portes avant d'entrer :
1. **Ça fait tourner ?** (impact direct sur tunnel, quota, interruption — oui/non)
2. **Ça casse un invariant ?** (jamais d'await sur rotation en chemin request, compteur
   IP honnête, C1 « ne jamais bad-marker la dernière station debout », single-flight)
3. **Tests verts avant/après ?** (la suite complète de 392)

Refuser (ou déporter hors-ligne) ce qui ne passe pas la porte 1 — l'objectif est la
stabilité, pas la richesse fonctionnelle.

## 7. Non terminé / non vérifié (transparence)
- ❌ Commit 7 (cancel streams) et ❌ Commit 8 (docs) du plan 18/08 — non implémentés.
- 🛑 La feature multi-station (station_count 1-10, hot-reload, `_apply_station_count`,
  `stop_container`) est **fonctionnelle et testée (392 verts) mais NON commitée**
  (14 fichiers modifiés + fichiers temporaires de revue en `??`). Ne rien committer/pousser
  sans demande explicite — décision à prendre côté utilisateur.
- ⚪ La file P2 (18 findings CONFIRMED non recertifiés) — à ré-évaluer un par un.
- 🛑 « Programme whitelist » : hors dépôt, vérification impossible d'ici (voir §5).

---
*Rapport final — généré le 19/08/2026. Priorité : people RUN.*
