# Implementation log — Phase 0 / Phase 1

**Date** : 2026-11-05  
**Feature** : `002-vpn-catalog-rotation`  
**Portée** : phases 0–2.5 exécutées, D-01 fermé, `registry.db` seule
source d’état runtime. Le 6/6 free reste bloqué par le quota amont (429
journaliers sur les IP testées), pas par l’infrastructure.

---

## Synthèse 2026-11-05 (soir)

- `registry.db` est la seule source runtime : claims, quota, baux, états de
  chaîne, pool, rotation partagée, moteur de latence, audit de scan.
- Les fichiers legacy (`ip_registry.db`, `catalog_claims.db`,
  `quota_ledger.json`, `fresh_chain_state*.json`, `shared_rotation.json`,
  `free_pool.json`, `allocator_state.json`) ont été migrés puis renommés
  `.migrated` ; aucun n’est recréé.
- **Cause racine des binds `no-egress`** : le repli DNS `plain` était écrit
  sans adresses IPv4, puis avec un format sans port. Gluetun refusait de
  démarrer (`upstream plain addresses ... 0 addresses`, puis
  `not an ip:port`). Corrigé en `1.1.1.1:53,8.8.8.8:53,9.9.9.9:53` dans le
  compose de base et dans l’override par station. Les conteneurs démarrent
  maintenant `healthy` et le DoT répond.
- Image Gluetun épinglée par digest
  (`qmcgaw/gluetun@sha256:4af0c…`) ; synchronisation du manifeste atomique
  (tmp + `mv` + `test -s` + `grep`) avant chaque bind.
- Le dashboard `/api/free/chain` expose les 6 stations, le transport réel,
  l’avancement, le débit, l’ETA, le prochain candidat et la dernière raison.
- **D-12 fermé** : image Gluetun épinglée par digest et manifeste installé
  atomiquement puis vérifié avant chaque bind.
- **D-13 fermé** : plus aucun JSON d’état runtime ; tous les documents sont
  dans `chain_state`/`quota_buckets`/`leases` de `registry.db`.
- **D-22 fermé** : `tests/test_import_rules.py` vert, `architecture.md` et
  les ADR décrivent la règle d’import domaine/application/adapters.
- **D-14 fermé** : `scripts/config_coverage.py` cherche désormais aussi les
  consommateurs dans `scripts/` (hors lui-même), le gate `--baseline` passe
  avec **3 clés mortes connues et documentées** : la métadonnée géo
  `last_checked_at` et deux entrées de la table `thinking.effort_caps`
  (`muse_spark`, `ling-3.0-flash-fin`) qui sont des données de modèles, pas
  des knobs. Elles sont conservées pour ne pas changer les plafonds
  d’effort.
- **Preuve reset (2026-11-05 ~23:10 UTC)** : l’egress VPN
  `186.247.161.149` renvoie 429 sur flint/nemotron avec un reset à
  ~4 165 s, tandis que l’IP hôte répond 200 sur ces mêmes modèles. Le quota
  est donc indexé sur l’IP d’egress et sa fenêtre journalière, pas sur le
  modèle seul.
- **Sonde multi-modèles** : `probe_models` couvre maintenant les 10 modèles
  free vivants + `big-pickle` (au lieu de deepseek/glm/mimo). Diagnostic
  direct depuis l’IP hôte : `flint-40b-free`,
  `nemotron-3.5-lightning-free` et `nemotron-3.5-super-vl-free` répondent
  **200**, tandis que les egress VPN testés restent 429. La recherche
  échantillonne donc aussi ces buckets.
- **T060 démarré** : `ip_rotation.station_health.shadow: true` depuis
  2026-11-05 ~21:45 UTC. Le sélecteur `BestIpSelector.rank()` calcule et
  journalise `[shadow-selector]` sans changer le routage legacy ; test
  `test_shadow_selector_logs_but_keeps_legacy_routing`. Fin de fenêtre
  nécessaire : 24 h.
- **D-24 fermé** : le superviseur écrit `logs/allocator_events.jsonl`
  (timestamp UTC, event, station, pid, transport, compteurs d’incidents)
  en plus des logs texte ; test
  `test_allocator_event_is_jsonl_and_correlated`.
- **D-17 fermé** : `.env`, `credentials.env` et `vpn/credentials.txt` sont
  gitignorés ; `tests/test_secret_hygiene.py` vérifie ces règles et scanne les
  sources pour toute clé littérale (`VPN_CONTROL_API_KEY`, `OPENVPN_PASSWORD`,
  `WIREGUARD_PRIVATE_KEY`).
