# Audit du watchdog des stations — 30/10/2026

**Périmètre** : `VPNManager._watchdog_loop` / `_watchdog_tick`, redémarrages et
budget, backoff, AUTH cooling, egress watchdog, half-open, `StationSupervisor`,
`HealthPolicy`, réveil Docker, hot-reload `station_count` et observabilité.

**Méthode** : lecture du code (`vpn/manager.py`, `ops/*.py`, `opencode.py`,
`dashboard/api.py`, `free/pool.py`), inventaire des tests, et relevé live sur
l'instance `http://localhost:4000` (6 stations, PID 40288).

> **Verdict initial** : le watchdog fonctionne et a réellement réparé des
> stations (AUTH/TLS, churn healthcheck, passage WireGuard). Mais son défaut
> principal était un **angle mort egress** : une station dont la sonde
> data-plane échoue pouvait être remise `CONNECTED` pendant 40 ticks
> (≈ 10 min à 15 s/tick) sans mise à jour de `_egress_state`. Le second
> défaut était le **budget de redémarrages**, à la fois doublement compté et
> contourné. Les rotations et le hot-reload laissaient aussi des fenêtres où
> le watchdog ne voyait plus la station, sans alerte.

### Correctifs appliqués (30/10/2026)

- Seuil egress = `watchdog_egress_grace_ticks` (6), plus de fenêtre
  `auto_wg_egress_ticks * 2` ; `egress_dead` va directement en recovery.
- `_egress_state=tunnel_down` au premier échec de sonde, `tunnel_up_full`
  après succès ; la recovery ferme aussi les breakers VPN/superviseur.
- Budget : un seul crédit par tick watchdog, restart « plain » et escalade
  comptés, `_docker_restart`/`_ensure_container` couverts.
- `stop()` attend/cancel la rotation et les tâches (watchdog, save, catch-up) ;
  `stop_station()` en bénéficie ; rollback d'upscale nettoie les orphelins.
- Breaker superviseur : bail single-probe, remise à zéro sur IP fraîche,
  dernier recours pool, exposition `/api/vpn-status`.
- `boot_error` basé sur les stations `enabled` et tolérant une rotation ;
  heartbeat/skips dans `/health` et `/metrics` ; watcher Docker publié avant
  `start()`.
- Hot-reload : `station_count` via config watch, `disabled_stations`,
  overrides watchdog per-station, backoff conservé, `HealthPolicy`.
- Le watchdog n'est plus créé par le setter avant `start()` : la boucle
  démarre après le registry/pool/reconcile, et un toggle post-boot reste
  couvert.
- `_watchdog_escalate` utilise l'horloge injectable `_now_fn()`.
- Tests : `tests/test_watchdog_audit_2026_10_30.py` (7 régressions) ; suite
  complète **exit 0** et gate contrats **513/513** après correctifs.

### Validation live après correctifs (30/10 20:34 UTC)

| Contrôle | Résultat |
|---|---|
| Processus | PID **2932** |
| `/health` | `ok`, `status_reasons=[]` après expiration de la pause de clé ; 6 lignes `watchdog` exposées |
| Flotte | **6/6 `connected`**, 6 conteneurs `healthy` |
| `egress_threshold` | **6** pour les 6 stations (grâce watchdog, plus 20) |
| Heartbeat | âge exposé (~80 s au relevé), `skipped_ticks=0` |
| `/api/vpn-status` | `boot_error=None`, `stale=False`, 6 superviseurs exposés |
| Smoke free | `/v1/chat/completions` **200**, `/v1/messages` **200** |
| Gemini | 502 explicite (clé payante expirée, pas un défaut watchdog) |

La suite du document conserve l'analyse initiale et les références ; les
sections « Défauts classés » décrivent l'état **avant** correctifs.

---

## 1. Architecture réelle

Il faut distinguer deux objets :

| Objet | Rôle | Boucle |
|---|---|---|
| `VPNManager._watchdog_loop` | Vrai watchdog runtime : sonde, décide, redémarre, récupère | **une tâche asyncio par station** (`vpn/manager.py:8435-8485`) |
| `StationSupervisor` | État passif par station : breaker local, trackers de latence, `react_to_decision()` | **aucune boucle** (`ops/supervisor.py:66-167`) |

