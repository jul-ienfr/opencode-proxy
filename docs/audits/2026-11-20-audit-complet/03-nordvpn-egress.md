# 03 — Audit NordVPN / egress / stations

**Date** : 2026-11-20
**Périmètre** : `vpn/manager.py` (11 471 l.), `vpn/auth.py`, `vpn/__init__.py`,
`vpn_manager.py`, `free/egress_adapters/nordvpn.py`, `free/station_identity.py`,
`free/identities.py`, `free/identity_generator.py`, `free/runtime_identity.py`,
`free/impersonation.json`, `free/wire_profile.py`, `free/station_health.py`,
`vpn_configs/`, `gluetun/`, `docker-compose.yml`, `station_repair.py`,
`station_supervisor.py`, `scripts/fresh_chain*.py`, `rotation/`.
**Mode** : lecture seule — aucun fichier existant modifié.

---

## 1. Résumé exécutif

Le sous-système NordVPN fonctionne, mais **deux chemins de provisionnement
coexistent et se contredisent** : `docker-compose.yml` déclare l'image upstream
pour 9 stations sur 10, alors que les 6 conteneurs vivants tournent le fork
`gluetun-016:local` — maintenu uniquement par des overlays *non versionnés* dans
`logs/`. Tout `compose up` de `vpn/manager.py` (8 sites, un seul `-f`, zéro
`--profile`) ramène donc la station sur l'image upstream, dont l'API de contrôle
v2 est absente : les rotations par API tombent en 404. Le même défaut frappe
l'auto-update, qui compare et pull l'image `qmcgaw/gluetun` pendant que la flotte
tourne autre chose. En configuration livrée (`repair_owner: supervisor`,
`station_repair.enabled: false`), ~2 000 lignes du watchdog et de la réparation
auth de `vpn/manager.py` sont mortes par un `return` à la ligne 9848. Enfin,
`get_public_ip()` — l'autorité de validation d'une rotation — retourne une IP
**auto-déclarée par gluetun** 2 fois sur 3, sans preuve d'egress, ce qui
contredit frontalement le principe VII. Le fichier a grossi de 8 559 → 11 471
lignes depuis l'audit du 24/10 (+34 %) sans aucune extraction, contre le
principe XIV (« aucun nouveau module ne dépasse 1 500 lignes »).

---

## 2. Carte de `vpn/manager.py` (11 471 lignes)

Table des matières générée par extraction des en-têtes `# ──` (26 sections) et
des `def` de premier/second niveau.

| Lignes | LOC | Section | Rôle | Appelants principaux |
|---|---:|---|---|---|
| 1–118 | 118 | En-tête, imports, globals | `_GLOBAL_FAILED_HOSTS`, `_ENV_RW_LOCK`, `_STACK_FLIP_LOCKS`, `_STATION_AUTH_GUARD` | — |
| 119–466 | 348 | Docker Desktop auto-launch (Windows) | `configure_docker_autostart`, `docker_daemon_status`, `request_docker_autostart`, `ensure_docker_running`, `_docker_desktop_process_running`, `_disable_docker_desktop_ui_on_startup`, `_start_docker_desktop` | `opencode.py:3353`, watchdog 10192 |
| 467–516 | 50 | Identity profiles : porteurs TLS | `_KNOWN_IMPERSONATIONS`, `_DEFAULT_IDENTITY_PROFILE` | `vpn_manager` shim, `__init__` |
| 517–686 | 170 | Identity profiles : client officiel | `OFFICIAL_*`, `_OFFICIAL_UA_BY_CLIENT`, `_BROWSER_HEADER_NAMES`, `_official_ua_for_client`, `_looks_like_browser_ua`, `_sanitize_extra_headers`, `_normalize_identity_profiles`, `_headers_key`, `_build_identity_pool` | `__init__:1610-1613`, `update_config` |
| 687–726 | 40 | `VPNState` | Machine à états + `is_up()` | partout |
| 727–807 | 81 | `CircuitBreaker` | Breaker par serveur | `__init__:1770`, `dashboard/api.py:1228` |
| 808–846 | 39 | `BackoffTimer` | Backoff exponentiel | `__init__:1774,1785` |
| 847–1971 | 1 125 | Déclaration classe + helpers purs + `__init__` | `RotationFailed`, `AuthCoolingDownError`, `_sh_quote`, `_normalize_country`, `_extract_current_ip`, `_parse_server_issue_detail`, `_clamp_cfg_number`, `_host_ttl_seconds`, `_classify_probe_exc`, `_classify_error_kind`, `_extract_current_hostname`, `_is_mutating_docker_op`, `__init__` (784 l.) | `opencode.py` lifespan, `free/pool` |
| 1972–2091 | 120 | Propriétés publiques | `enabled`, `proxy_mode`, `status`, `current_ip`, `current_identity`, `_live_identity`, `current_server`, `proxy_url`, `socks5_url`, `_compose_file_path` | `free/pool`, dashboard |
| 2092–2748 | 657 | Lifecycle | `start`, `ensure_watchdog`, `_startup_connect`, `stop`, `stop_container`, `connect`, `_check_rotation_storm`, `connect_next`, `request_rotation_cancel`, `_connect_next_impl` (266 l.) | `opencode.py`, `free/pool:5780` |
| 2749–2856 | 108 | Transitions + events | `_set_status`, `_publish_vpn_event`, `_on_container_event` | `docker_events.py`, SSE |
| 2857–4234 | 1 378 | Status & health | `refresh_status` (577 l.), `health_check`, `_get_probe_client`, `_probe_url`, `_valid_ip`, `_socks_public_ip`, `_http_public_ip`, `get_public_ip`, `_socks_egress_ok`, `_control_put_countries`, `_maybe_pin_widen/restore`, `_note_effective_ok`, `_probe_tunnel_light`, `_probe_connect` | watchdog, dashboard, `free/pool` |
| 4235–4465 | 231 | Fraîcheur IP + identité | `_ip_recent`, `_ip_in_use_by_other`, `_record_ip_change`, `_advance_identity`, `_finalize_ip`, `_commit_ip`, `_adopt_boot_ip` | rotation, `refresh_status` |
| 4466–5301 | 836 | Config | `get_config` (151 l.), `update_config` (362 l.), `update_health_policy`, `_country_for_host`, `get_status` (283 l., 126 clés) | dashboard |
| 5302–5619 | 318 | Docker helpers | `_docker_run` (funnel rotation), `_read_live_compose_pin`, `_compose_env` (112 l.), `_await_rotation_ops_drained`, `_docker_inspect`, `restart`, `_docker_op_begin/end`, `_docker_restart` | partout |
| 5620–7014 | 1 395 | Client control-server gluetun | `_control_exec`, `_control_status`, `_control_public_ip`, `_control_pin_country(_gated)` (247 l.), pays, `ensure_geo_egress`, `_fast_recover_via_control`, `_compose_up(_impl)`, `_ensure_container`, pin compose, `_refresh_server_list`, `_fetch_nord_loads`, `_least_loaded_hostnames` | rotation, watchdog |
| 7015–7224 | 210 | Flotte hétérogène | `_MIXED_SLOTS`, `_mixed_active`, `_mixed_slot`, `apply_mixed_slot`, `_auto_flip_decision` (164 l.) | `opencode.py:2691,3478` |
| 7225–7292 | 68 | Classification de flip | `_flip_is_auth_driven`, `_flip_is_churn_driven`, `_emergency_flip_decision` | tick watchdog |
| 7293–7933 | 641 | Canari WG + application de stack | `_wg_key_present`, `_canary_probe_once`, `_wg_canary_alive`, `_cancel_wg_flip_if_canary_dead`, `_apply_stack(_impl)`, `_sync_stack_env`, `_apply_ovpn_protocol` (196 l.) | tick, `opencode.py`, `scripts/station_env_reconcile.py` |
| 7934–8110 | 177 | Cascade WG→OV | `_cascade_*`, `set_stack`, `stack_info` | tick, dashboard |
| 8111–8589 | 479 | Réparation auth bornée | `_park_egress`, `_note_pool_*`, `_record_last_good`, `_auth_transport`, `_configure_auth_guard`, `_park_auth`, `_stop_service`, `_pin_last_good`, `_launch_auth_repair`, `_auth_repair_loop`, `_auth_probe_after_repair`, `_auth_repair_succeeded`, `_check_auth_failed` | tick (**mort en config livrée**) |
| 8590–8714 | 125 | Cooldown AUTH local | `_prune_auth_times`, `_auth_cooling`, `_auth_cool_remaining_s`, `_note_live_auth` | `_check_auth_failed`, `connect_next` |
| 8715–9376 | 662 | Watchdog : grâce + budget | `_egress_dead_threshold`, `_watchdog_restarts_recent`, `_watchdog_budget_exhausted`, `_note_watchdog_restart`, `_decide_watchdog_action` (128 l.), `_record_auth_failure`, `_host_blacklisted`, `_breaker_charge_*`, `_check_server_issue`, `_check_restart_churn`, `_tunnel_up_via_control`, `_launch_socks_catchup`, `_wait_healthy` (130 l.) | tick |
| 9377–9662 | 286 | Auto-update image | `note_free_*`, `_docker_image_id`, `_docker_repo_digest`, `check_update`, `_acquire/_release_update_lock`, `apply_update`, `_rollback_update`, `_update_opportune`, `_updater_loop` | station 1, dashboard |
| 9663–10886 | 1 224 | Auth watchdog / tick | `arm_egress_watchdog`, `_watchdog_mark_recovered`, `_watchdog_recover_fresh_ip`, `_watchdog_loop`, `_detect_external_container_change`, `_watchdog_tick` (wrapper), `_watchdog_tick_impl` (**962 l.**), `_watchdog_escalate`, `_resolve_gluetun_volume`, `_refresh_servers_list` | `_watchdog_loop` |
| 10887–11198 | 312 | Persistance d'état | `_get_state_path`, `_save_state_sync/async/debounced`, `save_state`, `load_state` (308 l.) | boot, rotations |
| 11199–11471 | 273 | Boot reconcile + patch de classe | `_docker_cli`, `_default_docker_runner`, `_env_value_from_inspect`, `_stack_from_env_file`, `reconcile_orphan_containers`, `_serialized_refresh` + monkey-patch `VPNManager.refresh_status` | `opencode.py:3625` |

**Métriques de complexité** : 198 méthodes dans `VPNManager`, 338 `except
Exception`, 288 corps d'`except` réduits à `pass`, 298 lectures
`getattr(self, "_x", default)` défensives, 42 `return` dans le seul
`_watchdog_tick_impl`, 40 horloges/cooldowns distincts
(`_auth_cool_until`, `_auth_grace_until`, `_auth_park_until`, `_parked_until`,
`_auth_global_pause_until`, `_storm_cooldown_until`, `_pin_widened_until`,
`_flip_annule_cooldown_until`, `_egress_dead_since`, …).