- **D-11 fermé** : `requests.db` est passé de 2,8 Go à 896 Mo
  (220 664 lignes > 7 jours archivées dans `logs/archive/`, backup vérifié),
  avec purge des corps à 04:00 et maintenance registre toutes les 6 h. Les
  agrégats `token_counters_daily` restent la source des compteurs.
- **D-21 fermé** : les valeurs partagées sont générées depuis `config.yaml`
  (`SERVER_COUNTRIES`, clé control API), `credentials.env` est la source
  unique des identifiants, et `test_env_staleness_guard.py` couvre la
  détection/résolution des divergences `.env`/process. `config_audit` et
  `config_coverage` servent de garde-fous.
- **D-08 fermé** : les réparations sont des chaînes par station
  (`RepairOrchestrator`/superviseur) ; un 429 quota réel n’alimente plus le
  breaker amont, et un événement amont exige plusieurs couples
  (station, IP) distincts, pas une station seule. Tests
  `test_upstream_event.py`.
- **D-15 fermé** : couverture chaos/fallback élargie — DNS/DoT/plain,
  egress SOCKS5/HTTP/control/docker, double failover stream, restart/churn,
  redémarrage superviseur, baux expirés, canary fiabilité et canary WG.
- **D-23 fermé** : les resets de quota sont en UTC (`next_utc_midnight`,
  `Retry-After`), testés dans `tests/test_quota_ledger.py` ; le seul
  `localtime` restant est une fenêtre de maintenance opérateur, pas un
  calcul de reset.
- **D-25 fermé** : la maintenance `run_once` (purge observations/incidents/
  config, checkpoint, intégrité, backup `VACUUM INTO`) tourne au boot et
  périodiquement (`ip_registry.maintenance_interval_s`, 6 h par défaut).
- **Vérification** : suite pytest complète verte ; `config_audit.py --strict`
  (468 clés, 238 lues, 0 dead, 0 missing) ; `debt_gate.py --strict`
  (0 P0 ouverte) ; `PRAGMA integrity_check=ok`.
- **Live** : les 6 conteneurs démarrent `healthy` et obtiennent un egress
  (WireGuard ou OpenVPN). Les IP testées depuis le 04/11 renvoient encore
  `429` avec reset journalier ; le pool n’est pas déclaré vide et les
  chaînes continuent d’échantillonner de nouveaux pays/IP.
- **SpecKit** : 52 tâches faites, 14 partielles, 10 restantes (dont les
  validations temporelles T060–T064, les mesures live T009/T020/T045/T046,
  la modularisation T071 et la clôture des dettes P1/P2 T072).

---

## Phase 0 — Garde-fous

| Tâche | État | Détail |
|---|---|---|
| T001 baseline SLO | fait (audit) | chiffres dans `audit.md` |
| T002 test hardcodes | fait | `tests/test_no_hardcoded_geo.py` |
| T003 cohérence config | fait (existant) | `tests/test_env_staleness_guard.py`, `config_audit.py --strict` |
| T004 non-régression bugs | partiel | tests lease, hostname vide, manifeste, classement |
| T005 snapshot | partiel | backup DB fait ; export catalogues à faire |
| T006 API vs Gluetun | fait | `audit.md` §2.8 |
| T007 tunnels isolés | fait | `audit.md` §2.3 / §2.11 |
| T008 IP disponibles | fait | `audit.md` §2.10 |
| T009 budget sessions | à faire | test contrôlé 6/7/8 sessions |

---

## Phase 1 — Quick wins réalisés

