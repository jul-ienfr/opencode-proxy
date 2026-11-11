# Journal d’implémentation — 014 qualification exhaustive NordVPN

## État

- Feature : `014-nordvpn-exhaustive-rotation`.
- Spécification : 12 stories, 130 exigences fonctionnelles, 85 critères de
  succès.
- Gate de release : `python scripts/quality_gate.py --with-soak` — 10/10 PASS.
- Dernier relevé : **522 tests** du gate, couverture **93 %**, 712 fichiers audités,
  0 violation de taille, 0 cycle, `config_audit` 0 dead / 0 missing.
- Suite complète hors Docker : **5 888 passed, 0 failed** (5 skips,
  1 xpass connu, 8 tests Docker désélectionnés) ; suite Docker :
  **7 passed, 1 xfail**. `ruff check .` : **0**.
- Toutes les modifications sont locales ; aucun commit ni push n’a été fait.

## Implémenté

### File et balayage

- `free/qualification/` : domaine pur, score potentiel/mesuré, store SQLite
  WAL, source API NordVPN, préflight, sweep, runtime, découverte, watchdog,
  `QualificationScheduler` (découverte → watchdog → preflight → sweep),
  sondes station, réparation unique, cutover, workers parallèles, rotation et
  read models.
- File classée par score, quota, RTT relatif opérateur, charge et fraîcheur ;
  claim transactionnel avec lease et epoch.
- Catalogue : **5 395 hosts publics éligibles** sur 7 070 ; 1 538 dédiés et
  137 transports non supportés sont `skipped` sans consommer de session.
- Quota par IP observée : un `429` ne condamne jamais le hostname et le reset
  fournisseur est respecté.
- `catalog_claims.claim_next` préfère `qualification_candidates`, avec repli
  legacy.

### Transports et santé

- Cycle `wireguard → openvpn-udp → openvpn-tcp → wireguard` (round 2),
  curseur et round persistés, reprise après crash.
- `transport_health` par station et global : 200, 429, infra, latence p50,
  taux de succès, `degraded` après 100 tentatives sans 200.
- Diagnostic OpenVPN : `bind`, `auth`, `dns`, `probe`, `timeout`, `quota`.
- Doublons `(hostname, transport, round, epoch, station)` refusés.
- `fast_discovery` désactivé en mode `auto` : le tri-stack complet est
  toujours parcouru.

### Stations et rotation

- **Transports indépendants** : le bail WireGuard global (`transport_leases`)
  et `shared_wireguard_key` ont été supprimés. Chaque station peut utiliser
  WireGuard, OpenVPN UDP et OpenVPN TCP indépendamment ; seule l'unicité d'IP
  reste globale. `allocator.transports` = `auto` pour les six stations.
- Workers indépendants par station, concurrence bornée, standby et protection
  de la dernière station utilisable.
- Cutover make-before-break, mesure de coupure, rollback.
- Supervision et découverte schedulées, heartbeat persistant, réparation
  locale unique (aucun second réparateur).
- Pause globale opt-in et désactivée par défaut ; plus de `country_backoff`.

### Intégration qualification ↔ pool

- `CatalogClaims.claim_next` utilise d'abord les candidats qualifiés avec
  preuve (`success_count > 0`, `measured_score > 0` ou `quota` reseté), puis
  le catalogue legacy connu-200, puis seulement la file non mesurée. Un
  hostname jamais testé ne peut plus préempter un serveur déjà prouvé.
- `CatalogClaims.record` synchronise le statut de qualification : `200` →
  `usable`, `429` → `quota` avec reset, `no_egress`/`bind_error` → `pending`
  avec backoff, `auth`/`dead` terminaux. Les hostnames ne restent plus bloqués
  en `claimed` après un échec.
- Les quatre hostnames `claimed` du run interrompu ont été remis `pending`.

### Cutover de production

- `free/qualification/cutover_adapter.py` : `AdapterCutoverPorts` exécute
  prepare/switch/probe/rollback sur le binder/prober du registre.
- `free/qualification/apply.py` : `apply_rotation` construit le snapshot, le
  planificateur, le cutover et l'exécuteur réels.
- `POST /api/egress/qualification/rotate` avec `apply=true` exécute la
  rotation ; il reste refusé en 409 tant que `shadow_mode` est actif.