---

## 3. Cycle de vie réel d'une station NordVPN

Séquence implémentée, avec les écarts constitutionnels.

### 3.1 Provision — `_compose_up` / `_ensure_container`

`docker compose -f <compose_file> up -d [--force-recreate] <service>`
(`manager.py:6534-6553`, `6555-6584`).

**Écart majeur** — un seul `-f` est passé (`manager.py:2239, 6543, 7361, 7614,
7884, 8291, 9431, 9510, 9578`) et `--profile` n'apparaît **jamais**
(`grep -c -- "--profile" vpn/manager.py` → `0`). Or les conteneurs vivants
reçoivent leur image et leur configuration control-plane par des overlays
`-f logs/compose-control-plane-N.yml` écrits hors du gestionnaire
(`free/ip_registry/adapters/compose_binder.py:334-357`, et à la main selon
`ops/runbooks/control-plane.md:38-64`). Toute recréation ordonnée par
`manager.py` **perd l'overlay** : voir F-P0-1.

### 3.2 Connect — `connect()` / `_connect_next_impl`

`_auth_gate()` (slot global, `vpn/auth.py:63-95`) → `_compose_up()` →
`_auth_connect_done()` → `_wait_healthy(120)` → `_check_auth_failed()` →
`_pin_country_for_rotation()` → `_finalize_ip()` → `CONNECTED`
(`manager.py:2256-2347`).

**Écart VI** — `_auth_gate` impose **2 connexions simultanées max** et 12 s /
30 s d'espacement (`vpn/auth.py:36-38, 79-86`). C'est un verrou *inter-stations*
posé sur une ressource **de compte**, pas de station. La constitution VI
n'interdit que les verrous *de transport* et l'exclusivité d'IP ; ce verrou est
donc licite au sens strict, mais il n'est **pas** un compteur de sessions : rien
dans le code ne connaît le nombre de sessions NordVPN ouvertes. `max_active_stations`
n'existe que dans `free/egress_adapters/nordvpn.py:36-40` et ne gate que des
*observations* (le module ne ouvre jamais de session — cf. son docstring
l. 1-8). Voir F-P1-3.

### 3.3 Bind — ports SOCKS5/HTTP

`proxy_url` = `socks5://…` / `http://127.0.0.1:{8887+N}` (`manager.py:2049-2078`).
Le bind est **déclaratif** : `manager.py` ne teste jamais que le port répond
avant de servir — c'est `_probe_tunnel_light` qui le découvre a posteriori.
Conforme au principe XI (chaîne de fallback SOCKS → HTTP → control).

### 3.4 Probe — `_probe_tunnel_light` / `get_public_ip`

Deux autorités distinctes :
- **egress** : `_probe_tunnel_light` (`manager.py:4052-4118`) — SOCKS5 CONNECT,
  puis fallback `control_status + control_public_ip`, puis HTTP-proxy.
- **identité d'IP** : `get_public_ip` (`manager.py:3673-3740`) — ordre tournant
  `_ip_method_idx` où **control passe en premier 2 fois sur 3**.

**Écart VII majeur** — `_control_public_ip` retourne l'IP **auto-déclarée par
gluetun** (`manager.py:5720-5732`, docstring : *"self-reported, not a tunnel
probe"*). `_finalize_ip` → `_commit_ip` (`manager.py:4310-4418`) enregistre
cette valeur dans le registre partagé, avance l'identité et journalise
l'historique **sans aucune preuve d'egress**. Voir F-P1-1.

### 3.5 Serve

`refresh_status` (cache 5 s, `_STATUS_CACHE_SECONDS`) + `_on_container_event`
(docker events) + `_probe_tunnel_light` au tick. Le pool (`free/pool`) route sur
`VPNState.is_up()` + `station_contract` (`free/station_health.py:380-400`), qui
lit 5 attributs **privés** du manager (`_auth_park_until`, `_repairing`,
`_parked_until`, `_egress_state`, `_last_effective_ok_at`) — couplage
bidirectionnel non contractuel (F-P2-6).

### 3.6 Repair

Deux chaînes coexistent :
1. **manager** : `_watchdog_tick_impl` (`manager.py:9842-10802`, 962 l.) +
   `_auth_repair_loop` (`8334-8438`) + `station_repair.RepairPolicy`.
2. **superviseur** : `scripts/fresh_chain_supervisor.py` (3 294 l.) →
   `scripts/fresh_chain.py` (3 242 l.), lancés par `opencode.py:2396`.

**Arbitrage par configuration seule** : `ip_rotation.repair_owner: supervisor`
(`config.yaml:289`) → `_watchdog_tick_impl` retourne à la ligne 9859 après un
simple `refresh_status()` ; `ip_rotation.station_repair.enabled: false`
(`config.yaml:690`) → `_station_repair_enabled = False` → `_egress_repair_threshold()`
retombe sur `_egress_dead_threshold()` (`8732-8736`) et `_decide_watchdog_action`
saute tout le bloc egress (`8802`) et le bloc auth (`8849`).

**Conséquence** : en configuration livrée, **962 lignes de tick + 479 lignes de
réparation auth + 125 lignes de cooldown local = ~1 570 lignes mortes**, plus la
réparation bornée de `station_repair.py`. Un simple basculement de
`repair_owner` dans le GUI ressuscite deux réparateurs concurrents — la course
que le commentaire de la ligne 9849 décrit comme déjà survenue.

**Écart VIII** — le principe dit « le superviseur est le SEUL réparateur ». Le
code maintient **quatre** déclencheurs de réparation : le tick manager, le
superviseur out-of-process, `ops/supervisor.StationSupervisor` (in-process), et
`free/qualification/watchdog.py:128` + `repair_adapter.py:18`. Trois sont
actifs simultanément.

### 3.7 Tableau des écarts constitutionnels

| Principe | Écart | Preuve |
|---|---|---|
| **II** (3 objets, 3 cycles de vie) | Aucun compteur de sessions de compte ; `max_active_stations` ne gate que des observations | `free/egress_adapters/nordvpn.py:36-40,123` |
| **III** (registry.db seule vérité) | `vpn_state{n}.json` + `shared_rotation.json` portent IP/identité/breaker en parallèle de `registry.db` ; `_ip_recent` lit le JSON, pas la base | `manager.py:4237-4253`, `10887-11198` |
| **V** (une IP = une station) | Respecté au commit/adoption (`_ip_in_use_by_other`, `4388`, `4433`) mais **pas** à la sélection par le control-server (IP auto-déclarée) | `manager.py:3673-3740` |
| **VI** (indépendance des stations) | `_configure_auth_guard()` écrit la politique d'UNE station dans le garde **partagé** (dernier écrivain gagne) ; `configure_docker_autostart()` idem sur des globals module | `manager.py:8221-8233`, `1923-1926` |
| **VII** (aucune preuve, aucun succès) | `get_public_ip` retourne une IP self-reported 2/3 du temps et `_commit_ip` la valide comme « fraîche » | `manager.py:3694-3740`, `4380-4418` |
| **VIII** (un seul réparateur) | 4 réparateurs, 3 actifs ; arbitrage par config | `manager.py:9848`, `config.yaml:289,690` |
| **XII** (toute option GUI a un effet) | 6 clés lues sans consommateur (§7) | table §7 |
| **XIV** (≤ 1 500 lignes) | 11 471 lignes ; +34 % depuis le 24/10 | `wc -l vpn/manager.py` |
| **XV** (adapter de plateforme unique) | 348 lignes de logique Windows (chemins `C:\Program Files\…`, `%APPDATA%`) dans `vpn/manager.py`, pas dans `core/platform/` | `manager.py:203-442` |
| **XVI** (wrapper commun obligatoire) | `vpn/manager.py` n'importe jamais `core.process` ; 5 sites `subprocess` avec `creationflags` fabriqué à la main | `manager.py:213,264,280,5342,11211` |

---

## 4. Findings

### P0

#### F-P0-1 — L'image du fork n'est déclarée que pour 1 station sur 10 ; tout `compose up` du manager la perd

- **Fichier:ligne** : `docker-compose.yml:416` (vs `26`, `185`, `296`, `591`) ;
  `vpn/manager.py:2239, 6543, 7361, 7614, 7884, 8291, 9431, 9510, 9578`.
- **Preuve** :
  - `grep -n "image:" docker-compose.yml` → seul `vpn-gluetun-4` porte
    `image: gluetun-016:local` ; les stanzas 1/2/3/5/6/7-10 portent
    `qmcgaw/gluetun@sha256:4af0c804…d183`.
  - `cat logs/compose-control-plane-1.yml` → `image: gluetun-016:local` +
    `pull_policy: never` (overlay **non versionné** : `git ls-files docs/…` →
    `?? docs/audits/…` ; `logs/` est dans `.claudeignore`).
  - `grep -c -- "--profile" vpn/manager.py` → `0` ; `grep -n '"compose", "-f"'
    vpn/manager.py | wc -l` → 8, tous avec un unique `-f`.
  - `free/ip_registry/adapters/compose_binder.py:121` :
    `if not tag.startswith("gluetun-016:"): return True  # A legacy image
    cannot serve the control plane` — l'assertion explicite que la flotte
    **doit** tourner le fork.
  - `grep -rn "extra_compose_files=" --include=*.py` hors tests → **aucun
    appelant de production**, donc rien ne rattrape l'overlay côté manager.
- **Impact** : toute rotation/recovery qui recrée une station (`--force-recreate`)
  ou un `apply_update` la ramène sur l'image upstream. L'API v2
  (`/v1/vpn/state`, `/v1/vpn/switch`, jobs) disparaît → 404 → les rotations par
  API et `compose_binder` cassent, la station reste sur un chemin legacy non
  testé. Le commentaire de `docker-compose.yml:412-415` affirme « Les stations
  tournent le fork local » — le fichier ne le déclare que pour une.
- **Recommandation** : promouvoir `image: gluetun-016:local` + `pull_policy: never`
  dans l'ancre `x-gluetun-multi-base` (`docker-compose.yml:5-48`) et dans la
  stanza `vpn-gluetun` ; versionner les overlays `compose-control-plane-*.yml`
  sous `compose/` (suivis) et les passer depuis `manager.py` via un
  `_compose_extra_files()` unique, ou migrer leur contenu dans l'ancre.
- **Effort** : M.

#### F-P0-2 — L'auto-update compare et pull l'image upstream pendant que la flotte tourne le fork

- **Fichier:ligne** : `vpn/manager.py:9423` (`check_update`), `9572`
  (`_rollback_update`), `10871` (`_refresh_servers_list`) ; `ops/updater.py:26,47`.
- **Preuve** :
  - `manager.py:9423` : `image = "qmcgaw/gluetun"` — puis
    `_docker_repo_digest(image)` et `compose pull <service>`.
  - `manager.py:9517-9529` : `apply_update` recrée avec `--pull never` — donc
    l'image effectivement utilisée est celle que compose résout, c'est-à-dire
    l'upstream de la stanza (F-P0-1), pas le fork.
  - `ops/updater.py:47` : `image = "qmcgaw/gluetun"` dans
    `apply_fleet_update`, avec `_image_id(host, image)`.
- **Impact** : le digest surveillé n'est **pas** celui qui tourne. Un
  `check_update()` peut donc signaler « mise à jour disponible » sur un
  changement de l'upstream sans rapport, et `apply_update` recrée **toutes** les
  stations — sur l'image upstream (perte du fork), avec `_rollback_update` qui
  retague `old_id` sur `qmcgaw/gluetun`. Aggravé par `_watchdog_escalate`
  (`manager.py:10817-10824`) qui appelle `apply_update(check_opportune=True)`
  pendant une panne (où `_update_opportune()` retourne `True` par construction,
  `manager.py:9502`).
- **Recommandation** : dériver l'image depuis la définition compose réelle
  (`docker compose config --format json` → `.services[<svc>].image`) et refuser
  `check_update` quand l'image résolue n'est pas `qmcgaw/gluetun` ; aligner
  `ops/updater.py` sur la même source.
- **Effort** : S.

### P1

#### F-P1-1 — `get_public_ip()` valide une rotation sur une IP auto-déclarée 2 fois sur 3 (principe VII)

- **Fichier:ligne** : `vpn/manager.py:3694-3740` (`get_public_ip`),
  `5720-5732` (`_control_public_ip`), `4310-4418` (`_finalize_ip` → `_commit_ip`).
- **Preuve** :
  ```python
  # manager.py:3694-3710
  _m = int(getattr(self, "_ip_method_idx", 0) or 0) % 3
  self._ip_method_idx = (_m + 1) % 3
  if _m == 1:   order = ("socks", "control", "http")
  elif _m == 2: order = ("control", "http", "socks")
  else:         order = ("control", "socks", "http")   # control 1er
  ```
  et `manager.py:5720-5721` : `"""IP as reported BY gluetun (self-reported,
  not a tunnel probe)."""`. `_finalize_ip` (`4324`) appelle `get_public_ip()`
  puis `_commit_ip(new_ip)` (`4339`), qui appelle `_advance_identity()` et
  journalise l'historique.
- **Impact** : dans 2 ordres sur 3, une rotation est déclarée réussie, l'identité
  avance et l'IP entre au registre partagé **sans qu'aucune trame n'ait traversé
  le tunnel**. `_breaker_charge_success()` est appelé (`2601`) ce qui ferme la
  fenêtre de warm-up et masque un tunnel réellement mort. Contredit
  explicitement « Un bind sans egress réel n'est pas un succès » (VII) et
  « Aucune preuve, aucun succès ».
- **Recommandation** : séparer les deux contrats — `get_public_ip()` pour
  l'affichage/diagnostic, et `prove_egress_ip()` (SOCKS5 ou HTTP-proxy
  uniquement) pour toute validation de rotation/commit. Le control-server reste
  utile comme *indice* de secours, jamais comme preuve.