| Tâche | État | Fichiers |
|---|---|---|
| T010 supprimer `PROVEN_HOSTS` | fait | `scripts/fresh_chain.py` |
| T011 supprimer `_is_gold_ip` | fait | `scripts/fresh_chain_supervisor.py`, tests |
| T012 classement par qualité | fait | `scripts/fresh_chain.py` (`_host_success`, score) |
| T013 vérifier le manifeste station | fait | `read_station_manifest`, `fresh_chain.run` |
| T014 lease 600 s + renew | fait | `free/catalog_claims.py`, `fresh_chain.py` |
| T015 superviseur seul | fait | `config.yaml` (`station_repair.enabled: false`) |
| T016 station 7 | fait (opérationnel) | conteneur arrêté, restart=no |
| T017 lignes sans hostname | fait | DB archivée + `eligible()` filtre |
| T018 429 pinné | partiel | `free/repair_queue.py` + pool publie la demande + superviseur force la station ; effet live après redémarrage proxy |
| T019 `client_usable` | fait | `free/pool.py` expose `client_usable`, `transport_effective`, `model_usable_by_model` |
| T020 test de charge | à faire | après stabilisation |
| T021 périmètre pays | partiel | sélection ne filtre plus le pays ; `server_countries` manager reste |
| T022 démarrage conserve | partiel | fast-path existant ; règle superviseur à renforcer |
| T023 fallback IP | fait | `free/ip_registry/adapters/egress.py` : SOCKS5h → SOCKS5 → HTTP → control API Gluetun → `docker exec` |
| T024 fallback DNS | partiel | `docker-compose.yml` : DoH cloudflare + google + quad9 (testé, egress OK) ; fallback DoT/plain à faire |
| T025 contrat GUI | partiel | poids du score lus depuis `ip_registry.selection.score_weights.*` et testés ; manifeste GUI complet à faire |
| T026 tri-stack auto | fait | `fresh_chain.py --transport auto` + `build_auto_candidates` + boucle par serveur ; `allocator.transports: auto` ; `free_quota_mark` synchronise `_stack_effective` |

---

### Correctif de robustesse associé

- `FreeIPPool._ensure_proactive_ticker()` ne crée plus de tâche asyncio quand
  aucun event loop n’est actif (`set_stations()` appelé depuis un test ou un
  outil synchrone). Sans ce garde, `set_stations()` levait
  `RuntimeError: no running event loop` et laissait une coroutine non
  attendue.

---

## Changements de configuration

- `ip_registry.allocator.max_candidates_per_chain: 20 → 500`
- `ip_registry.allocator.rebind_limit_per_min: 6 → 12`
- `ip_registry.allocator.max_parallel_repairs: 3 → 6`
- `ip_registry.allocator.lease_ttl_s: 300 → 600`
- `ip_registry.allocator.host_429_cooldown_s: 10 → 600`
- `ip_registry.allocator.no_egress_backoff_s: 10 → 120`
- `settle_s` / `egress_wait_s` laissés à 12/45 tant que le préflight n’est
  pas implémenté (réduction prévue en Phase 3)
- suppression de `allocator.max_per_24` et `allocator.max_per_country`
  (aucun quota pays/24 ; seule l’unicité d’IP compte)
- `ip_rotation.station_repair.enabled: true → false`
- `ip_registry.allocator.transports`: `wireguard` → `auto` pour les 6 stations
- ajout de `ip_rotation.transport_order: [wireguard, openvpn-udp, openvpn-tcp]`
- ajout de `ip_registry.selection.score_weights` (40/25/20/10/5), lus par le
  classement des candidats

---

## Changements base de données

- Backup : `logs/ip_registry.db.bak-20261105-before-empty-host-archive`
- Création de `legacy_ip_state`
- Archivage de **3 267** lignes `ip_state.hostname=''`
- `ip_state` ne contient plus de ligne sans hostname
- `eligible()` filtre désormais `hostname <> ''`

Note : la table `legacy_ip_state` contient 3 267 lignes uniques après
dédoublonnage.

---

## Tests exécutés

- `tests/test_no_hardcoded_geo.py` : vert.
- `tests/test_fresh_chain_candidates.py` : vert.
- `tests/test_catalog_claims.py` : vert (lease 600 s + renew).
- `tests/test_ip_registry_sqlite_registry.py` : vert (hostname vide exclu).
- `tests/test_ip_registry_egress.py` : vert (SOCKS5h → SOCKS5 → HTTP →
  control API → `docker exec`).
- `test_build_auto_candidates_merges_transports` + `_transport_parts` :
  verts.
- `tests/test_fresh_chain_auto_transport.py` : vert (WireGuard échoue,
  OpenVPN TCP réussit sur le même hostname).
- `tests/test_repair_queue.py` : vert (demande idempotente, complete, TTL).
- `test_score_weights_are_configurable` : vert (un poids succès à 1.0 change
  l’ordre des candidats).
- Test DNS `cloudflare,google,quad9` : Gluetun démarre, DNS ready, egress UK
  OK (`194.88.96.71`).