Le superviseur ne redémarre pas : il est consulté par le pool via son breaker
(`free/pool.py:982-994`) et appelé sur le chemin free non-streaming
(`opencode.py:13878-13882`). Le watchdog, lui, appelle directement
`_docker_restart()` / `_ensure_container()` et ne passe pas par le superviseur.

### Cycle d'un tick

1. Gates : `enabled`, `proxy_mode == vpn`, `_desired_connected`
   (`vpn/manager.py:8517-8522`).
2. Heartbeat toutes les 300 s (`:8530-8546`).
3. Skip si rotation en cours (`:8549-8564`) ou si `_lock` est tenu
   (`:8565-8570`).
4. Sous `_lock` : refresh statut, sonde egress, compteur
   (`:8572-8641`).
5. Décision unique `restart | auth-cooling | grace | grace-first | recheck |
   budget-exhausted` (`:7587-7636`).
6. Recovery : fast-pin control, pin pays, restart léger, escalade compose,
   IP fraîche, flip stack (`:8898-9092`).
7. Hors lock : canari WG, application du flip, escalade 30 min
   (`:9093-9231`).

### Sources de réveil

- Docker events → `_on_container_event()` → `_watchdog_event.set()`
  (`vpn/manager.py:2597-2621`, `docker_events.py:147-201`).
- Échec réel d'une requête free → pool `notify_connection_failure()` →
  `arm_egress_watchdog()` (`free/pool.py:2385-2460`,
  `vpn/manager.py:8395-8407`).
- 401 control API, EOF SOCKS + HTTP mort, churn healthcheck, rotation morte.
- Le tick est aussi planifié par intervalle / backoff / cadence armée
  (`vpn/manager.py:8460-8485`).

### Configuration actuelle

| Clé | Valeur |
|---|---:|
| `watchdog_interval` | 20 s |
| `watchdog_backoff_base` / `max` | 15 s → 60 s |
| `watchdog_max_restarts_per_hour` | 3 |
| `watchdog_auth_grace_s` | 240 s |
| `watchdog_egress_grace_ticks` | 6 |
| `auto_wg_egress_ticks` | **20** |
| `egress_failure_tick_interval` | 15 s |
| `rotation_max_duration` | 240 s |
| `circuit_breaker_threshold` / recovery | 3 / 300 s |
| `station_bad_ttl_s` (breaker superviseur) | 60 s |

---

## 2. Ce qui fonctionne bien

- **Une tâche par station**, pas de watchdog global qui mélange les états.
- **Réveil événementiel** : un `die/stop/kill` Docker ou un échec free réveille
  la boucle immédiatement, sans attendre l'intervalle.
- **Décision unique** : le tick ne logue pas « restarting » avant les gardes ;
  AUTH cooling, grâce et budget sont consultés dans `_decide_watchdog_action()`
  (`vpn/manager.py:7587-7636`).
- **Backoff borné** et réinitialisé sur un tick sain
  (`BackoffTimer`, `vpn/manager.py:721-754`).
- **Récupération sur IP fraîche** : `_watchdog_recover_fresh_ip()` re-pin le
  pays puis `_finalize_ip(allow_stale=False)` pour ne pas revenir sur le
  serveur qui vient d'échouer (`vpn/manager.py:8409-8433`).
- **Garde anti-flap** health (`ops/health.py:110-147`) et breaker superviseur
  par station.
- **Fail-soft** : une exception de tick est loguée et la boucle continue
  (`vpn/manager.py:8450-8456`).
- **6 stations live** : après bascule s1/s3 en WireGuard, les six tunnels ont
  été vus `connected` avec `egress_state=tunnel_up_full`, et le redémarrage du
  proxy a conservé la topologie.

---

## 3. Constats live (30/10/2026, ~17:45 UTC)