- **Effort** : M.

#### F-P1-2 — Le garde `httpcore[socks]` est calculé puis jamais lu dans `_probe_connect`

- **Fichier:ligne** : `vpn/manager.py:4176-4184`.
- **Preuve** :
  ```python
  try:
      import httpcore  # noqa: F401
      _has_socks = True
  except ImportError:
      _has_socks = False
      logger.debug("[vpn] httpcore[socks] non installé — probe via socks5 peut être direct")
  # ... _has_socks n'est PLUS JAMAIS lu dans la méthode
  client = self._get_probe_client()
  ```
  `awk 'NR>=4170 && NR<=4234' vpn/manager.py | grep -n "_has_socks"` → 2 lignes,
  deux affectations, aucune lecture. Le chemin jumeau `_canary_probe_once`
  (`7309-7317`) **fait** le contrôle (`if not _has_socks: return False`).
- **Impact** : `_probe_connect` est l'**autorité unique** d'`egress_dead` pour
  les deux stacks (`manager.py:4052`, `10048`). La protection documentée
  (« sinon proxy=socks5 ouvre TCP direct (faux positif) ») n'existe pas dans ce
  chemin : selon la version de `httpx`/`socksio`, la sonde peut réussir en
  direct et ne jamais déclarer le tunnel mort — ou lever, ce qui est traité
  comme `"error"` (mort, sans grâce). Asymétrie avec le canari.
- **Recommandation** : `if not _has_socks: return "error"` (ou retourner le
  verdict `"no_socks"` classé « indéterminé », comme le canari) ; factoriser le
  contrôle dans `_get_probe_client`.
- **Effort** : S.

#### F-P1-3 — Aucun compteur de sessions NordVPN ; le canari WG est une 7ᵉ session

- **Fichier:ligne** : `vpn/auth.py:36-38,79-86` ; `vpn/manager.py:7295, 7359-7400` ;
  `free/egress_adapters/nordvpn.py:36-40,123` ; `docker-compose.yml:590`.
- **Preuve** :
  - `_auth_gate` ne borne que la *simultanéité des handshakes* (2) et la
    cadence (12 s / 30 s) — jamais le **nombre de tunnels ouverts**.
  - `max_active_stations` (défaut 6, `nordvpn.py:36`) n'est appliqué que dans
    `NordVpnEgressAdapter.refresh`, sur des observations de catalogue
    (`nordvpn.py:123` : `if active > self.max_active_stations:` → écrit une
    observation, n'ouvre rien). Son propre docstring le dit : *"It never opens
    a session"*.
  - `_WG_CANARY_SERVICE = "vpn-wg-test"` (`manager.py:7295`) est monté par
    `compose up -d vpn-wg-test` (`7366`) avec `VPN_TYPE=wireguard`
    (`docker-compose.yml:613`), sur le même compte que les 6 stations.
  - `config.yaml:667` : `station_count: 6`.
- **Impact** : 6 stations + 1 canari = **7 tunnels NordVPN simultanés**. Le
  plan compte est le seul garde-fou réel, et son dépassement produit exactement
  la rafale d'`AUTH_FAILED` que tout le reste du code combat (throttle, garde
  auth, park, backoff). Aucun code ne relie `station_count` à une limite de
  sessions ni n'empêche le canari de démarrer quand la flotte est complète.
- **Recommandation** : introduire un compteur de sessions de compte (objet
  « session » du principe II) alimenté par les transitions
  `CONNECTING→CONNECTED` / `stop`, consulté par `connect`, `_ensure_container`,
  le canari et le superviseur ; refuser le canari si la flotte est à la limite.
- **Effort** : M.

#### F-P1-4 — La garde AUTH partagée est configurée par chaque station (dernier écrivain gagne)

- **Fichier:ligne** : `vpn/manager.py:8211-8233` (`_configure_auth_guard`),
  `103` (`_STATION_AUTH_GUARD` global), `1864` (appel dans `__init__`).
- **Preuve** :
  ```python
  # manager.py:8221-8231 — appelé depuis CHAQUE manager
  policy = self._repair_policy
  _STATION_AUTH_GUARD.window_s = max(5.0, float(policy.auth_global_window_s))
  _STATION_AUTH_GUARD.min_stations = max(2, int(policy.auth_global_min_stations))
  _STATION_AUTH_GUARD.pause_s = pause
  _STATION_AUTH_GUARD.max_pause_s = max(pause, min(60.0, pause * 2.0))
  ```
  Or `_repair_policy = RepairPolicy.from_config(cfg)` (`1862`) où `cfg` a subi le
  merge `per_station` (`1211-1229`) : `per_station` peut donc porter
  `station_repair.auth_global_pause_s` pour la station 3, et la station 3
  écrasera la politique globale des 6.
- **Impact** : violation du principe VI (« Chaque station est indépendante ») et
  effet non déterministe selon l'ordre de construction des managers
  (`opencode.py` lifespan construit 1..N). Le même défaut existe pour
  `configure_docker_autostart` (`manager.py:1923-1926`) qui écrit des globals
  module (`_DOCKER_AUTOSTART_ENABLED`, `_DOCKER_AUTOSTART_COOLDOWN_S`).
- **Recommandation** : configurer la garde **une fois** au boot depuis la config
  globale (`config.settings`), hors `per_station` ; documenter la garde comme
  ressource de compte, pas de station.
- **Effort** : S.

#### F-P1-5 — `_STATION_AUTH_GUARD.clear()` sans transport annule les pauses des autres transports

- **Fichier:ligne** : `vpn/manager.py:8475-8478` (dans `_auth_repair_succeeded`),
  contre `4007` (`clear(transport=…)`).
- **Preuve** :
  ```python
  # manager.py:8475-8478
  try:
      _STATION_AUTH_GUARD.clear()   # <-- aucun transport
  except Exception:
      pass
  ```
  `station_repair.py:418-423` : `clear(transport=None)` vide `global_scope`
  **et** `self.transports.clear()` — donc les pauses `openvpn-udp`,
  `openvpn-tcp` et `wireguard` accumulées par les autres stations.
  Le commentaire de `station_repair.py:307-313` pose pourtant le contrat
  inverse : *"An OpenVPN AUTH_FAILED must not freeze WireGuard repairs"*.
- **Impact** : une réparation auth réussie sur une station efface les pauses de
  toutes les autres et de tous les transports → rafale d'AUTH immédiate sur un
  compte encore rate-limité. Violation du principe VI (indépendance des
  transports, « aucun verrou inter-stations »).
- **Recommandation** : `_STATION_AUTH_GUARD.clear(transport=self._auth_transport())`.
- **Effort** : S.

#### F-P1-6 — Constitution XVI : `vpn/manager.py` n'utilise jamais le wrapper commun, et les scripts service-spawned échappent au gate

- **Fichier:ligne** : `vpn/manager.py:213, 264, 280, 5342, 11211` ;
  `scripts/fresh_chain.py:35,104,261,300,354,1229,1239` ;
  `scripts/fresh_chain_supervisor.py:86,188,550,917,1039,1055,1080,1087,1101,2019` ;
  `free/chain_repair.py:25-30` ; `scripts/platform_audit.py:34-52`.