- Dry-run réel `fresh_chain.py --station 2 --transport auto --dry-run` :
  manifeste station lu (WG 9 787, OVPN UDP 9 786, OVPN TCP 9 786), 9 787
  candidats fusionnés, sortie 0.
- Live après redémarrage superviseur : chaînes 1/4/6 en `transport auto`,
  slice 500 ; la flotte était encore 0/6 `client_usable` au moment du
  contrôle, réparation en cours. Aucun succès n’est déclaré avant preuve
  200.
- Preuve live du tri-stack : station 1, `ca1941` — WireGuard
  `no-egress`, OpenVPN UDP egress `186.247.35.85` puis `429`, OpenVPN TCP
  `no-egress`, serveur suivant. Le comportement demandé est bien appliqué.
- **Constat de performance** : un serveur peut coûter ~6 min (WG + OV UDP +
  OV TCP) quand les trois échouent. À 6 stations, le catalogue complet reste
  trop lent sans le préflight (T040) et la réduction des timeouts (T042). Le
  préflight devient la priorité performance de la Phase 3.
- `tests/test_station_allocator_supervisor.py` : vert (plus de protection
  « gold »).
- `tests/test_pool_station_set.py` : vert.
- `tests/test_station_count_config.py`, `test_station_repair.py`,
  `test_apply_station_count.py`, `test_env_staleness_guard.py` : verts.
- `scripts/config_audit.py --strict` : **446 clés, 216 lues, 0 dead,
  0 missing**.
- Suite élargie hors `test_ip_registry_probe.py` : **164 passed**.
- Suite pools/stations/stack : **~230 passed**, 1 skipped.
- `test_free_station_reliability.py` + `test_station_isolation.py` : verts
  après le garde `_ensure_proactive_ticker`.
- `test_ip_registry_probe.py` : 8 échecs **préexistants**, liés au payload
  officiel modifié dans le working tree (`official_payload.json`), pas aux
  changements Phase 1. À traiter séparément.
- `test_free_shims_registry.py::test_les_outils_officiels_sont_imposes` :
  échec **préexistant** lié à la liste d’outils du harness, pas à Phase 1.

---

## Effets opérationnels

- `opencode-vpn-7` arrêté (`restart=no`) : hors `station_count=6`.
- Aucun conteneur de test `audit-vpn-*` restant.
- La station 7 n’est plus réparée ni comptée.
- **Déploiement** : le superviseur a été redémarré (PID 8664) pour charger
  `TRANSPORTS` avec `auto`. Les chaînes 1/4/6 ont démarré en `transport auto`
  (slice 500). Les chaînes 2/3/5 déjà en cours (ancien code, slice 20) ont été
  laissées finir pour ne pas interrompre les binds. Le proxy n’a pas été
  redémarré.

---

## Phase 2.5 — base de données

- T080 : ADR + carte des modules dans `architecture.md` ; package `rotation/`
  créé (domain, application, ports, infrastructure, interfaces).
- T081 : schéma cible `rotation/infrastructure/db/schema.sql` créé, avec
  `schema_migrations`, serveurs/transports/endpoints, IP, observations,
  quotas, leases, warm pool, sweep, repair, fallbacks, incidents, état
  stations, config et scores.
- `rotation/infrastructure/db/migrate.py` : `connect`, `apply_schema`,
  `current_version` (WAL, foreign_keys, idempotent).
- Tests : `tests/test_rotation_schema.py` → **3 passed** (idempotence,
  foreign keys, CHECK, unicité des leases actifs).
- T082 : `rotation/infrastructure/db/repositories.py` — `RegistryRepository`
  + `UnitOfWork` : sources, serveurs/transports/endpoints, IP, observations,
  lease atomique, warm spare, repair queue, scores.
- T086 : `compute_scores()` matérialise `score_snapshots` depuis les
  observations fenêtrées.
- Tests : `tests/test_rotation_repositories.py` → **2 passed** (unicité de
  lease, release, scores, repair queue).
- T083 : `rotation/infrastructure/db/migrate_legacy.py` migre
  `servers`, `server_transports`, `endpoints`, `ip_state`, `observations`,
  `assignments` et `verified_spares` vers le schéma cible ; test sur DB
  legacy synthétique (`tests/test_rotation_migration.py`).
- T084/T085 : `rotation/infrastructure/db/retention.py` — purge par table,
  `wal_checkpoint`, `integrity_check`, backup `VACUUM INTO` ; test de
  restauration.