| Observation | Valeur / preuve |
|---|---|
| Topologie | `station_count=6`, 6 conteneurs `opencode-vpn` … `-6`, tous `healthy` |
| État à un instant de rotation | `healthy=5`, `routable=4`, s3 `tunnel_down`, s4 `action=restart`, s5 `degraded` (AUTH local) |
| `boot_error` | `boot 5/6: connected 5 — reconcile/pool` **alors qu'une rotation est en cours** — faux positif |
| `status_age_s` | 100 s, déjà monté à **312 s** (`stale=True`) pendant une rotation |
| `skipped_ticks` | s3 = **26**, s4 = **26**, s2 = 16, s5 = 13 |
| Egress armé | s3 `egress_failures=20`, `egress_armed=True`, statut pourtant `connected` |
| Logs | `s5 control=running (egress dead, transient eg_fail=21/22/23/24) — clearing stale error, set CONNECTED` |
| Logs | s1/s3 même message avec `eg_fail=20…27` |
| Budget | s1 `watchdog_restart_budget_exhausted (3/h)` à 17:00, 17:17 et 17:24 |
| Heartbeat | visible uniquement dans `logs/debug.log`, pas dans `/health` ni `/metrics` |

Ces chiffres ne sont pas une panne du proxy : ils montrent le watchdog en train
de travailler. Ils illustrent en revanche les angles morts décrits ci-dessous.

---

## 4. Défauts classés

### P1-1 — Une station egress morte est remise `CONNECTED` jusqu'à 40 ticks

**Code** : le seuil annoncé est `max(auto_wg_egress_ticks, watchdog_egress_grace_ticks)`
= **20** (`vpn/manager.py:7539-7552`). Mais la branche « half-open transitoire »
teste `_egress_failures < auto_wg_egress_ticks * 2` = **40**
(`vpn/manager.py:8745`, répété `:8982`). Si le control gluetun dit `running`,
elle remet `CONNECTED` et sort, **sans mettre `_egress_state` à
`tunnel_down`** (`vpn/manager.py:8781-8795`).

**Conséquence** :
- `egress_dead` n'atteint jamais la chaîne de recovery avant 40 ticks ;
- à 15 s/tick, une station morte reste « connectée » **~10 minutes** ;
- si `_egress_state` valait déjà `tunnel_up_full`, le pool ne peut pas la
  distinguer d'une station saine : il continue à lui envoyer des requêtes ;
- les streams en cours ne sont annulés qu'au seuil de 20
  (`pool.cancel_streams`), donc trop tard pour les requêtes déjà parties.

**Preuve live** : s3 était `connected` avec `egress_state=tunnel_down` et
`egress_failures=20` ; les logs s5 montrent `transient eg_fail=21…24` →
`set CONNECTED`. s1/s3 ont été vus jusqu'à `eg_fail=27` avant une rotation
manuelle.

**Correctif recommandé** :
1. Si `egress_dead` est vrai (compteur ≥ `_egress_dead_threshold()`), **sauter**
   la branche transitoire et aller vers la recovery.
2. Mettre `_egress_state = "tunnel_down"` dès le premier échec de sonde, et
   `tunnel_up_full` seulement après une sonde réussie.
3. Utiliser `watchdog_egress_grace_ticks` comme seule fenêtre de grâce, pas
   `auto_wg_egress_ticks * 2` (ce dernier pilote le flip WG, pas la santé).
4. Ajouter un test qui vérifie qu'à `egress_failures == threshold`, le statut
   n'est pas `CONNECTED` et que le pool exclut la station.

### P1-2 — Budget de redémarrages : double comptage et contournements

**Double comptage** : dans le même tick, le chemin light appelle
`_note_watchdog_restart()` (`vpn/manager.py:9023`), puis si le restart échoue,
l'escalade compose appelle `_ensure_container()` ; si `_auth_failed` ou
`_server_issue` est vrai, `_ensure_container(force_recreate=True)` compte
**une deuxième fois** (`vpn/manager.py:5920-5924`). Un seul incident AUTH peut
donc consommer 2 des 3 crédits horaires.