- **Preuve** :
  - `grep -n "core.process\|run_hidden\|popen_hidden" vpn/manager.py` → **vide**.
    Le fichier fabrique `CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0`
    (`manager.py:458`) et le passe à la main sur ses 5 sites `subprocess`.
  - `scripts/platform_audit.py:34-52` : `RUNTIME_DIRS = (core, dashboard, free,
    observability, ops, rotation, server, streaming, upstream, vpn)`,
    `ROOT_MODULES = (docker_events.py, opencode.py, vpn_manager.py)` — **`scripts/`
    n'est pas scanné**.
  - Or `opencode.py:2396` lance `scripts/fresh_chain_supervisor.py` via
    `_sp.Popen(..., creationflags=0x08000000)` — ce n'est pas une CLI opérateur,
    c'est un processus du service. Il est audité par personne.
  - `free/chain_repair.py:30` : `creationflags=0x08000000` littéral au lieu
    d'importer `core.process.CREATE_NO_WINDOW`.
  - `grep -rn "run_hidden\|popen_hidden" vpn/ free/ ops/ scripts/ dashboard/
    observability/ core/ opencode.py station_repair.py` → **aucun résultat**
    (le seul import vivant est `free/public_egress_container.py:26`).
- **Impact** : aucune fenêtre ne s'ouvre aujourd'hui (tous les sites portent le
  flag), mais le contrat XVI (« un wrapper commun est la **seule** voie
  autorisée ») est violé sur 16 sites et le gate statique ne peut pas détecter
  une régression dans `scripts/` — la moitié des lancements de processus du
  service.
- **Recommandation** : router les 16 sites par
  `core.process.run_hidden`/`popen_hidden` ; ajouter `scripts/fresh_chain.py`,
  `scripts/fresh_chain_supervisor.py` et `station_supervisor.py` aux
  `RUNTIME_DIRS`/`ROOT_MODULES` de `platform_audit.py` ; les marquer
  `interactive` uniquement s'ils sont réellement opérateur-lancés (ce n'est pas
  le cas).
- **Effort** : M.

#### F-P1-7 — `vpn/manager.py` : 11 471 lignes contre 1 500 (principe XIV), +34 % depuis l'audit du 24/10

