# Tasks — Fiabilité des requêtes free

**Feature**: `012-request-reliability`
**Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)
**Audit**: [docs/AUDIT_REQUETES_2026-11-08.md](../../docs/AUDIT_REQUETES_2026-11-08.md)

## Phase 1 — Setup

- [x] T001 Créer `specs/012-request-reliability/` avec spec, plan, research,
  quickstart et contrat.
- [x] T002 Relever les mesures de référence dans
  `docs/AUDIT_REQUETES_2026-11-08.md`.
- [x] T003 Vérifier que la gate actuelle est verte
  (`pytest -k "not docker"` → 100 %, EXIT=0).

## Phase 2 — Fondation : modes free

- [x] T004 Introduire `_FREE_POOL_MODES = ("vpn", "socks5", "auto_egress")`
  dans `opencode.py`.
- [x] T005 Utiliser `_FREE_POOL_MODES` dans `_try_free_model_first`
  (`opencode.py:15497`, `15837`, `15854`, `15868`).
- [x] T006 Utiliser `_FREE_POOL_MODES` dans `_systemone_free_via_pool`
  (`opencode.py:27758`, `28136`).
- [x] T007 Ajouter le test SystemOne `auto_egress` dans
  `tests/test_systemone.py`.
- [x] T008 Ajouter le test « pool vide → 503 » et « pool actif → pas de 503 »
  dans `tests/test_systemone.py`.

## Phase 3 — US1 : SystemOne/auto_egress (P1)

- [x] T009 Vérifier en live `POST /v1/systemone` avec `jev-1.13-free` :
  jamais `503 pool indisponible`.
- [x] T010 Vérifier que le smoke test de `192.168.31.101` obtient `200`/`429`
  dans `logs/requests.db`.

## Phase 4 — US2 : canari IPv4 (P1)

- [x] T011 Passer `WARP 2..6` IPv4 à `enabled: false` dans `config.yaml`.
- [x] T012 Mettre à jour `tests/test_plan_011_baseline.py` : 12 lignes
  déclarées, 7 actives (6 IPv6 + 1 canari IPv4).
- [x] T013 Vérifier `/api/vpn/socks5` : 7 endpoints actifs.
- [x] T014 Vérifier qu'aucune rotation n'est déclenchée par un endpoint IPv4.

## Phase 5 — US3 : NordVPN (P1)

- [x] T015 Vérifier `NORDVPN_TOKEN` avec
  `python scripts/refresh_nord_credentials.py --check` → 200.
- [x] T016 Appliquer les credentials de service
  (`python scripts/refresh_nord_credentials.py --apply`).
- [x] T017 Parker les stations Docker (`disabled_stations: [1..6]`) pendant la
  validation.
- [x] T018 Valider une station (egress public, `AUTH_OK`) puis réactiver
  progressivement 1 → 6.
- [x] T019 Vérifier `AUTH_FAILED` < 50/jour et `/api/vpn-status` cohérent.

## Phase 6 — US4 : quota et rotation (P2)

- [x] T020 Parser `FreeUsageLimitError` + `Retry-After` dans
  `free/pool.py`/`free/quota_ledger.py`.
- [x] T021 Bloquer `(modèle, IP)` jusqu'au reset ; exclure du gel anti-churn.
- [x] T022 Brancher `station_max_rotations_per_hour=3` et l'intervalle
  600–900 s dans `WarpRotator`.
- [x] T023 Ne rotationner que sur quota journalier réel.
- [x] T024 Ajouter les tests de distinction quota/transitoire.

## Phase 7 — US5 : slot wait (P2)

- [x] T025 Passer `model_slot_wait_s` à 0,25 s dans `config.yaml`.
- [x] T026 Ajouter une métrique de distribution d'attente.
- [ ] T027 Vérifier le TTFB p95 < 35 s après 24 h.

## Phase 8 — Observabilité (P2)

- [x] T028 Ajouter `client_request_id`, `attempt_no`, `final_status` à
  `requests` (migration SQLite).
- [x] T029 Écrire `rotation_jobs.finished_at/result/reason`.
- [x] T030 Réparer `/api/free-health` (`served`/`unproven`).
- [x] T031 Ajouter les alertes 429/IP morte/rotation/slot/AUTH.

## Phase 9 — Vérification finale

- [x] T032 `python scripts/config_audit.py --strict` → 0 dead/0 missing.
- [x] T033 `pytest -k "not docker"` → EXIT=0.
- [x] T034 Smoke live SystemOne + WARP + `/api/vpn-status`.
- [ ] T035 Comparer les métriques 24 h aux seuils SC-001..SC-007.