**Sous-comptage** : `_ensure_container(force_recreate=False)` fait un
`docker restart` sans compter (`vpn/manager.py:5925-5926`), `_watchdog_escalate`
appelle `_ensure_container()` sans compter (`:9227`), et `_finalize_ip()` peut
redémarrer via `_ensure_container()` (`:4031`).

**Preuve live** : s1 a atteint `watchdog_restart_budget_exhausted (3/h)` à
17:00, puis de nouveau à 17:17 et 17:24 ; la station est alors mise en
`auth_cooling 300s` et ne peut plus être réparée par le watchdog.

**Correctif recommandé** : un seul point de comptage autour de l'opération
Docker réelle, avec un identifiant de tick pour éviter le double comptage ;
compter aussi les restart « plain » et l'escalade ; exposer le budget restant.

### P1-3 — Rotation : le watchdog est totalement aveugle pendant la rotation

**Code** : `_watchdog_tick` retourne **avant** le refresh et la sonde si
`_rotation_task` est en cours (`vpn/manager.py:8549-8564`) ou si `_lock` est
tenu (`:8565-8570`). Seul un compteur `_watchdog_skipped_ticks` est incrémenté.

**Preuve live** : `skipped_ticks` = 26 pour s3 et s4, 16 pour s2, 13 pour s5 ;
`status_age_s` est monté à 312 s avec `stale=True`. Une rotation peut durer
jusqu'à `rotation_max_duration=240s` ; des rotations enchaînées peuvent
prolonger la fenêtre.

**Conséquence** : pendant une rotation, un tunnel peut mourir sans que le
watchdog le voie ; le dashboard sert un état périmé et `boot_error` peut
basculer à tort. Aucun healthcheck n'alerte sur `skipped_ticks` ou
`status_age_s`.

**Correctif recommandé** : pendant une rotation, faire au minimum une sonde
control/health légère (sans docker) et publier `watchdog_skipped_ticks` /
`status_age_s` dans `/health` et `/metrics`, avec alerte si la fenêtre dépasse
`rotation_max_duration + marge`.

### P1-4 — `stop_station()` peut être annulé par une rotation en cours

**Code** : `FleetController.stop_station()` met `_disabled=True`, appelle
`m.stop()` puis `m.stop_container()` (`ops/fleet.py:143-166`), mais ne fait pas
`request_rotation_cancel()` et n'attend pas la rotation. Le downscale de
`_apply_station_count` le fait pourtant (`opencode.py:2305-2318`).

`VPNManager.stop()` annule `_watchdog_task` **sans l'attendre** et ne touche
pas `_rotation_task` (`vpn/manager.py:1995-2019`). Une rotation déjà lancée
peut donc recréer le conteneur après le `docker rm -f`, ou un watchdog en
cours de `to_thread` peut continuer après l'arrêt.

**Conséquence** : station « arrêtée » qui réapparaît, conteneur orphelin,
deux watchdogs sur le même conteneur après un `start_station`.

**Correctif recommandé** : `stop_station` doit appeler
`request_rotation_cancel()`, attendre la fin de `_rotation_task`, puis
`stop()` avec `await` de la tâche watchdog avant `stop_container()`.

### P1-5 — Rollback d'upscale : watchdogs orphelins

**Code** : `_apply_station_count()` crée les nouveaux managers, et
`m.enabled = ...` crée immédiatement leur watchdog
(`opencode.py:2189-2199`, `vpn/manager.py:1820-1831`). En cas d'exception, le
rollback restaure la registry, les superviseurs, le pool et le watcher
(`opencode.py:2278-2297`), mais **n'arrête pas les nouveaux managers** ni leurs
watchdogs. Leurs conteneurs peuvent continuer à être recréés hors registry.

**Correctif recommandé** : dans le `except`, appeler `stop()` et
`request_rotation_cancel()` sur chaque manager créé, attendre, puis
`stop_container()` ; ne restaurer la registry qu'après.

### P2-6 — Breaker superviseur : pas de garde « dernière station », pas de single-probe

Le breaker `StationSupervisor` ouvre après `circuit_breaker_threshold` échecs
5xx (`ops/supervisor.py:143-167`). Le pool refuse alors la station
(`free/pool.py:982-994`). Mais :