- **Fichier:ligne** : `vpn/manager.py` (fichier entier).
- **Preuve** : `wc -l vpn/manager.py` → `11471`. Le docstring de `vpn/auth.py:3-5`
  rappelle la baseline de la Phase 4 : *"Première découpe du god object (8559
  lignes)"*. Soit **+2 912 lignes en 27 jours**. La seule extraction jamais
  réalisée est `vpn/auth.py` (120 lignes). Le docstring de `vpn/manager.py:22`
  l'admet : *"La découpe fine stations/compose/watchdog/geo reste en follow-up."*
  `.specify/memory/constitution.md:132-139` : *"Aucun nouveau module ne dépasse
  1 500 lignes ; les monolithes existants sont extraits par lots."* Aucun gate de
  taille n'existe (`grep -rn "1500\|MAX_LINES\|max_lines"
  scripts/platform_audit.py tests/test_constitution_rules.py` → vide).
- **Impact** : `_watchdog_tick_impl` fait 962 lignes et 42 `return` ; 338
  `except Exception` dont 288 réduits à `pass` masquent les erreurs de contrôle ;
  298 `getattr(self, "_x", default)` rendent tout renommage d'attribut
  silencieusement toléré. Le coût de modification est le vrai risque : chaque
  correctif touche un fichier que personne ne peut relire.
- **Recommandation** : §6 (plan de découpage) ; ajouter un gate de taille dans
  `platform_audit.py` avec la liste des monolithes tolérés et un plafond
  décroissant.
- **Effort** : L.

### P2

#### F-P2-1 — Le tick watchdog (962 l.) est mort en configuration livrée, mais ressuscitable d'un clic

- **Fichier:ligne** : `vpn/manager.py:9848-9859` ; `config.yaml:289` (`repair_owner:
  supervisor`), `config.yaml:690` (`station_repair.enabled: false`) ;
  `vpn/manager.py:8732-8736`, `8802`, `8849`.
- **Preuve** : `if getattr(self, "_repair_owner", "manager") == "supervisor": …
  return` (l. 9858). Les 944 lignes suivantes ne s'exécutent jamais. Le
  `update_config` (`4787-4794`) accepte `station_repair` / `station_repair_enabled`
  en hot-reload, donc un changement de config **réactive** deux réparateurs
  concurrents sans redémarrage.
- **Impact** : ~1 570 lignes non testées en production mais atteignables ;
  deux propriétaires de réparation possibles.
- **Recommandation** : soit supprimer la branche manager (et `station_repair.py`
  + `_auth_repair_loop` + `_decide_watchdog_action`) avec les tests associés,
  soit la rendre non activable (retirer les clés du GUI). Ne pas laisser les
  deux.
- **Effort** : M (suppression) / S (verrouillage).

#### F-P2-2 — `refresh_status` est monkey-patché à l'import du module

- **Fichier:ligne** : `vpn/manager.py:11448-11471`.
- **Preuve** : `VPNManager.refresh_status = _serialized_refresh(VPNManager.refresh_status)`
  au niveau module. Le wrapper inspecte `kwargs.get("force")` puis `args[0]` et,
  si le verrou est pris et `force` vrai, **attend** (`async with lock:`) — un
  tick watchdog forcé peut donc bloquer derrière un scan dashboard.
- **Impact** : introspection cassée (`inspect.signature` sur la méthode liée),
  sous-classes et mocks ignorés, latence du watchdog non bornée par la cadence.
- **Recommandation** : déplacer la sérialisation dans `refresh_status` lui-même
  (un `_refresh_serialized: bool`), ou utiliser un décorateur appliqué dans la
  classe.
- **Effort** : S.

#### F-P2-3 — `_compose_env` réécrit `.env` à chaque appel (10 sites, dont les chemins de recovery)

- **Fichier:ligne** : `vpn/manager.py:5498-5512` ; appelé depuis `2243, 6543,
  6551, 7619, 7885, 8293, 9433, 9520, 9588`.
- **Preuve** :
  ```python
  # manager.py:5498-5512 — à la FIN de _compose_env, donc à chaque appel
  from ops.env_store import update_env
  update_env(_env_path, {f"VPN_TYPE_STATION{s}": stack for s in …})
  ```
  `ops/env_store.update_env` → `config.store.upsert_env` (`config/store.py:172`)
  qui prend le verrou cross-process `transaction()` et relit tout le fichier.
- **Impact** : chaque `compose up`/`stop`/`pull`/`restart` déclenche une
  transaction `.env` verrouillée, alors que 5 autres écrivains
  (`_apply_stack:7570`, `_sync_stack_env:7732`, `_apply_ovpn_protocol:7858`,
  `_clear_compose_pin_file:6634`, `update_env`) prennent le même verrou. Le
  no-op est supprimé par `upsert_env`, ce qui limite le coût, mais la
  contention reste sur le chemin de recovery.
- **Recommandation** : séparer « construire l'env » (pur, cacheable) de
  « synchroniser `.env` » (un seul écrivain, appelé uniquement quand la stack
  change réellement).
- **Effort** : M.

#### F-P2-4 — Deux machines à états de santé de station en parallèle

- **Fichier:ligne** : `station_repair.py:82-101` (`RepairPolicy`) +
  `vpn/manager.py:1872` (`RepairState`) vs `free/station_health.py:42-160`
  (`StationHealth` / `StationHealthRegistry`).
- **Preuve** : les deux exposent `good_probes` / `bad_probes` / un état
  `repairing|parked|probing|usable` et un compteur de parcs. `free/pool/__init__.py:827`
  construit **son** `RepairPolicy.from_config(_rel_cfg)` et
  `free/pool/__init__.py:777` son `StationHealthRegistry()` — indépendamment de
  `vpn/manager.py:1862`. Les deux lisent la même section `station_repair` de
  `config.yaml` mais avec des objets distincts.
- **Impact** : deux vérités sur l'état d'une station ; `station_contract`
  (`free/station_health.py:380-400`) doit lire 5 attributs privés du manager
  pour reconstituer ce que `RepairState` sait déjà.
- **Recommandation** : faire de `StationHealthRegistry` la seule machine à
  états, et de `RepairPolicy`/`RepairState` son unique source de politique ;
  supprimer les champs dupliqués côté manager.
- **Effort** : L.

#### F-P2-5 — `RepairState`/`RepairPolicy` : champs déclarés sans consommateur

- **Fichier:ligne** : `station_repair.py:83` (`detect_after_s`), `90`
  (`max_recreates_per_hour`) ; `vpn/manager.py:1876` (`_auth_global_pause_until`).
- **Preuve** :
  - `grep -rn -F "detect_after_s" .` (hors Lib/_research/audit-wt/gluetun) →
    `station_repair.py:83` et `:119` uniquement : lu depuis la config, jamais
    utilisé.
  - `_auth_global_pause_until` : `grep -n "_auth_global_pause_until"
    vpn/manager.py` → `1876` (initialisation) et `8567` (affectation). Jamais lu.
- **Impact** : dette + fausse impression de pilotage (`detect_after_s` est
  exposé dans `config.yaml:686`).
- **Recommandation** : supprimer ou brancher (`detect_after_s` pourrait gater la
  détection egress ; `_auth_global_pause_until` devrait alimenter
  `_auth_global_pause_remaining`).
- **Effort** : S.

#### F-P2-6 — `free/station_health.py` dépend de 5 attributs privés du manager

- **Fichier:ligne** : `free/station_health.py:386-397`.
- **Preuve** : `getattr(station, "_auth_failed")`, `_auth_park_until`,
  `_repairing`, `_parked_until`, `_egress_state`, `_last_effective_ok_at`.
  Idem `free/pool/__init__.py:108,594` (`_station`), `dashboard/routers/*`
  (`pool._station_health`), `free/qualification/repair_adapter.py:39`.
- **Impact** : tout renommage d'attribut dans `manager.py` casse silencieusement
  le contrat d'éligibilité (les `getattr` par défaut retournent une valeur
  neutre). Constitution XIV : « les dépendances passent par des ports ».
- **Recommandation** : exposer un `station_contract_snapshot()` public sur
  `VPNManager` (déjà partiellement fait par `get_status`), et faire lire
  `station_health` sur ce snapshot.
- **Effort** : M.

#### F-P2-7 — 348 lignes de logique Windows dans `vpn/manager.py` (principe XV)

- **Fichier:ligne** : `vpn/manager.py:203-442`.
- **Preuve** : `_docker_desktop_process_running` (`tasklist … Docker Desktop.exe`),
  `_disable_docker_desktop_ui_on_startup` (`os.path.expandvars(r"%APPDATA%")`),
  `_start_docker_desktop`, et `ensure_docker_running:391-396` qui liste
  `r"C:\Program Files\Docker\Docker\Docker Desktop.exe"` en dur. Les 4 lignes
  portent le marqueur `# platform-audit: allow Windows adapter debt` — dette
  reconnue, mais XV exige un `core/platform/` unique.
  `free/station_identity.py:145` porte le même marqueur ; `free/chain_repair.py:24-33`,
  `scripts/fresh_chain_supervisor.py:187-197,2018-2026` et `opencode.py:2355-2367`
  dupliquent le test `os.name == "nt"` (tasklist/taskkill).
- **Impact** : 5 copies d'un même branchement OS hors adapter ; XV violé par
  construction.
- **Recommandation** : créer `core/platform/` avec `supports_service`,
  `process_group`, `pid_alive`, `launch_desktop`, `suppress_console` ; y déplacer
  ces 348 lignes et les 5 copies.
- **Effort** : L.

#### F-P2-8 — `rotation/` : 9 fichiers sur 19 sans consommateur de production

- **Fichier:ligne** : `rotation/domain/__init__.py`, `rotation/ports/__init__.py`,
  `rotation/interfaces/__init__.py` (3 paquets vides) ;
  `rotation/application/{repair,sweep,warm_pool}.py` (221 l.) ;
  `rotation/infrastructure/config/schema.py` ;
  `rotation/infrastructure/db/{repositories,claims,legacy_ip,migrate_legacy}.py`.
- **Preuve** :
  ```
  grep -rn --include=*.py -g '!tests/**' -g '!rotation/**' \
    'from rotation|import rotation' .
  ```
  → 7 résultats, **tous** vers `rotation.infrastructure.db.{migrate,maintenance}`.
  `rotation.domain` → 0 hit repo-wide. `UnitOfWork|RegistryRepository` hors
  `rotation/`+`tests/` → 0 hit. `rotation.application.*` → seulement
  `tests/test_rotation_{repair,sweep,warm_pool}.py`.
  Le `rotation/__init__.py:1-6` se décrit lui-même : *"strangler migration …
  no runtime path is switched here yet."*
- **Impact** : ~960 lignes mortes + 3 paquets vides ; `repositories.py` duplique
  le pool chaud, le curseur de balayage et la file de réparation qui sont vivants
  dans `scripts/fresh_chain*.py` et `free/repair_queue.py` ; `claims.py`
  redéclare les 3 tables de `free/catalog_claims.py:43-62`.
- **Recommandation** : conserver `infrastructure/db/{migrate,schema.sql,maintenance,retention}`
  (LIVE), supprimer `application/*`, `config/schema.py`, `domain/`, `ports/`,
  `interfaces/`, et `repositories.py`/`claims.py`/`legacy_ip.py`/`migrate_legacy.py`
  (ou les geler explicitement).
- **Effort** : M.

#### F-P2-9 — `scripts/fresh_chain*.py` : 6 536 lignes service-spawned qui dupliquent le manager

- **Fichier:ligne** : `scripts/fresh_chain.py` (3 242 l.),
  `scripts/fresh_chain_supervisor.py` (3 294 l.) ; spawn `opencode.py:2396`,
  `scripts/fresh_chain_supervisor.py:550`.
- **Preuve** : les deux sont **LIVE** (`logs/fresh_chain_supervisor.lock` porte un
  PID vivant, `logs/fresh_chain_supervisor.log` mtime du jour). Duplication
  fonctionnelle avec `vpn/manager.py`, concepts identiques, implémentations
  indépendantes :

  | Concept | `vpn/manager.py` | `scripts/fresh_chain*.py` |
  |---|---|---|
  | Garde AUTH | `vpn/auth.py:63` (15 appels) | `fresh_chain_supervisor.py:837,857,877,892` |
  | Attente santé | `_wait_healthy:9251` | `fresh_chain.py:2895-2907`, `supervisor:1099,1122,1140` |
  | Sonde | `_probe_url:3553`, `_probe_connect:4154`, `_probe_tunnel_light:4052` | `fresh_chain.py:1819`, `free/ip_registry/adapters/probe.py` |
  | Compose | ~201 références | `free/ip_registry/adapters/compose_binder.py:334` |
  | Pin `.env` | `_apply_stack`, `_sync_stack_env` | `fresh_chain.py:1277`, `supervisor:2047` |
  | Échelle de réparation | `_watchdog_tick_impl:9842` | `supervisor:1163,2148` |
  | Liveness PID | — | `free/chain_repair.py:18`, `supervisor:173`, `opencode.py:2358` |

  Aucun import croisé : `grep -n 'fresh_chain|ops\.supervisor' vpn/manager.py` →
  vide. L'arbitrage est purement config (`config.yaml:289,690`).
  `docs/adr/ADR-010-nordvpn-control-plane.md:12` (2026-11-09) fixait la baseline
  à 2 535 / 1 934 lignes → **+28 % / +70 % en 11 jours**, alors que la décision 9
  du même ADR impose un démantèlement par tranches.
- **Impact** : trois implémentations de « est-ce que cette station est saine ? »,
  trois gates AUTH, quatre liveness PID, deux écrivains de
  `SERVER_HOSTNAMES_STATIONn` dans le même sous-système.
- **Recommandation** : trancher le propriétaire (le superviseur, conformément à
  VIII) puis extraire de `fresh_chain*` les primitives partagées
  (`probe`, `auth_gate`, `health_wait`, `env_pin`) dans un module commun
  consommé par les deux ; supprimer la copie du manager.
- **Effort** : L.

### P3

| # | Finding | Fichier:ligne | Preuve | Recommandation | Effort |
|---|---|---|---|---|---|
| F-P3-1 | `_has_socks` mort → déjà F-P1-2 | — | — | — | — |
| F-P3-2 | `wg_canary_countries` lu puis jamais utilisé | `vpn/manager.py:1420-1421` ; `config.yaml:731` | `grep -n "_WG_CANARY_COUNTRIES" vpn/manager.py` → 1 seule ligne (affectation) | supprimer la clé ou l'injecter dans `SERVER_COUNTRIES` du canari | S |
| F-P3-3 | `credentials_file` / `_auth_file` sans effet | `vpn/manager.py:1268, 4538` ; `config.yaml:126` | `grep -n "_auth_file" vpn/manager.py` → 2 hits (affectation + echo status) ; les vraies creds passent par `credentials.env` (`docker-compose.yml:32,204,316,612`) | supprimer la clé ou la brancher | S |
| F-P3-4 | `stations.reconciler.enabled` sans consommateur | `config.yaml:66-67` | `grep -rn 'yaml_get("stations"' …` → seulement `free/quality.py:77` (`quality`) et `ops/health.py:85` (`health`) | supprimer la section ou la brancher | S |
| F-P3-5 | `use_nordvpn_api` n'alimente qu'un dropdown GUI | `config.yaml:718` ; `dashboard/routers/nordvpn_routes.py:55,60` | seul consommateur : `countries_getter(bool(cfg.get("use_nordvpn_api")))` | documenter « affichage seul » (XII) ou brancher sur `_fetch_nord_loads` | S |
| F-P3-6 | `_ovpn_port_idx` écrit et exposé, jamais lu pour décider | `vpn/manager.py:1593, 7877, 8063` | aucune lecture en condition | supprimer | S |
| F-P3-7 | `_ovpn_ports` ne contient pas `tcp:8443` alors que la doc du cycle Q6 le promet | `vpn/manager.py:1592` vs `7743, 7763` | `self._ovpn_ports = ["udp:1194", "tcp:443"]` (2 entrées) ; `if cur_port == "8443": cur_idx = 2` est inatteignable | aligner la liste sur la doc (3 entrées) ou corriger la doc | S |
| F-P3-8 | `classify_repair_cause` test-only | `station_repair.py:55` | hors tests : 0 appelant | supprimer ou brancher dans `_decide_watchdog_action` | S |
| F-P3-9 | `_get_probe_client` : clients httpx jamais fermés à `stop()` | `vpn/manager.py:3523-3551` | `grep -n "_probe_clients" vpn/manager.py` → 3 hits, tous dans la méthode ; aucun `aclose()` dans `stop()` (`2177-2232`) | fermer dans `stop()` | S |
| F-P3-10 | `_valid_ip` accepte une IPv6 | `vpn/manager.py:3576-3588` | `ipaddress.ip_address` accepte v4 **et** v6 ; `_NORDVPN_AF_INET_RE:937` ne matche que v4 | restreindre à `IPv4Address` ou documenter | S |
| F-P3-11 | `ov_auth_station_threshold` lu deux fois en 6 lignes | `vpn/manager.py:1835, 1841` | `_thr_raw` sert uniquement au test `>= 999` ; la valeur effective vient de `_clamp_cfg_number` | fusionner en une lecture | S |
| F-P3-12 | `_egress_repair_threshold()` retourne toujours `_egress_dead_threshold()` en config livrée | `vpn/manager.py:8732-8736` | `_station_repair_enabled = False` (`config.yaml:690`) → la branche `return 2` est morte | supprimer la branche ou le kill-switch | S |
| F-P3-13 | `watchdog_interval` : le commentaire annonce 7, `config.yaml` livre 20 | `vpn/manager.py:1629-1632` ; `config.yaml:729` | commentaire obsolète | corriger le commentaire | S |
| F-P3-14 | `.env` porte `VPN_TYPE_STATION7` alors que `station_count: 6` | `.env:45` ; `config.yaml:667` | `docker-compose.yml` définit 7-10 mais aucun `SERVER_HOSTNAMES_STATION7..10` dans `.env` → une montée à 8 booterait sur des valeurs vides | purger les clés orphelines ou compléter 7-10 | S |
| F-P3-15 | Services compose sans référence code | `docker-compose.yml:657` (`docker-socket-proxy`), `:700` (`vpn-alerts`) | `grep -rn "opencode-vpn-alerts\|docker-socket-proxy\|socket-proxy" --include=*.py .` → seuls des hits dans `scripts/vpn_alerts.py` (CLI autonome) | retirer les profils ou les documenter | S |
| F-P3-16 | `Dockerfile` cassé par `.dockerignore` | `Dockerfile:67,69,70` vs `.dockerignore:13,22` | `COPY vpn/ ./vpn/` → `ERROR: "/vpn": not found` (reproduit) ; idem `scripts/precompress.py`, `scripts/vpn_alerts.py` | retirer `vpn/` et `scripts/*` des exclusions, ou exclure le `COPY` | S |

---

## 5. Inventaire du code mort (sourcé)

Toutes les preuves sont des greps sur le dépôt hors `Lib/`, `_research/`,
`.audit-wt/`, `.audit-logs-wt/`, `node_modules/`, `gluetun/`.

### 5.1 Méthodes de `VPNManager` sans appelant de production

Le balayage exhaustif des 198 méthodes (`awk` sur les `def` indentés +
comptage d'occurrences sur tout le corpus) ne trouve **aucune** méthode à zéro
référence : chacune est appelée au moins une fois en interne ou par un test.
Le tableau ci-dessous liste les méthodes **appelées uniquement depuis
`vpn/manager.py` lui-même** et **jamais depuis un test**, donc sans filet :

| Méthode | Définition | Références totales | Réf. externes | Réf. tests |
|---|---|---:|---:|---:|
| `_auth_global_pause_remaining` | `manager.py:8235` | 2 | 0 | 0 |
| `_prune_auth_times` | `manager.py:8592` | 2 | 0 | 0 |
| `_note_live_auth` | `manager.py:8627` | 2 | 0 | 0 |
| `_next_country_preview` | `manager.py:6501` | 2 | 0 | 0 |
| `_country_for_host` | `manager.py:4994` | 2 | 0 | 0 |
| `_ensure_geo_egress_inner` | `manager.py:6156` | 2 | 0 | 0 |
| `_control_put_countries` | `manager.py:3846` | 2 | 0 | 0 |
| `_control_pin_country_gated` | `manager.py:5782` | 2 | 0 | 0 |
| `_compose_up_impl` | `manager.py:6534` | 2 | 0 | 0 |
| `_mark_pool_repairing` | `manager.py:8151` | 2 | 0 | 0 |
| `_note_egress_restart` | `manager.py:8112` | 2 | 0 | 0 |
| `_read_live_compose_pin` | `manager.py:5371` | 2 | 0 | 0 |
| `_record_used_server` | `manager.py:7002` | 2 | 0 | 0 |
| `_load_nord_country_ids` | `manager.py:6803` | 2 | 0 | 0 |
| `_nord_country_id` | `manager.py:6793` | 2 | 0 | 0 |
| `_nord_tech_filter` | `manager.py:6814` | 2 | 0 | 0 |
| `_flip_is_auth_driven` | `manager.py:7229` | 2 | 0 | 0 |
| `_flip_is_churn_driven` | `manager.py:7234` | 2 | 0 | 0 |
| `_egress_restart_budget_ok` | `manager.py:8117` | 2 | 0 | 0 |
| `_refresh_servers_list` | `manager.py:10857` | 2 | 0 | 0 |
| `_resolve_gluetun_volume` | `manager.py:10843` | 2 | 0 | 0 |
| `_current_remote_ip` | `manager.py:6386` | 3 | 0 | 0 |
| `_fetch_nord_ip_map` | `manager.py:6897` | 3 | 0 | 0 |
| `_countries_list_for_pool` | `manager.py:6091` | 4 | 0 | 0 |
| `_cascade_elapsed` | `manager.py:7944` | 9 | 0 | 0 |
| `_local_next_country` | `manager.py:6044` | 3 | 0 | 0 |

Commandes de preuve (exemple) :
```
grep -rn --include=*.py -w -F "_prune_auth_times" .
# → vpn/manager.py:8592 (def)  +  vpn/manager.py:8647 (appel)
grep -rn --include=*.py -w -F "_cascade_elapsed" . | grep -v "^./vpn/manager.py"
# → (vide)
```

### 5.2 Code mort par configuration livrée (le plus volumineux)

| Zone | Lignes | Condition d'activation |
|---|---:|---|
| `_watchdog_tick_impl` après la ligne 9859 | ~944 | `repair_owner: supervisor` (`config.yaml:289`) |
| `_auth_repair_loop` + `_park_auth` + `_stop_service` + `_pin_last_good` + `_auth_probe_after_repair` + `_auth_repair_succeeded` + `_launch_auth_repair` | ~479 | `station_repair.enabled: false` (`config.yaml:690`) |
| `_decide_watchdog_action` bloc egress (8802-8831) | ~30 | idem |
| `_egress_repair_threshold` branche `return 2` (8734-8735) | 2 | idem |
| `station_repair.RepairPolicy` champs `detect_after_s`, `max_recreates_per_hour` | — | jamais lus |
| `_cascade_*` (7934-8010) + `_ov_auth_cascade_enabled` | ~177 | `cascade_enabled: true` mais `ov_auth_cascade_enabled: false` (`config.yaml:114,241`) — la cascade intra-OV est morte, la cascade WG→OV vit |
| `_WG_CANARY_*` (7293-7453) | ~160 | `wg_canary_enabled: false` (`config.yaml:732`) → `_cancel_wg_flip_if_canary_dead:7431` retourne avant `_wg_canary_alive` |
| `apply_mixed_slot` effet réel | ~20 | `auto_mixed_stacks: false` (`config.yaml:96`) → `_mixed_active()` False → `apply_mixed_slot:7047` retourne le slot sans rien appliquer |

### 5.3 Fichiers / modules morts ou quasi morts

| Chemin | LOC | Verdict | Preuve |
|---|---:|---|---|
| `rotation/domain/__init__.py`, `rotation/ports/__init__.py`, `rotation/interfaces/__init__.py` | ~6 | **DEAD** | `grep -rn "rotation.domain" .` → 0 hit |
| `rotation/application/{repair,sweep,warm_pool}.py` | 221 | **TEST-ONLY** | seuls `tests/test_rotation_{repair,sweep,warm_pool}.py` |
| `rotation/infrastructure/config/schema.py` | ~90 | **TEST-ONLY** | `grep -rn "rotation.infrastructure.config" .` hors tests → 0 |
| `rotation/infrastructure/db/repositories.py` | ~250 | **TEST-ONLY** | `grep -rn "UnitOfWork\|RegistryRepository" .` hors `rotation/`+`tests/` → 0 |
| `rotation/infrastructure/db/claims.py` | ~90 | **TEST-ONLY** | `maintenance.py:15` ne le passe que si `claims_source` ; seul `tests/test_claims_migration.py:26` le fait |
| `rotation/infrastructure/db/legacy_ip.py`, `migrate_legacy.py` | ~320 | **TEST-ONLY** | même garde ; docstring de `migrate_legacy` : *"not wired into the runtime yet"* |
| `rotation/infrastructure/db/{migrate,schema.sql,maintenance,retention}` | ~400 | **LIVE** | `free/ip_registry/registry_db.py:63`, `free/quota_ledger.py:616`, `observability/incidents.py:24`, `scripts/fresh_chain.py:434`, `scripts/fresh_chain_supervisor.py:2243,3245` |
| `logs/ip_registry.db` | 0 octet | **DEAD** (artefact) | `ls -la` → 0 byte, mtime 2026-11-09 |
| `logs/repair_queue.db` | 20 Ko | **STALE** | `free/repair_queue.py:17` pointe déjà `logs/registry.db` ; mtime 2026-11-10 |
| `logs/shared_rotation.json.migrated` | 2,4 Ko | **DEAD** | `free/rotation.py:449` : *"Persist into registry.db; JSON is only a SQLite-down fallback"* |
| `vpn_configs/custom/` | 0 fichier | **VIDE** | `ls -la vpn_configs/custom/` → `total 8` (2 entrées `.`/`..`) ; `config.yaml:127` `custom_ovpn_file: ''` |
| `vpn_configs/credentials.txt{,.bak-nord-*}` (11 fichiers) | 52 o × 11 | **LEGACY** | `grep -rn "credentials.txt" --include=*.py` hors scripts → seulement `vpn/manager.py:1268` (`_auth_file`, mort) ; la migration est faite (`scripts/make_credentials_env.py:4`) |

### 5.4 Exports de `station_repair.py` sans consommateur

| Nom | Réf. hors tests | Verdict |
|---|---:|---|
| `classify_repair_cause` | 0 | TEST-ONLY |
| `RepairPolicy.detect_after_s` | 0 (lu depuis config, jamais utilisé) | DEAD |
| `AUTH_GLOBAL`, `CREDENTIALS`, `SERVER`, `DOCKER`, `CAPACITY`, `UNKNOWN` | 0 | DEAD (constantes de `classify_repair_cause`) |
| `RepairState.snapshot` | 1 (`manager.py:5104`) | LIVE (mais jamais vérifié par un test) |
| `GlobalAuthGuard` | 1 (`manager.py:103`) | LIVE |

### 5.5 Duplications à résorber

| Paire | Preuve |
|---|---|
| `_refresh_server_list` (`manager.py:6690`, wipe du cache) vs `_refresh_servers_list` (`manager.py:10857`, `gluetun update -providers`) | deux noms à une lettre d'écart, sémantiques opposées, les deux appelées |
| 5 écrivains `.env` dans `manager.py` (`6634`, `7570`, `7732`, `7858`, `5504`) | chacun refait un RMW + `_ENV_RW_LOCK` + `store.transaction` |
| 3 gates AUTH (`vpn/auth.py:63`, `fresh_chain_supervisor.py:837`, `_note_live_auth:8627`) | 3 fenêtres, 3 backoffs, 3 seuils |
| 4 liveness PID (`free/chain_repair.py:18`, `fresh_chain_supervisor.py:173`, `opencode.py:2358`, `manager.py:203`) | 4 copies de `tasklist`/`os.kill` |
| 3 implémentations de pool chaud (`rotation/…/repositories.py:309`, `fresh_chain.py:551`, `fresh_chain_supervisor.py:1503`) | même table `warm_pool`, deux écrivains LIVE |
| 2 schémas de claims (`rotation/…/claims.py:14-37` vs `free/catalog_claims.py:43-62`) | mêmes 3 tables, un test-only |
| 2 `RotationPlanner` (`free/rotation_planner.py` vs `free/qualification/rotation.py`) | classes homonymes, vocabulaires d'action qui se recouvrent |
| 2 machines de santé (`RepairState` vs `StationHealthRegistry`) | cf. F-P2-4 |

---

## 6. Plan de découpage de `vpn/manager.py`

Cible : 11 471 → ≤ 1 500 lignes (principe XIV), en conservant la façade
`VPNManager` (le shim `vpn_manager.py` et les 66 fichiers de tests qui
l'importent ne doivent pas changer).

### 6.1 Modules à extraire

| Module cible | Contenu à extraire | Lignes actuelles | LOC estimées | Dépendances | Effort |
|---|---|---:|---:|---|---|
| `vpn/docker_desktop.py` | 119-466 : `configure_docker_autostart`, `docker_daemon_status`, `is_daemon_unreachable_error`, `_note_daemon_reachable`, `_docker_desktop_process_running`, `_disable_docker_desktop_ui_on_startup`, `_start_docker_desktop`, `_docker_autostart_worker`, `request_docker_autostart`, `ensure_docker_running` + les globals `_DOCKER_*` | 348 | ~330 | `core/platform` | S |
| `vpn/identity_pool.py` | 467-686 : `_KNOWN_IMPERSONATIONS`, `_DEFAULT_IDENTITY_PROFILE`, `OFFICIAL_*`, `_OFFICIAL_UA_BY_CLIENT`, `_BROWSER_HEADER_NAMES`, `_official_ua_for_client`, `_looks_like_browser_ua`, `_sanitize_extra_headers`, `_normalize_identity_profiles`, `_headers_key`, `_build_identity_pool` | 220 | ~220 | — | S |
| `vpn/state_machine.py` | 687-846 : `VPNState`, `CircuitBreaker`, `BackoffTimer`, `RotationFailed`, `AuthCoolingDownError` | 160 | ~160 | — | S |
| `vpn/parsing.py` | 870-1173 : `_sh_quote`, `_normalize_country`, `_COUNTRY_ALIASES`, `_NORDVPN_HOST_RE`, `_WIREGUARD_PEER_RE`, `_NORDVPN_AF_INET_RE`, `_ISSUE_TS_RE`, `_extract_current_ip`, `_extract_current_hostname`, `_parse_server_issue_detail`, `_host_ttl_seconds`, `_classify_probe_exc`, `_classify_error_kind`, `_is_mutating_docker_op`, `_clamp_cfg_number` | ~300 | ~300 | — | M |
| `vpn/docker.py` | 5302-5619 : `_docker_run` (funnel), `_docker_cli`, `_default_docker_runner`, `_docker_inspect`, `_read_live_compose_pin`, `_compose_env`, `_await_rotation_ops_drained`, `_docker_op_begin/end`, `_docker_restart`, `_env_value_from_inspect`, `_stack_from_env_file`, `reconcile_orphan_containers` (11199-11442) | 318 + 273 | ~560 | `vpn/env_sync` | L |
| `vpn/env_sync.py` | Les 5 écrivains `.env` fusionnés en une API (`sync_stack`, `sync_protocol`, `clear_pin`) + `_ENV_RW_LOCK` | ~250 | ~120 | `config.store` | M |
| `vpn/nordvpn_api.py` | 6764-7014 : `_http_get_json`, `_nord_country_id`, `_load_nord_country_ids`, `_nord_tech_filter`, `_fetch_nord_loads`, `_fetch_nord_ip_map`, `_least_loaded_hostnames`, `_server_recent`, `_record_used_server` | 250 | ~250 | — | S |
| `vpn/control.py` | 5620-6763 : tout le client control-server (`_control_exec`, `_control_status`, `_control_public_ip`, `_control_pin_country(_gated)`, `_control_put_countries`, `_release_pin_auth`, pays/périmètre, `ensure_geo_egress`, `pin_country`, `unpin_country`, `_fast_recover_via_control`, `_current_hostname`, `_resolve_current_host`, `_current_remote_ip`, `_compose_up(_impl)`, `_ensure_container`, `_compose_pin_key`, `_clear_compose_pin_file`, `_escape_dead_compose_pin`, `_refresh_server_list`) | ~1 144 | ~1 100 | `vpn/docker`, `vpn/nordvpn_api` | L |
| `vpn/probe.py` | 3523-4233 : `_get_probe_client`, `_probe_url`, `_valid_ip`, `_socks_public_ip`, `_http_public_ip`, `get_public_ip`, `_socks_egress_ok`, `_needs_egress_check`, `_probe_tunnel_light`, `_http_proxy_egress_ok`, `_probe_connect`, `_note_effective_ok`, `_secondary_path_active`, `_feed_pin_windows` | ~710 | ~700 | `vpn/state_machine` | L |
| `vpn/stack.py` | 7015-7933 : `_MIXED_SLOTS`, `_mixed_active/slot`, `apply_mixed_slot`, `_auto_flip_decision`, `_flip_is_*`, `_emergency_flip_decision`, `_wg_key_present`, canari WG, `_apply_stack(_impl)`, `_sync_stack_env`, `_apply_ovpn_protocol`, `_cascade_*`, `set_stack`, `stack_info` | ~920 | ~900 | `vpn/docker`, `vpn/env_sync` | L |
| `vpn/repair.py` | 8111-8714 : `_park_egress`, `_note_pool_*`, `_record_last_good`, `_auth_transport`, `_configure_auth_guard`, `_park_auth`, `_stop_service`, `_pin_last_good`, `_launch_auth_repair`, `_auth_repair_loop`, `_auth_probe_after_repair`, `_auth_repair_succeeded`, `_check_auth_failed`, `_prune_auth_times`, `_auth_cooling`, `_note_live_auth`, `_record_auth_failure`, `_host_blacklisted`, `_breaker_charge_*`, `_check_server_issue`, `_check_restart_churn`, `_decide_watchdog_action`, `_egress_*_threshold`, `_watchdog_*_recent`, `_watchdog_budget_exhausted` | ~1 270 | ~1 200 | `station_repair`, `vpn/auth` | L |
| `vpn/watchdog.py` | 9663-10886 : `arm_egress_watchdog`, `_watchdog_mark_recovered`, `_watchdog_recover_fresh_ip`, `_watchdog_loop`, `_detect_external_container_change`, `_watchdog_tick(_impl)`, `_watchdog_escalate`, `_resolve_gluetun_volume`, `_refresh_servers_list`, `_wait_healthy`, `_tunnel_up_via_control`, `_launch_socks_catchup` | ~1 220 | ~1 100 | tout le reste | L |
| `vpn/rotation.py` | 2092-2748 + 4235-4465 : `start`, `stop`, `connect`, `connect_next`, `_connect_next_impl`, `_finalize_ip`, `_commit_ip`, `_adopt_boot_ip`, `_ip_recent`, `_ip_in_use_by_other`, `_advance_identity`, `disconnect`, `_set_status`, `_publish_vpn_event`, `_on_container_event` | ~890 | ~850 | `vpn/control`, `vpn/probe` | L |
| `vpn/status.py` | 2857-3522 + 4466-5301 : `refresh_status`, `health_check`, `get_config`, `update_config`, `get_status`, `_serialized_refresh`, `update_health_policy` | ~1 500 | ~1 450 | tout | L |
| `vpn/updater.py` | 9377-9662 : `note_free_*`, `_docker_image_id`, `_docker_repo_digest`, `check_update`, `_acquire/_release_update_lock`, `apply_update`, `_rollback_update`, `_update_opportune`, `_updater_loop` | 286 | ~280 | `vpn/docker` | M |
| `vpn/persist.py` | 10887-11198 : `_get_state_path`, `_save_state_sync/async/debounced`, `save_state`, `load_state` | 312 | ~300 | — | M |

**Reste dans `vpn/manager.py`** : la classe `VPNManager` réduite à ses
propriétés publiques, `__init__` (784 l. — à dégrapper en `_init_*` par section),
et la composition des modules ci-dessus. Cible réaliste : ~1 200-1 500 lignes.

### 6.2 Ordre d'exécution recommandé

1. `vpn/parsing.py` + `vpn/state_machine.py` + `vpn/identity_pool.py`
   (purs, zéro état, déplacement mécanique — les tests existants passent tels
   quels). **S**
2. `vpn/docker_desktop.py` (isolé, 4 sites d'appel). **S**
3. `vpn/nordvpn_api.py` + `vpn/control.py` (frontière nette : tout passe par
   `_docker_run`). **L**
4. `vpn/env_sync.py` puis `vpn/docker.py` (fusion des 5 écrivains — c'est le
   pré-requis pour supprimer la contention `.env`). **M**
5. `vpn/probe.py` (après avoir corrigé F-P1-1 et F-P1-2 : on ne déplace pas un
   contrat faux). **L**
6. `vpn/stack.py` + `vpn/rotation.py` + `vpn/status.py`. **L**
7. `vpn/repair.py` + `vpn/watchdog.py` — **en dernier**, et seulement après
   avoir tranché F-P2-1 (garder ou supprimer la branche manager). **L**
8. `vpn/updater.py` + `vpn/persist.py`. **M**

### 6.3 Garde-fous du découpage

- Le shim `vpn_manager.py` doit continuer à ré-exporter **les mêmes objets** :
  les 66 fichiers de `tests/` patchent via ce namespace
  (`scripts/audit_shim_surface.py` existe déjà pour vérifier la surface).
- `vpn/__init__.py` est déjà une façade paresseuse (PEP 562) — ne pas y ajouter
  d'import eager (le cycle `config/settings.py → vpn_manager → vpn → free` est
  documenté l. 10-16).
- Ajouter le gate de taille dans `scripts/platform_audit.py` avec la liste des
  monolithes tolérés et un plafond qui décroît à chaque lot.

---

## 7. Options / config sans consommateur

### 7.1 `config.yaml` — clés lues sans effet

| Clé | Valeur livrée | Consommateur | Verdict |
|---|---|---|---|
| `ip_rotation.wg_canary_countries` | `Switzerland,Germany,Netherlands` | `manager.py:1420-1421` → `_WG_CANARY_COUNTRIES`, attribut jamais relu (1 seule référence) | **DEAD** |
| `ip_rotation.credentials_file` | `vpn_configs/credentials.txt` | `manager.py:1268` → `_auth_file` ; relu seulement en `4538` (echo `get_status`). Les vraies creds viennent de `credentials.env` | **DEAD** |
| `stations.reconciler.enabled` | `true` | aucun (`grep -rn 'yaml_get("stations"'` → `quality` et `health` uniquement) | **DEAD** |
| `ip_rotation.use_nordvpn_api` | `false` | `dashboard/routers/nordvpn_routes.py:55,60` → dropdown pays | **GUI-only** (XII : à marquer « affichage seul ») |
| `ip_rotation.station_repair.detect_after_s` | `20` | `station_repair.py:119` (lecture) puis jamais | **DEAD** |
| `ip_rotation.station_repair.max_recreates_per_hour` | `1` | 2 réfs, aucune décision | **DEAD** |
| `ip_rotation.ov_auth_cascade_cooldown_min` | — | `manager.py:1479` ; la cascade intra-OV est désactivée (`ov_auth_cascade_enabled: false`) | **DEAD en config livrée** |
| `ip_rotation.ovpn_endpoint_port` | `''` | `manager.py:1379` : `cfg.get("ovpn_endpoint_port", cfg.get("endpoint_port", …))` — `''` étant **présent**, le fallback `endpoint_port` est inatteignable et `_port_cfg` devient `''` → `_port_int` = défaut | **piège** (la clé existe pour neutraliser le fallback) |
| `ip_rotation.status_cache_seconds` | `5.0` | `manager.py:1763` — mais `_watchdog_tick_impl:10041` utilise `max(self._STATUS_CACHE_SECONDS, 30.0)` : la valeur < 30 est sans effet dans le tick | **partiellement inerte** |
| `ip_rotation.auto_mixed_stacks` | `false` | `manager.py:1599,7023` — `apply_mixed_slot` est appelé (`opencode.py:2691,3478`) mais retourne sans rien appliquer quand `_mixed_active()` est faux | **inerte** |
| `ip_rotation.wg_canary_*` (4 clés) | `wg_canary_enabled: false` | `manager.py:7431` sort avant `_wg_canary_alive` | **DEAD en config livrée** |

### 7.2 Divergences code ↔ `config.yaml` (défauts qui ne s'appliquent jamais)

Le fichier livré écrase presque tous les défauts du code. Là où les deux
diffèrent, **un seul est vivant** et la valeur morte reste dans la lecture
`cfg.get(...)`, ce qui rend toute modification du code par défaut inopérante.

| Clé | Défaut code | `config.yaml` | Ligne du défaut |
|---|---|---|---|
| `enabled` | `False` | `true` | `1241` |
| `quota_per_ip` | `300` | `200` | `1243` |
| `switch_delay` | `5` | `1` | `1244,1775` |
| `server_countries` | `"Germany"` | `''` | `1269` |
| `country_rotation` | `False` | `true` | `1302` |
| `country_offset` / `_stride` | `0` / `0` | `14` / `17` | `1303,1305` |
| `egress_allow_any_country` | `False` | `true` | `1308` |
| `ovpn_protocol` | `"udp"` | `tcp` | `1373` |
| `auto_wg_egress_ticks` | `3` | `20` | `1405` |
| `wg_canary_enabled` | `True` | `false` | `1422` |
| `egress_failure_tick_interval` | `2.0` | `15.0` | `1446` |
| `ip_probe_budget` | `8.0` | `20.0` | `1447` |
| `cascade_enabled` | `False` | `true` | `1453` |
| `auto_ov_fail_threshold` | `3` | `1` | `1579` |
| `auto_mixed_stacks` | `True` | `false` | `1599` |
| `identity_diversity` | `False` | `true` | `1608` |
| `watchdog_interval` | `60` | `20` | `1632` |
| `wait_healthy_poll` | `2.0` | `0.5` | `1635` |
| `rotation_fail_cooldown` | `300` | `60` | `1754` |
| `status_cache_seconds` | `10.0` | `5.0` | `1763` |
| `circuit_breaker_threshold` | `3` | `1000` | `1771` |
| `circuit_breaker_recovery` | `300` | `5` | `1772` |
| `watchdog_backoff_base` / `_max` | `30.0` / `300.0` | `5` / `20` | `1783,1784` |
| `server_pick_mode` | `"proxy"` | `gluetun` | `1503` |
| `repair_owner` | `"manager"` | `supervisor` | `1870` |
| `station_max_rotations_per_hour` | `10.0` | `3` | `1802` |
| `rotation_storm_cooldown_s` | `600.0` | `30` | `1803` |
| `ov_auth_backoff_s` | `90.0` | `30` | `1843` |
| `bad_ttl` (unité) | `1440.0` **minutes** | `10` | `1061` |
| `docker_autostart` | `sys.platform == "win32"` | `true` | `1924` |

**Triple source de vérité sur `circuit_breaker_threshold`** : `3` (défaut code
`1771`), `1000` (`config.yaml:117`), `3` (recalculé dans
`dashboard/api.py:1231-1232`). Trois valeurs en circulation pour une clé.

**Conflit d'unité sur `bad_ttl`** : `manager.py:1054-1064` le traite en
**minutes** (bornes 1–4320, `ttl_min * 60.0`) tandis que
`free/pool/__init__.py:7323-7328` le clampe en **secondes**
(`max(1, min(3600, _bt))`). `config.yaml:103` livre `10` : TTL effectif 10 min
ou 10 s selon le chemin.

### 7.3 Variables d'environnement sans effet

| Variable | Lieu | Verdict |
|---|---|---|
| `CLIENT_AUTH_TOKEN` | `.env:44` | **OBSOLÈTE** — 0 référence dans tout le dépôt (`.py`, `.md`, `.yml`, `.yaml`, `.json`). Le chemin vivant est `trust.py:319,322` → `config.yaml:47-49` (`client_auth.mode/key`) |
| `VPN_TYPE_STATION7` | `.env:45` | **OBSOLÈTE** — `station_count: 6` (`config.yaml:667`) |
| `UPDATER_PERIOD` | `docker-compose.yml:621` | **DEAD** — 0 consommateur Python ; le fork force `UPDATER_PERIOD=0` (`gluetun/Dockerfile:267`) |
| `GLUETUN_CONTROL_API_KEY` | `config.yaml:1231` (`api_key_env`) | **nom divergent** — la clé réellement lue est `VPN_CONTROL_API_KEY` (`manager.py:5640`, `config/settings.py:125`, `credentials.env`). `scripts/fresh_chain.py:2911-2914` contourne avec un fallback |
| `SERVER_COUNTRIES_STATION7..10` / `SERVER_HOSTNAMES_STATION7..10` / `OPENVPN_PROTOCOL_STATION7..10` | absents de `.env` | `docker-compose.yml` les interpole pour 10 stations ; une montée à 8+ booterait sur les défauts compose (`openvpn`, udp, vide) |

### 7.4 Options lues puis jetées

| Option | Lue à | Jetée à | Détail |
|---|---|---|---|
| `ov_auth_station_threshold` | `manager.py:1835` | `manager.py:1841` | `_thr_raw` sert uniquement au test `>= 999` ; la valeur effective est relue par `_clamp_cfg_number` |
| `ovpn_endpoint_port` | `manager.py:1379` | — | `''` étant présent, le fallback `endpoint_port` est inatteignable |
| `upstream.proxy` / `OPENCODE_PROXY` | `config/settings.py:507` | `config/discovery.py:331,398,455` | `config.yaml:12` et `.env:1` livrent une chaîne vide → les 3 sites reçoivent `''` |
| `ip_registry.allocator.transports` | `free/ip_registry/config.py:104` | — | lu comme `list` brut, jamais validé ; `scripts/fresh_chain*.py` relit le YAML directement |
| `observability.slo_targets*` | `opencode.py:25225-25226` | `opencode.py:25227-25229` | dans un `try/except: pass` — une valeur malformée supprime silencieusement le verdict SLO |

---

## 8. Zones non auditées / incertitudes

| Zone | Raison | Ce qu'il faudrait |
|---|---|---|
| `gluetun/` (fork Go, 95 fichiers `internal/control/`) | Hors périmètre langage ; seul le README/AGENTS/Dockerfile et la structure ont été lus | Audit Go dédié : contrat des routes v2 (`/vpn/switch`, `/vpn/rotate`, `/egress/verify`), compat `settings_compat.go`, sûreté de `CONTROL_STATE_MODE=env` |
| `free/ip_registry/adapters/compose_binder.py` (26 Ko) | Effleuré (l. 121, 264-357) pour prouver F-P0-1 | Audit complet : idempotence du rebind, gestion de `rebind_budget`, concurrence entre deux stations qui rebindent |
| `free/qualification/*` | Non lu (un `WatchdogService` + `SupervisorRepairAdapter` détectés = 4ᵉ réparateur) | Compter les déclencheurs de réparation réellement armés en config livrée |
| `ops/supervisor.py` (387 l.) | Lu partiellement (l. 68-138, 273-285) | Vérifier la sérialisation `restart()` et le breaker local vs `CircuitBreaker` du manager |
| `scripts/fresh_chain*.py` (6 536 l.) | Cartographié fonctionnellement, pas lu ligne à ligne | Audit dédié : c'est le réparateur réellement actif (VIII) |
| Valeurs des secrets | Non imprimées par principe | `git log --all --diff-filter=A -- vpn_configs/` retourne le commit `3e360dd` — à vérifier (contenu jamais committé ou retiré après coup ?) |
| Cap de sessions NordVPN du compte | Aucune source dans le dépôt | Confirmer auprès du compte (6 ou 10) avant de dimensionner F-P1-3 |
| `logs/registry.db` (167 Mo) | Interdit par `.claudeignore` | Compter les lignes de `ip_state` / `chain_state` / `leases` pour mesurer la dérive vs `vpn_state*.json` |
| `free/identities.py` (32 Ko), `free/station_identity.py` (28 Ko) | Effleurés (docstrings, signatures) | Audit dédié : le vault par IP, `rotate_installation`, la conformité au principe III (registry.db) |

### Incertitudes déclarées

1. **F-P1-2** : la conséquence exacte de `_has_socks` mort dépend de la version
   d'`httpx`/`socksio`. Avec `httpx` récent, `AsyncClient(proxy="socks5://…")`
   lève sans `socksio`, donc le « faux positif » documenté est peut-être
   inatteignable — mais le contrôle est bel et bien absent, contrairement au
   chemin canari. À confirmer par un test d'intégration.
2. **F-P0-1** : la gravité dépend de si `compose up` est réellement appelé en
   production. En config livrée (`repair_owner: supervisor`), le tick manager ne
   recrée rien ; mais `_escape_dead_compose_pin` (appelé par le tick, donc mort),
   `apply_update` (vivant via `_updater_loop`, station 1) et les endpoints
   dashboard `restart`/`apply_update` restent des chemins vivants. Confirmé
   destructeur pour `apply_update`.
3. **F-P2-1** — *résolu après vérification* : les 944 lignes mortes **sont**
   couvertes par les tests. `grep -rln "_watchdog_tick" tests/` → 27 fichiers ;
   `_auth_repair_loop` / `_launch_auth_repair` / `_park_auth` /
   `_auth_repair_succeeded` → `tests/test_station_repair.py` ;
   `_decide_watchdog_action` → `tests/test_station_repair.py` +
   `tests/test_audit_routabilite_pc.py` ; `repair_owner` →
   `tests/test_plan_011_baseline.py`, `tests/test_identity_rotation.py`,
   `tests/test_pool_characterization.py`. Conséquence pour le plan : la
   suppression (option recommandée) fait tomber une couverture réelle et doit
   donc se faire **en un lot dédié**, en supprimant aussi les tests associés —
   ce n'est pas un simple `rm`. Cela confirme au passage que la branche est un
   chemin de première classe maintenu activement, donc que le risque de
   résurrection accidentelle (F-P2-1, via `update_config`) est réel.