## Dependencies

- T004–T008 avant T009/T010.
- T011–T014 indépendants de T015–T019.
- T020–T024 après T015–T019 (sinon la rotation masque le diagnostic auth).
- T028–T031 après T020–T027.

## Parallel

- T015–T019 (NordVPN) et T011–T014 (canari IPv4) peuvent avancer en parallèle.
- T028–T031 peuvent être parallélisés après les correctifs runtime.


## Notes d'implémentation (2026-11-08)

- **Découverte P0.2** : le pool `FreeIPPool.enabled` et `on_request` étaient
  liés à `_vpn.enabled` (Docker). Parquer les stations NordVPN désactivait donc
  aussi WARP. Corrigé : `enabled` = Docker **ou** endpoints SOCKS5 actifs, et
  `on_request` utilise `self.enabled`.
- **Découverte P0.2 bis** : `get_status().enabled` lisait aussi
  `_vpn.enabled` ; corrigé.
- **NordVPN** : token 200, credentials de service identiques et valides ;
  un conteneur Gluetun isolé se connecte (`Initialization Sequence Completed`).
  Les `AUTH_FAILED` venaient de la limite de sessions / des processus
  `fresh_chain` qui relançaient les stations. Les six stations sont parkées et
  les chasseurs `fresh_chain` arrêtés ; `AUTH_FAILED` = 0 sur la fenêtre
  d'observation.
- **SystemOne live** : après correctifs, `/v1/systemone` atteint WARP et rend
  `200` (puis `400 api_usage_error` selon le payload amont) — plus aucun
  `503 pool indisponible`.
- **Canari IPv4** : 12 lignes WARP déclarées, 7 actives (6 IPv6 + `WARP 1`).
- **Rotation jobs finalisés (T029)** : `rotation_jobs` porte `finished_at` et
  `result`/`reason` réels (`ok`/`failed`) au lieu de rester `started`.
  Migration SQLite additive, test dans `tests/test_rotation_budget.py`.
- **Free-health** : `served` contient désormais `mimo-v2.6-flash-free` après
  les premiers succès WARP ; les modèles sans preuve restent `unproven`.