- aucune garde « dernière station » : si les 6 breakers sont ouverts,
  `_best_station()` retourne `None` (`free/pool.py:1124-1154`) et le chemin
  free n'a plus de candidat ;
- le half-open n'a pas de jeton single-probe : `breaker_open()` remet
  simplement `consecutive_failures = threshold - 1`, tous les appelants
  passent (`ops/supervisor.py:158-167`) ;
- `on_ip_finalized()` remet `consecutive_failures=0` mais pas
  `breaker_open_until` (`ops/supervisor.py:200-211`) : une IP fraîche peut
  rester exclue jusqu'à la fin du cooldown ;
- le breaker superviseur n'est pas exposé dans `/api/vpn-status` ; le
  dashboard peut afficher une station « routable » que le pool refuse.

**Correctif recommandé** : réutiliser la garde N-2 du pool, ajouter un
single-probe half-open, remettre `breaker_open_until=0` dans
`on_ip_finalized`, et exposer `breaker_open` / `consecutive_failures`.

### P2-7 — `boot_error` faux positif pendant une rotation ou avec une station désactivée

`/api/vpn-status` recalcule `boot_error` à chaque poll avec
`resolved_station_count()`, qui **ignore `disabled_stations`** et ne sait pas
qu'une rotation est en cours (`dashboard/api.py:3505-3517`). Le boot fan-out,
lui, calcule correctement le nombre de stations `enabled`
(`opencode.py:3034-3058`). D'où le `boot 5/6` observé alors que s4 était
simplement en rotation.

**Correctif recommandé** : calculer `expected` avec les stations effectivement
`enabled` et non désactivées, tolérer une station `connecting` pendant
`rotation_max_duration + marge`, ou réserver `boot_error` au boot.

### P2-8 — `refresh_status(force=True)` n'est pas vraiment forcé

`_serialized_refresh` renvoie `self.get_status()` dès que `_refresh_lock` est
occupé, sans regarder `force` (`vpn/manager.py:9750-9764`). Le watchdog appelle
`refresh_status(force=True)` (`:8430`, `:8613`), mais peut recevoir un état
périmé si un dashboard/SSE tient le verrou.