- T087 : `tests/test_import_rules.py` — le domaine n’importe aucune I/O
  (`sqlite3`, `subprocess`, `requests`, `httpx`, `asyncio`, `docker`) et
  n’importe pas les adapters.
- Tests schéma/migration/repositories/import/repair queue : **12 passed**.
- Tests config typée + debt gate + repositories : **6 passed**.
- `scripts/debt_gate.py --strict` : **3 P0 ouvertes** (D-01, D-09, D-18),
  gate volontairement rouge avant la fin Phase 2.5.

## Correctifs live 2026-11-05 (DNS, MTU, egress, recherche)

Trois causes racines ont été isolées par des tests contrôlés, pas par
supposition :

1. **DoH mort** : toutes les requêtes `dns.google`, `cloudflare-dns.com` et
   `dns.quad9.net` expiraient ; Gluetun redémarrait le tunnel en boucle.
   `DNS_UPSTREAM_RESOLVER_TYPE=dot` a été validé live (DNS + egress OK) et
   remplace DoH dans les quatre blocs Compose. Un repli `plain` a aussi été
   validé (conteneur jetable, `1.1.1.1:53/8.8.8.8:53`).
2. **`WIREGUARD_MTU=1280` nuisible** : avec la même clé et le même serveur
   `ca1947`, un conteneur au MTU par défaut est sain ; avec 1280, chaque
   healthcheck DNS expire et le tunnel redémarre toutes les 6 s. Le MTU WG
   n'est plus forcé (MSSFIX OpenVPN conservé).
3. **Cibles de healthcheck IP** : forcer `1.1.1.1:443/8.8.8.8:443` a le même
   effet sur les serveurs qui bloquent ces adresses ; les cibles par défaut
   (`cloudflare.com`, `github.com`) sont revenues.

Autres correctifs :

- `StationEgress` rejette l'IP publique de l'hôte : l'API de contrôle Gluetun
  renvoie l'IP de l'hôte quand le tunnel est arrêté. Sans ce filtre, un bind
  mort était accepté comme « egress » puis marquait l'IP de l'hôte en 429.