- **T028** : `requests` porte `client_request_id`, `attempt_no`,
  `final_status`. Le middleware de retry pose un id stable et incrémente
  `attempt_no` ; `final_status` est le statut effectif de la tentative
  (la ligne finale d'un client est celle qui a le plus grand `attempt_no`).
  Migration additive + tests DB/contrat/retry à jour.
- **T026/T031** : `slot_wait` (p50/p95/max/timeouts) est exposé dans
  `/api/free-health` et `vpn_alerts` alerte sur `p95 ≥ 1 s` et
  `timeouts ≥ 100`. Tests dans `tests/test_vpn_alerts_capacity.py`.
- **T018 (partiel)** : station 1 testée seule, connexion OpenVPN OK et IP
  `202.49.186.166` obtenue, mais la sonde d'egress public échoue puis le
  serveur suivant rend `AUTH_FAILED`. Station reparkée ; la réactivation 1→6
  reste ouverte.
- **T018 (WireGuard)** : avec `VPN_TYPE_STATION1=wireguard` et
  `SERVER_HOSTNAMES_STATION1=al46.nordvpn.com`, un compose manuel donne
  `Public IP 87.120.102.220 (Albania)` et un conteneur **healthy**. Le manager
  réordonne ensuite les serveurs et réintroduit des `AUTH_FAILED` ; les six
  stations sont reparkées. Le pin WG par station est conservé dans `.env`
  pour la reprise.
- **T018 termin?** : les six stations sont pass?es une par une en WireGuard via
  `/api/vpn/station/{sid}/stack`, avec les pins `SERVER_HOSTNAMES_STATIONn`
  (al46/al73/al37/al36/al41/al45), puis `/probe`. R?sultat : **6/6 connected,
  client_usable=true**, egress 87.120.102.x, `AUTH_FAILED`=0, free chat 200.
- **Parkage durable** : `ip_registry.allocator.mode: off` empêche
  l'auto-démarrage du superviseur `fresh_chain` ; superviseur + six chasseurs
  arrêtés, aucun conteneur VPN en marche, `AUTH_FAILED` = 0.

- **P1 quota/rotation (2026-11-08)** : le ledger distinguait déjà
  `FreeUsageLimitError` (scope trial-exhausted) d'un 429 transitoire. Ajouté :
  budget `station_max_rotations_per_hour: 3`, intervalle
  `station_min_rotation_interval_s: 600`, slot wait `0.25 s`. Le rotator lit
  ces deux clés au démarrage.

- **T027/T035 — outil et snapshot** : `scripts/request_reliability_report.py`
  calcule les seuils SC-001…SC-007 en lecture seule. Fenêtre 2 h après la
  remise en route : 161 tentatives / 161 succès (100 %), 157 requêtes client
  finales / 157 succès (100 %), 0 IP morte, 7 rotations OK, **TTFB p95
  17,9 s** (tous les seuils OK). La durée totale p95 (40,1 s) est portée par
  les grosses sorties (5 000+ tokens), pas par le routage. Le run 24 h reste
  à faire.

- **Veille 24 h automatisée** : `scripts/request_reliability_watch.py` tourne
  en tâche de fond (`pythonw`, PID 22256) et réécrit
  `logs/reliability_24h.json` toutes les heures. La fenêtre inclut encore les
  incidents d'avant le correctif ; elle deviendra propre 24 h après la remise
  en route (2026-11-10 ~08:20).

- **Validation burst client** : 10 requêtes non-stream
  `deepseek-v4.1-flash-free` via `127.0.0.1:4000` → **10/10 HTTP 200**
  (première 3,4 s, suivantes 2–27 ms grâce au cache amont).

- **Fraîcheur santé** : `station_health.max_effective_age_s` 180 → 900.
  Avec le superviseur `fresh_chain` arrêté, les sondes ne rafraîchissaient
  plus `last_ok` toutes les 180 s : les six stations passaient
  `egress-stale` après ~3 min d'inactivité. 900 s couvre l'intervalle de
  sonde et évite la bascule WARP parasite ; un vrai échec reste détecté par
  la requête cliente et le bad-mark.

- **Fenêtre propre T027/T035** : `request_reliability_report.py` accepte
  `--clean-start` et la veille utilise `--clean-start 2026-11-09T08:20:00Z`
  (fin de la remise en route des six stations WG). Le JSON expose
  `elapsed_hours` et `full_window_ready` ; le verdict ne porte plus sur les
  incidents d'avant correctif. Fenêtre complète attendue le
  **2026-11-10 ~08:20 UTC**.

- **T018bis — IP d'egress fraîche** : le test direct du chemin free via le
  SOCKS de chaque station a montré que les IP NordVPN connues rendent
  `FreeUsageLimitError` (quota par IP épuisé), alors que l'hôte direct et
  WARP IPv6 rendent 200. Une reconnexion de al46 a fourni
  `87.120.102.213`, qui répond **200**. Procédure de remise en état :
  `force-recreate` du conteneur jusqu'à obtenir une IP dont le test
  d'inférence free rend 200, puis laisser la sonde de santé et le ledger
  du pool constater le 200.

- **Objectif six stations** : test direct du chemin free via le SOCKS de
  `warp-1`…`warp-6` → **6/6 HTTP 200** (une requête réelle par endpoint,
  egress IPv6 `2a09:bac5:...`). Les six stations WARP sont client-utilisables ;
  côté NordVPN, l'IP d'egress doit être recyclée quand elle rend un 429.

- **T018 — nuance importante** : le `client_usable=true` des six stations
  NordVPN après `/stack` était une preuve d'egress, pas d'inférence. Test
  réel du chemin free via leur SOCKS :
  - station 1 (al46, IP recyclée `87.120.102.213`) → **200** ;
  - stations 2-6 → **429 `FreeUsageLimitError`** sur leurs IP actuelles,
    malgré un tunnel sain.
  Les six stations **WARP** sont 6/6 à 200 (note « Objectif six stations »).
  La couche NordVPN reste un appoint : son IP d'egress doit être recyclée
  jusqu'à un 200 avant d'être comptée comme capacité free.

- **Validation streaming** : `deepseek-v4.1-flash-free`, `stream=true` via le
  proxy → **HTTP 200**, `text/event-stream`, premier chunk en **1,98 s**,
  `[DONE]` reçu, contenu `pong`.
- **Re-test WARP après rotation** : les six endpoints `warp-1`…`warp-6`
  répondent de nouveau **6/6 HTTP 200** avec de nouvelles IP d'egress
  (`2a09:bac5:...`). La rotation WARP continue de fournir des IP saines.