**Correctif recommandé** : si `force=True`, attendre le verrou puis exécuter
le scan (ou retourner un marqueur « stale » que l'appelant sait traiter).

### P2-9 — La recovery ne remet pas à jour `_egress_state` ni les breakers

`_watchdog_recover_fresh_ip()` remet `CONNECTED`, `_error=None`, le backoff et
le churn, mais ne pose pas `_egress_state = "tunnel_up_full"` et n'appelle pas
`_breaker_charge_success()` ni `supervisor.record_success()`
(`vpn/manager.py:8409-8433`). Le `VPNManager.CircuitBreaker` n'est consulté
que par `connect_next()` (`:2194`), pas par le watchdog.

**Conséquence** : une station réparée peut rester exclue par le pool
(`tunnel_down` périmé) ou bloquée pour les rotations suivantes (breaker open).

**Correctif recommandé** : dans la queue de recovery, poser `tunnel_up_full`,
fermer le breaker VPN et appeler `record_success()` du superviseur.

### P2-10 — Hot-reload partiel du watchdog

- `watchdog_interval` n'est appliqué que si la clé est **globale** ; un
  override `per_station: {"3": {"watchdog_interval": 7}}` met à jour
  `_config` mais pas `_watchdog_interval` (`vpn/manager.py:4338-4339`).
- Un changement de `watchdog_backoff_base/max` recrée un `BackoffTimer` neuf
  et **perd** `consecutive_failures` (`:4388-4394`).
- `update_config({"enabled": ...})` écrit `_enabled` directement, sans passer
  par le setter : il ne crée pas le watchdog à l'activation et ne l'arrête pas
  à la désactivation (`:4279-4280`).
- `disabled_stations` n'est lu qu'au boot et dans `_apply_station_count`
  (`opencode.py:2834-2842`), pas par le config watch.
- `HealthPolicy` est construit une seule fois dans `__init__`
  (`vpn/manager.py:1568-1575`) ; `stations.health` n'est pas hot-reloadé.

**Correctif recommandé** : unifier ces handlers, réutiliser le setter
`enabled`, fusionner les overrides per-station avant d'appliquer, et
reconstruire `HealthPolicy` sur changement.

### P2-11 — Le chemin egress ignore `_auth_cooling()`

`_decide_watchdog_action()` ne consulte `_auth_cooling()` que si le chemin est
AUTH (`vpn/manager.py:7601-7628`). Un tick classé `egress dead` avec
`_auth_failed=False` peut donc redémarrer alors qu'un cooldown AUTH local est
actif. Le budget épuisé force bien `_auth_cool_until` (`:8880-8889`), mais la
décision non-AUTH ne la lit pas.

**Correctif recommandé** : consulter `_auth_cooling()` au début de toute
branche de recovery, quel que soit `kind`.

### P2-12 — Arrêt / tâches : annulations non attendues

- `stop()` cancel le watchdog sans `await` (`vpn/manager.py:2011-2013`) ;
- `_rotation_task`, `_server_issue_recheck_task`, `_socks_catchup_task` et
  `_save_state_task` ne sont pas annulées ;
- au shutdown, les tâches de boot sont attendues via
  `wait_for(shield(task), 5.0)` (`opencode.py:3524-3550`) : au timeout, elles
  **continuent** ;
- le `DockerEventWatcher` peut être démarré avant d'être publié dans
  `shared_state.docker_event_watcher` (`opencode.py:3069-3082`) : fenêtre
  d'orphelin au shutdown ;
- les workers de rotation du pool n'ont pas d'arrêt déterministe
  (`free/pool.py:2572-2671`) ; `EventManager._flush_loop` n'a pas d'arrêt
  explicite.

**Correctif recommandé** : un `shutdown()` ordonné par manager (cancel +
await watchdog/rotation/refresh), puis arrêt du watcher et des workers pool.

### P3-13 — Le watchdog peut ticker avant `start()`

Le setter `enabled` crée la tâche (`vpn/manager.py:1820-1831`) alors que
`opencode.py` l'affecte avant le registry, le pool et le `boot_fanout`
(`opencode.py:2839-2842`, `:2907`, `:3028`). `_watchdog_event` n'existe pas
encore (`:1955-1956`) : les événements Docker de cette fenêtre ne réveillent
rien. Les seuils limitent l'impact, mais le premier tick peut scanner Docker
avant le vrai démarrage.

**Correctif recommandé** : créer le watchdog uniquement dans `start()`, ou
poser un gate `if self._watchdog_event is None: return`.

### P3-14 — Horloges et observabilité incomplètes

- `_now_fn` est injectable, mais `_watchdog_escalate` utilise
  `time.monotonic()` directement (`vpn/manager.py:9201`) ;
- `_watchdog_last_action` n'est pas remis à `None` sur un tick sain
  (`:1735`, `:8816`) : le dashboard peut afficher une ancienne action ;
- le heartbeat n'est pas exposé dans `get_status()` (`:4657-4741`) ; seuls
  les logs le portent ;
- `/metrics` n'expose que les restarts (`opencode.py:4075-4085`), pas
  heartbeat, skipped, grace, ni breaker superviseur ;
- `_watchdog_skipped_ticks` ne compte que les skips rotation/lock, pas les
  retours précoces AUTH/grâce/budget.

**Correctif recommandé** : exposer heartbeat age, last_action, skipped,
breaker superviseur et grace dans `/health` et `/metrics`.

---

## 5. Couverture de tests

La suite complète est verte, mais les chemins suivants ne sont pas couverts :

| Trou | Pourquoi c'est important |
|---|---|
| `_watchdog_event.wait()` réel et exception de boucle | le réveil événementiel est le cœur du watchdog |
| Jitter ±20 % | désactivé sous pytest (`vpn/manager.py:8467-8471`) |
| Gates `enabled` / `_desired_connected` | empêchent de reconnecter une station volontairement arrêtée |
| Skip `_lock` et skip rotation | mesuré à 26 ticks live, jamais testé |
| Branche transitoire `control=running` + egress mort | **P1-1**, aucun test |
| Budget double comptage / `_ensure_container(False)` / escalade | **P1-2**, aucun test |
| `stop_station` + rotation en cours | **P1-4**, aucun test |
| Rollback upscale et watchdogs orphelins | **P1-5**, aucun test |
| Breaker superviseur dans le pool, last-station, single-probe | **P2-6**, aucun test |
| `ops/health.py` en mode production | sous pytest, hystérésis et holds sont forcés à 0 (`ops/health.py:69-72`) |
| Intégration `record_success/failure` HTTP → superviseur | aucun test |
| `start()`/`stop()` créent/annulent réellement les tâches | aucun test direct |
| Heartbeat et métriques calculées depuis les managers réels | rendu testé avec snapshot codé en dur |

Tests faibles ou contradictoires repérés :

- `tests/test_chaos_lifecycle.py:75` : `assert ... == 0 or True` (tautologie) ;
- `tests/test_gluetun_control_api.py` : plusieurs `assert len(puts) in (0,1)` /
  `in (0,2)` qui passent même si le fast-pin ne fait rien ;
- `tests/test_hot_reload_phase4.py:188-221` : ne teste que la persistance YAML,
  pas `update_config()` ni la cadence live ;
- `tests/test_rotation_n_stations.py:37-60` : un `except` peut skip toute la
  boucle N stations ;
- `tests/test_fleet_stop_p0.py` : ne vérifie pas l'exclusion réelle du pool.

---

## 6. Plan de correction recommandé

### Lot 1 — P1, avant tout nouveau réglage de flotte

1. **Egress** : seuil unique `watchdog_egress_grace_ticks`, sortie immédiate
   vers recovery dès `egress_dead`, `_egress_state=tunnel_down` au premier
   échec, test de non-régression pool.
2. **Budget** : comptage unique par tentative Docker réelle, toutes voies
   comptées, test du double comptage AUTH + compose.
3. **Stop/rotation** : `request_rotation_cancel()` + `await` avant
   `stop_container()`, `stop()` attend watchdog et rotation.
4. **Rollback upscale** : arrêt des managers créés avant restauration de la
   registry.
5. **Observabilité** : `boot_error` basé sur les stations `enabled` et
   tolérant `connecting`, `skipped_ticks` / `status_age_s` dans `/health`.

### Lot 2 — P2

6. Breaker superviseur : garde dernière station, single-probe, reset sur IP
   fraîche, exposition dashboard.
7. `_serialized_refresh(force=True)` : attente ou marqueur stale.
8. Recovery : `_egress_state=tunnel_up_full`, `_breaker_charge_success()`,
   `supervisor.record_success()`.
9. Hot-reload : `enabled` via setter, per-station `watchdog_interval`,
   `disabled_stations`, `HealthPolicy`.
10. AUTH cooling consulté sur toutes les branches de recovery.
11. Shutdown ordonné (watchdog, rotation, watcher Docker, workers pool).

### Lot 3 — P3

12. Créer le watchdog dans `start()` uniquement (ou gate event).
13. Exposer heartbeat/last_action/skipped/breaker dans `/health` et
    `/metrics`, et ajouter des alertes.
14. Compléter les tests listés au § 5, en commençant par P1-1, P1-2 et P1-4.

---

## 7. Conclusion

Le watchdog n'est pas cassé : il a réparé des stations réelles et la flotte
6 stations est actuellement opérationnelle. Mais il est **trop tolérant sur
l'egress** et **trop conservateur sur le budget**, ce qui peut laisser une
station morte « connected » pendant plusieurs minutes et épuiser les
redémarrages en un seul incident. Les rotations, le hot-reload et le rollback
d'upscale ajoutent des fenêtres où la supervision est incomplète.

Le correctif P1-1 (egress) est le plus rentable : il réduit directement les
timeouts de requêtes free et rend le pool cohérent avec la sonde réelle.