- L'API de contrôle est interrogée après un contrôle `status=running` et son
  JSON `public_ip` est parsé (l'ancien code comparait le JSON brut à une IP).
- `GluetunControlBinder` : bascule de hostname en quelques secondes ;
  `HybridBinder` l'utilise pour le transport déjà configuré et garde Compose
  pour un changement de transport. Le binder Compose ne transmet plus les
  `VPN_TYPE_STATION*`/`SERVER_HOSTNAMES_STATION*` obsolètes du process proxy.
- Egress rapide : 2 URLs et timeout 3 s (au lieu de 3×5×8 s = 120 s) ;
  le binder de contrôle utilise `control_only` (~1 s) et un settle de 2 s.
- Recherche : filtre des /24 et /16 déjà 429, apprentissage
  endpoint-/16 → egress-/16 depuis les 429, et round-robin des pays dans le
  tier public. Ce sont des heuristiques de performance, pas des quotas pays.
- Un seul propriétaire de réparation : `station_repair.enabled=false` coupe
  le watchdog, la rotation proactive (`rotation_planner.enabled=false`),
  `on_429_action=cooldown` et la rotation du pool ; le superviseur reste
  seul à lancer les chaînes. Les gates sont testés par la suite existante.
- **Bail WireGuard global** (`transport_leases`) : la clé NordLynx partagée
  ne supporte qu'une session active. Une station qui détient le bail garde
  WireGuard une heure ; les autres passent en OpenVPN au lieu de la
  déloger. `CatalogClaims.acquire_transport/renew_transport/
  release_transport` ; test dans `tests/test_catalog_claims.py`.
- **Backoff pays temporaire** : après 3 échecs dans le même pays pendant un
  run, le pays est mis de côté 30 min (`abandoned`, pas un quota permanent).
  C'est ce qui a fait sortir les chaînes de la Suisse, où elles tournaient
  depuis 08:10.
- **État live à 09:03 UTC** : 0/6 `model_usable`. Les chaînes ont quitté la
  Suisse (backoff) et échantillonnent Chypre, République tchèque, Allemagne
  (`cy36`, `cz109`, `cz117`, `de1104`, `de1106`). Les egress déjà vus sont
  de vrais `FreeUsageLimitError` (reset ~minuit UTC), pas des pannes de
  tunnel. La recherche continue ; aucun succès n'est déclaré sans 200.

Tests ajoutés/verts :

- `tests/test_gluetun_control_binder.py` : **5 passed**.
- `tests/test_hybrid_binder.py` : **6 passed**.
- `tests/test_ip_registry_egress.py` : **9 passed** (host IP, control_only).
- `tests/test_ip_registry_compose_binder.py` : **9 passed** (env obsolète).

État live au moment de la rédaction : station 1 est **saine** en
WireGuard+DoT (`ca1947`, egress `186.247.35.136`) ; la sonde officielle
renvoie un vrai `FreeUsageLimitError` (429, reset ~minuit UTC), pas une
panne de tunnel. Les six chaînes continuent la recherche d'une IP dont le
quota n'est pas déjà consommé ; aucun 200 n'est déclaré avant preuve.

## État au 2026-11-05 10:00 UTC

- **DNS** : DoH mesuré mort ; DoT validé live et déployé. Repli plain validé
  en conteneur jetable.
- **MTU** : `WIREGUARD_MTU=1280` mesuré nuisible (healthcheck DNS en boucle) ;
  MTU par défaut restauré, MSSFIX OpenVPN conservé.
- **Egress** : l'IP publique de l'hôte est rejetée ; l'API de contrôle est
  parsée (`status=running`, `public_ip`) ; `control_only` évite les 3×5×8 s.
- **Réparation** : un seul propriétaire (watchdog/rotation pool coupés,
  superviseur seul). Bail WireGuard global pour la clé NordLynx partagée.
- **Quota** : ledger par IP (`bucket=default` pour les modèles chat free) ;
  un 200 remet `exhausted=false`. ~700 IP consommées, catalogue 9787.
- **Recherche** : `stop_after_429=true` et `fast_discovery=true`
  (`fast_discovery_transport=openvpn-udp`) : un transport par station,
  bascule de hostname par l'API de contrôle, ~20-30 s par host au lieu de
  2-6 min. Le tri-stack complet reste disponible avec `fast_discovery=false`.
- **Live 10:00 UTC** : 0/6 `model_usable`. Les chaînes échantillonnent
  Canada, Suisse, Colombie (`ca1981`, `ch475`, `co19`, `co25`) ; egress
  `187.13.11.66` (s1), `187.15.143.213` (s5), tous 429. Aucun 200 depuis le
  04/11 ; aucun quota global de compte n'est affirmé, seules les IP testées
  ont consommé leur quota journalier.
- **Préflight (T040/T041)** : `free/ip_registry/preflight.py` —
  `PreflightProber` DNS + endpoint TCP/UDP + handshake injectable, budget,
  `preflight_many` avec concurrence bornée (64) et résumé RTT/erreurs.
  `tests/test_ip_registry_preflight.py` : **8 passed**.
- **SweepCursor (T036)** : table `sweep_cursor` + méthodes repository
  `record_sweep`/`next_sweep`/`sweep_progress` + `rotation/application/sweep.py`.
  `tests/test_rotation_sweep.py` : reprise après réouverture, backoff, progrès.
- **Tests** : suite pytest complète **0 échec** après migration du contrat
  officiel OpenCode 2.2.5 (122 échecs corrigés) et ajout préflight/sweep ;
  `config_audit.py --strict` 464 clés, 234 lues, 0 dead, 0 missing ;
  `debt_gate.py --strict` toujours rouge sur D-01/D-09/D-18.
- **WarmPool (T033)** : `rotation/application/warm_pool.py` — TTL, promotion
  atomique (SELECT+DELETE), cible de réapprovisionnement, statut ready/missing.
  `tests/test_rotation_warm_pool.py` : **2 passed**.
- **RepairOrchestrator (T034)** : `rotation/application/repair.py` — un job
  par station, parallélisme borné, résultat par station, réconciliation au
  boot. `tests/test_rotation_repair.py` : **3 passed**.
- **T038** : test d'intégration 100 candidats 429 → 200 avec binder/probe
  factices (`test_ip_registry_allocator.py`), et deux réparations parallèles
  (RepairOrchestrator).
- **D-18 fermé** : `rotation/infrastructure/db/maintenance.py` +
  `scripts/registry_maintenance.py` — schema versionné, purge, checkpoint,
  integrity check, backup `VACUUM INTO`. Appelé au boot du superviseur
  (`registry maintenance: ...`), testé dans
  `tests/test_registry_maintenance.py`.
- **D-09 fermé** : `free/ip_registry/dns.py` (`DnsFallback` DoT → plain) +
  `dns_mode` dans le binder Compose/hybride + câblage dans la chaîne
  (repli après 2 échecs, reset sur bind réussi). Tests :
  `tests/test_ip_registry_dns.py`.
- **D-01 — migration des claims** : `rotation/infrastructure/db/claims.py`
  copie `catalog`, `claims`, `rebind_budget`, `transport_leases` dans
  `logs/registry.db` (idempotent). La maintenance du superviseur migre au
  boot ; `fresh_chain` et le superviseur utilisent désormais
  `logs/registry.db` par défaut. Migration live vérifiée : **9788 catalog /
  3223 claims**, ancien `catalog_claims.db` renommé `.migrated`, chaîne
  station 1 sur `db=...\\logs\\registry.db`.
- **D-01 — migration de l’état de rotation** : `free/chain_state.py` porte
  désormais les documents `fresh_chain_state*.json`, `shared_rotation.json`,
  `latency_engine.json`, `free_pool.json` et `allocator_state.json` dans la
  table `chain_state` de `registry.db`. Les lecteurs runtime
  (`opencode.py`, `free/ip_registry/status.py`, `free/pool.py`,
  `free/rotation.py`, superviseur) lisent `registry.db` ; les fichiers legacy
  ont été migrés puis renommés `.migrated`.
- **D-01 — migration du registre IP runtime** : `free/ip_registry/registry_db.py`
  remplace `SqliteIpRegistry` dans le proxy et le superviseur pour les
  opérations `reconcile` / `assigned_for_station` / `release_station` /
  `assign_observed`. Les baux vivent dans `leases`, les IP dans `egress_ips`,
  les observations dans `observations`. `logs/ip_registry.db` n’est plus
  recréé au démarrage.
- **D-01 — migration du ledger de quota** : backend SQLite dans
  `free/quota_ledger.py` (`path=*.db` → table `quota_buckets`), config
  `ip_quota.ledger_path: logs/registry.db`. Migration live :
  **1247 buckets** importés, `quota_ledger.json` renommé `.migrated`, proxy
  redémarré et `/api/pool-status` lit bien le ledger SQLite. Tests :
  `tests/test_quota_ledger_sqlite.py`.
- **D-01 — fermé** : plus aucune source de vérité JSON ou DB legacy lue par
  le runtime. `debt_gate.py --strict` passe ; `registry.db` est la seule base
  de décision. Tests : `tests/test_chain_state.py`,
  `tests/test_shared_rotation.py`, `tests/test_latency_engine_state.py`,
  `tests/test_registry_db_runtime.py`, `tests/test_legacy_ip_migration.py`.
- **Avancement SpecKit** : 50/90 tâches faites, 10 partielles, 30 à faire
  (T030–T032, T035–T038, T040–T041 vérifiées ; T033/T034/T042 partielles).

## Limites connues de cette étape

- Le `transport_effective` est synchronisé par `free_quota_mark` quand le
  proxy tourne avec le nouveau code ; avant redémarrage, il peut rester
  l’ancien.
- T024 est fermé : DoT primaire, repli plain avec adresses IPv4 `:53`
  explicites (`DNS_UPSTREAM_PLAIN_ADDRESSES`), sinon Gluetun refuse de
  démarrer (« not an ip:port » / « 0 addresses »). Tests
  `tests/test_ip_registry_dns.py`.
- T025 est partiel (poids du score configurables ; manifeste GUI complet à
  faire).
- T088 : `rotation/infrastructure/config/schema.py` — `RotationConfig`
  typée (selection, transport, repair), poids normalisés, ordre de transport
  validé.
- T089 : `scripts/debt_gate.py` + `debt_status.json` — le gate passe,
  **0 P0 ouverte** ; `registry.db` est branché au runtime.
- T018 est partiel : la file de réparation existe, mais le proxy doit être
  redémarré pour publier les demandes.
- Le proxy doit être redémarré pour charger `free/pool.py`, `opencode.py` et
  la config GUI. Le superviseur, lui, a été redémarré et utilise `auto`.

## Prochaine étape

Phase 3/4 : T043–T047 (manifeste atomique, image épinglée, mesures
préflight/bind, sweep, dashboard) puis T050–T066 (fiabilité, tests de panne,
validation temporelle) et T070–T075 (décommissionnement, modularisation).