- `StationSnapshot.current_hostname` permet un rollback vers le hostname
  d'origine, pas vers une IP.
- Tests : binder/prober fakes, cutover `committed`, refus shadow, callback
  d'apply, snapshot hostname/quota.

### Démantèlement du pool

- `free/pool.py` converti en package `free/pool/` : `__init__.py` porte le
  legacy, les slices extraites vivent dans `selection.py` (rang egress,
  déduplication IP), `quota.py` (clé de bucket, rejet `socks5:`),
  `public_pool.py` (sous-réseau, sélection publique) et `rotation.py` (gardes
  ordonnées de `_launch_rotation`).
- Les chemins `__file__` du legacy sont ancrés sur `_ROOT` après le
  déplacement ; le logger reste `free.pool` pour l'attache dashboard.
- `RepairQueue` lit `OPENCODE_REPAIR_QUEUE_DB` : la suite de tests n'écrit plus
  de fausses demandes `10.0.0.x` dans le registre de production. Les trois
  demandes parasites ont été supprimées du registre live.

### Préexistants corrigés

- `free_model_map` : le comportement normal est la carte à **5 entrées**
  (`mimo-v2.5`, `glm-5.3-flash`, `deepseek-v4.1-flash`, `mimo-v2.6-flash`,
  `minimax-m3.1-flash`). Les tests attendaient encore les anciens mappings
  `glm-5.1`/`kimi-k2.6`/`deepseek-v4-flash` : ils sont désormais classés
  payants ; `mimo-v2.5` résout vers `mimo-v2.6-flash-free` (la cible config
  `deepseek-v4-flash-free` est remappée au défaut tant qu'elle n'est pas dans
  `models:`).
- `vpn_stack: wireguard` est le comportement normal du parc (T018) ;
  `test_rotation_config` attend désormais `wireguard` (`auto` reste le
  fallback d'une valeur absente/invalide).
- `egress_providers` : NordVPN est premier, WARP second, comportement normal
  de la feature 014 ; `test_plan_011_baseline` est aligné.
- `test_e2e_protocol_matrix` et `test_wire_compaction_phase1` injectent un
  mapping `glm-5.1` **test-only** pour exercer la conversion protocolaire,
  sans dépendre d'un mapping de production retiré.
- `docs/models-protocol.md` et `docs/_drift_manifest.json` décrivent la carte
  normale ; la clé de drift utilise `free_model_map` (les ids de modèle
  contiennent des points, incompatibles avec la syntaxe pointée).
- `test_public_proxies_security` scanne désormais tout `free/pool/*.py`.
- `test_plan_011_live` vérifie `usable_stations == active_stations_with_quota`
  (contrat quota-aware) au lieu du compteur brut `station_health`.
- Suite complète relancée après ces corrections (sortie
  `logs/full-suite-final5.log`).
- `ruff check .` : 29 anomalies préexistantes → **0** (imports, variables
  inutilisées, doublon de test, `yield from`, `dict()`), dont le doublon
  `test_max_completion_tokens_clamped_to_official_cap` qui masquait un test.

### Diagnostic live

- Le superviseur `fresh_chain` tournait depuis 19 h en `allocator mode=off` :
  le pool déléguait les réparations (`repair_owner: supervisor`) à un
  processus inactif. Passé en `observe`, il identifie correctement
  `broken=[2..6]` avec `quota=draining`/`daily-bucket-exhausted`.
- `CatalogClaims.claim_next` écarte désormais les candidates dont
  `last_egress_ip` est épuisée ; la chaîne vérifie aussi l'IP connue de la
  ligne avant tout bind. Les lignes legacy sans IP observée restent à couvrir.
- Le watchdog distingue désormais **quota** et panne : `model_usable_reason`
  (`daily-bucket-exhausted`, `draining`, `quota-blocked`, …) → cause `QUOTA`,
  état `DRAINING`, aucun incident ni job de réparation. Les faux incidents
  quota ne polluent plus le canary/SLO.
- `quota_per_ip` recalibré de **10 à 200** : le ledger live montre 176 succès
  avant le premier 429 et l'audit 004 estime ~200 req/5 h. `update_config`
  rafraîchit maintenant `QuotaProfile.default_quota` (auparavant seuls les
  endpoints SOCKS5 étaient mis à jour), ce qui évite de marquer les stations
  `draining` après 8 requêtes alors que le serveur en accepte davantage.
- La file de réparation live était polluée par des demandes de test
  (`quota-429`, IP `10.0.0.x`) ; nettoyées et isolées via
  `OPENCODE_REPAIR_QUEUE_DB`.
- Réparation live validée : station 2 `uk-se1.nordvpn.com` (130.195.218.110)
  et station 3 `al73.nordvpn.com` (87.120.102.39) ont produit un `200` free
  valide, pin `.env` et `WINNER`.
- Station 5 réparée en WireGuard : `uk2433.nordvpn.com` → `84.247.40.26`,
  `200` valide, pin `.env`, `model_usable=true`. Le registre enregistre
  désormais le transport réel du gagnant (plus de `'wireguard'` codé en dur)
  et `_registry_hosts(transport)` filtre les preuves 200 par transport.
- Un `200` efface désormais `blocked_until` du lease `(modèle, IP)` et
  `mark_station_ok` propage le succès au registre `ip_quota` ; sans cela une
  station réparée restait `quota-blocked`.
- Limite mesurée : plusieurs IP d'egress sont déjà épuisées ou partagées ;
  un transport forcé doit gagner sur `fast_discovery`, sinon la chaîne TCP
  lie WireGuard et bloque les autres stations par le lease de clé partagée.
- Le daemon de qualification est activé (`daemon_enabled: true`) et le
  superviseur le lance en shadow toutes les 900 s avec `--watchdog` : le
  `station_state` est rafraîchi (heartbeat `ok`, six stations sondées).
- Canary régénéré après activation : **0/6 utilisables**, 2 incidents ouverts,
  transports historiques partiels, 0 cutover — les six tunnels répondent mais
  le quota par IP est épuisé. Échec honnête conservé dans
  `logs/canary-014.json`.
- **Redémarrage du proxy effectué** (PID 39420) : la config normale est
  chargée (carte free à 5 entrées, NordVPN premier, `quota_per_ip=200`), les
  routes qualification répondent, le ledger a été purgé (2 809 buckets).
  Dernier relevé : 4/6 connectées, 0/6 `model_usable` sous trafic ; stations
  5/6 encore en erreur sur des pins OpenVPN morts. Canary post-restart :
  `logs/canary-014-after-restart.json` (0/6, échec honnête).
- **Bail WireGuard supprimé** : `transport_leases`, `shared_wireguard_key` et
  les méthodes `acquire/renew/release_transport` retirés. Preuve live :
  stations 5 (`uk2433`, 84.247.40.26) et 6 (`al41`, 87.120.102.87) servent
  **simultanément en WireGuard** ; 6/6 connectées, 2/6 `model_usable` (les
  quatre autres sont en quota épuisé sous trafic). `allocator.transports` est
  passé à `auto` pour les six stations ; constitution VI mise à jour (1.2.1).

- Routeurs extraits de `dashboard/api.py` : qualification, catalogue egress
  (lecture + mutations), NordVPN, stations, statut free.
- `GET /api/egress/qualification/quota` : ledger par IP, reset, 429.
- `GET /api/egress/qualification/metrics` : JSON ou Prometheus.
- Rétention 7 jours sur tentatives, cutovers, incidents et découvertes ;
  checkpoint WAL `TRUNCATE`.

## Preuves

- `scripts/bench_qualification.py` : upsert ~0,011 ms/item, score p95
  ~0,005 ms, claim p95 ~2,7 ms.
- `scripts/soak_qualification.py` : 10 s, ~4 750 claims, pic ~0,22 MB,
  DB ~4,5 MB, 0 violation ; profil 60 s persistant : 24 850 claims,
  pic 0,217 MB, DB 4,523 MB, 0 violation.
- `scripts/module_size_audit.py` : 0 violation sur 712 fichiers.
- `scripts/dependency_graph.py` : 0 cycle.

## Reste

- Soak 24 h réel et canary six stations avec preuves live.
- `POST /api/egress/qualification/rotate` (le GET quota est fait).
- Slices de démantèlement S1–S8 et S11–S13 : manager, pool, fresh_chain,
  opencode.
- Checklist de release finale et clôture de la feature.

## P0 boucle de contrôle (2026-11-10)

- Admission **fail-closed** : `ModelSlotTimeout` dans `free/pool`,
  `model_slot_wait_s=12`, `max_inflight_per_model_ip=4`,
  `model_slot_fail_closed=true`, `Retry-After=2`. Les quatre call-sites
  (hedge, stream, séquentiel, SystemOne) renvoient 503 + Retry-After ou un
  refus SSE immédiat, sans rotation ni paid.
- Fuite de jeton stream corrigée : `_open_free_stream` ferme le wrapper qui
  porte le jeton (modèle, IP), plus la réponse brute seule.
- Garde ledger **au dispatch** dans les trois boucles ; `_legacy_candidate`
  exclut les hostnames dont l'egress est dans `forbidden_ips`.
- Les demandes de réparation dont l'IP ne correspond plus à l'IP live sont
  supprimées : plus de re-rotation d'une station déjà réparée.
- `allocator.mode=enforce`, `max_parallel_repairs=4`,
  `rebind_limit_per_min=8`. Preuve live : `slot_wait limit=4, timeouts=0,
  rejections=0` ; s2 (186.247.44.119) et s5 (186.247.44.131) réparées sur
  des 200 réels pendant la session.
- Filtre `claims.egress_ip` sur la file de qualification et le catalogue
  legacy : une ligne legacy sans `last_egress_ip` ne peut plus ramener une IP
  brûlée (al73/al74/uk2433). `refresh` : tranche 25, cooldown 120 s.
- Confirmation en deux probes avant de pinner une IP : un 200 isolé sur une IP
  partagée n'est pas une preuve de marge (budget de rotation churn).
- Preuve finale : **6/6 `model_usable`** simultanément, six IP distinctes,
  toutes en WireGuard ; superviseur `broken=[] running=[]`, `slot_wait`
  `limit=4, timeouts=0, rejections=0` ; suite complète **5 888 passed**.
- `egress_catalog.qualification.daemon_enabled=true`,
  `daemon_max_hosts=1` : tick de qualification borné <1 s, plus de blocage
  180 s du superviseur ; le résumé reste frais pour le canary.
- **Bridge chaîne → qualification** (`free/qualification/chain_bridge.py`) :
  les tentatives réelles de `fresh_chain` alimentent `qualification_attempts`
  et `transport_health` (200/429/no-egress/bind_error), sans modifier l'état
  des candidats. Test : `tests/test_qualification_chain_bridge.py`.
- **Garde de réserve** (`_repair_holds`) : refresh proactif borné à
  `usable - active > 3`, réparation d'une station down protège la dernière
  station debout ; `max_parallel_repairs=2`. Quatre refreshs simultanés
  avaient produit un épisode 0/6 le 2026-11-10 22:32Z.
- **Garde AUTH** (`_auth_pause_remaining`) : pendant une pause AUTH, une
  seule chaîne est autorisée (probe unique), et `auth_fail_30min > 0` impose
  un espacement de 60 s entre les démarrages ; `rebind_limit_per_min=3`.
- **Capacité mesurée** : 429 après 20–40 requêtes par IP partagée, limite de
  sessions NordVPN basse, ~1 bind utile/1–2 min. Le 6/6 stable exige T076
  (standby/second tunnel) ou une source d'egress supplémentaire.

## Qualifieurs dédiés (2026-11-11)

- **Goulot identifié** : le daemon de qualification restait en shadow et ses
  époques duraient 7 ms (`epoch-20261111-055456`), donc 0 test réel ; avec
  `daemon_max_hosts=1`/900 s, 4 hosts/h au mieux. Seules les chaînes de
  réparation testaient : 284 tentatives/24 h, p50 68,4 s, 3 hosts utiles/h.
- **Control API réparé** : la clé est `VPN_CONTROL_API_KEY` (pas
  `GLUETUN_CONTROL_SERVER_API_KEY`) et le conteneur est `opencode-vpn-{N}`
  (pas `vpn-gluetun-{N}`). `build_registry_station_adapter` accepte
  `force_control`, `probe_models`, `probe_timeout_s` et un tuning
  settle/egress/poll.
- **`free/qualification/qualifiers.py`** : slots 7–10 via les services Compose
  existants (profil `multi-station`), WireGuard seul, une époque live par slot
  (`qualifier-N-*`), une tentative par host, sonde mono-modèle 8 s. Docker et
  epochs sont injectables ; les erreurs Docker sont capturées par slot.
- **Pilotage GUI** : carte « Qualifieurs dédiés » (activer + nombre + Appliquer
  + Tout arrêter + tableau des slots). `GET/POST
  /api/egress/qualification/qualifiers` ; si le manager n'est pas attaché, le
  POST persiste et répond `restart_required: true`.
- **Config** : `shadow_mode: false`, `daemon_shadow: true`,
  `max_hosts: 200`, `max_attempts_per_host_per_epoch: 1`,
  `transport_order: [wireguard]`, `transport_retry_rounds: 1`,
  `daemon_interval_s: 60`, `daemon_max_hosts: 40`, bloc
  `qualification.qualifiers` (`max_count: 4`, `recommended_max: 2`,
  `station_base: 7`).
- **Débit attendu** : 1 slot ≈ 220–280 hosts/h, 2 ≈ 440–560, plafond pratique
  ~500/h (sessions NordVPN). Au-delà de 2, risque de cascade `AUTH_FAILED` sur
  les six stations.
- **Isolation** : le tick superviseur est forcé en shadow dès qu'un qualifieur
  est actif (`_qualified_shadow_mode`) ; les conteneurs 7–10 sont stoppés au
  shutdown et par « Tout arrêter ».
- Tests : `tests/test_qualification_qualifiers.py` (clamping, reconcile,
  persistance, époque live, override transport) et 7 tests de routes dans
  `tests/test_qualification_api.py` ; contrat UI étendu.

### Retour terrain et robustesse (2026-11-11)

- **Cause du `no_egress` massif** : les premiers candidats clivables étaient
  des serveurs **Dedicated IP** (`de694`, `de689`, `uk2916`, `at156`, `tr77`).
  Le compte standard les fait passer `running` mais le handshake WireGuard
  reste muet (`rx_packets=0`, `public_ip=""`) ; ce sont des faux négatifs
  d'infrastructure, pas des hosts morts. Une ré-ingestion de
  `logs/nordvpn_api.json` a marqué **1 538** lignes `skipped/dedicated` et
  remis **0** Dedicated IP en `pending`.
- **Upsert des skips durs** : `upsert_candidates` n'écrasait pas le statut
  existant, donc un host déjà `pending` restait clivable à vie. Il applique
  maintenant `skipped` + `skipped_reason` pour `dedicated`, `denylisted`,
  `unsupported-transport` et `removed-from-catalog`, tout en préservant
  `operator-paused` et les statuts mesurés. Tests :
  `test_reingest_marks_an_existing_pending_host_skipped`,
  `test_reingest_preserves_an_operator_pause`.
- **Époques annulées** : `run_epoch` intercepte `asyncio.CancelledError`,
  relâche le host en cours (`claimed`/`probing` → `pending`) et ferme
  l'époque en `cancelled`. Le manager ferme les orphelines au tick suivant
  (`close_running_epochs`). Tests :
  `test_cancelled_epoch_is_closed_and_the_claim_released`,
  `test_close_running_epochs_is_namespaced_and_keeps_the_live_one`.
- **Compteurs par époque** : `close_epoch` accepte désormais les compteurs du
  run ; `tested`/`usable` ne sont plus les totaux de la flotte. Le
  `last_promoted=0` observé n'était donc pas un bug : l'époque concernée
  n'avait produit aucun 200 (20 tentatives `no_egress`).
- **Validation live (count=1)** : 3 époques réelles ont testé 60/80/100
  hosts ; la réserve globale s'est maintenue autour de 8–12 hosts `usable`
  (~10 au dernier contrôle), avec des 200 réels dès que le hostname est
  standard (`uk2433`, `fj3`). Les 429 restants sont des quotas **par IP**,
  pas un épuisement du compte. Les champs `usable` des époques antérieures
  reflétaient le total de flotte : ne pas les relire comme des promotions
  par époque.
