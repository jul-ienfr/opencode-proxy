# Tasks: Rotation catalogue mondial et réparation automatique

**Feature** : `002-vpn-catalog-rotation`  
**Plan** : `specs/002-vpn-catalog-rotation/plan.md`  
**Spec** : `specs/002-vpn-catalog-rotation/spec.md`  
**Statut** : Phase 0 partielle et Phase 1 en cours. Voir
`implementation-log.md` pour les tâches faites/partielles. Ce fichier reste la
liste de travail de référence.

**Légende** :

- `[P]` : peut être fait en parallèle d'une autre tâche.
- `[ ]` : à faire.
- `[x]` : fait.
- `[~]` : partiel (voir `implementation-log.md`).
- Les chemins sont indicatifs ; ils seront confirmés en revue.

---

## Phase 0 — Garde-fous et baseline

- [x] **T001** — Mesurer la baseline SLO sur 24 h et 7 j : `routable=0`,
      `routable<=2`, `routable=6`, `healthy`, 429 par IP, bind success par
      station. **Fichiers** : `scripts/station_slo_watch.py`,
      `logs/station_slo.jsonl`, `logs/ip_registry.db`. **Critère** : un
      rapport chiffré comparable avant/après.
- [x] **T002** — Écrire le test de régression « aucun littéral de hostname,
      pays ou préfixe IP dans les modules de sélection ». **Fichiers** :
      `tests/test_no_hardcoded_geo.py`, `scripts/fresh_chain.py`,
      `scripts/fresh_chain_supervisor.py`, `free/ip_registry/*`. **Critère** :
      le test échoue sur `PROVEN_HOSTS` et `_is_gold_ip` actuels.
- [x] **T003** — Écrire le test de cohérence `config.yaml` / `.env` /
      `docker-compose.yml` / conteneurs (type VPN, protocole, hostname,
      station_count, clé). **Fichiers** : `tests/test_env_staleness_guard.py`,
      `scripts/make_credentials_env.py`. **Critère** : la divergence
      `VPN_TYPE_STATION5` est détectée.
- [~] **T004** — Écrire les tests de non-régression des bugs identifiés :
      station 6 `be160` absent du manifeste, lease 15 s < bind 60 s, station 7
      hors scope, 3 286 lignes sans hostname. **Critère** : chaque bug a un
      test rouge.
- [~] **T005** — Snapshot DB + export des catalogues API/Gluetun avant toute
      modification. **Fichiers** : `logs/ip_registry.db`,
      `logs/nordvpn_api.json`, `logs/gluetun_nordvpn_servers.json`.
      **Critère** : restauration possible.

### Déjà exécuté pendant l'audit (pas une implémentation)

- [x] **T006** — Comparaison exhaustive API vs Gluetun : 6 948 API, 9 787
      Gluetun, 0 endpoint/pays divergent, 2 839 Gluetun-only. Résultats :
      `audit.md` §2.8.
- [x] **T007** — Tests de tunnels isolés : UK OK, ZA/AR/JP/AU en échec au
      moment du test ; puis test de **parallélisme avec la clé partagée** :
      UK + Albanie simultanés, deux IP distinctes. Résultats : `audit.md`
      §2.3 et §2.11.
- [x] **T008** — Inventaire des IP réellement disponibles : 629 IP avec 200
      sur 7 jours, 249 hostnames. Résultats : `audit.md` §2.10.
- [ ] **T009** — Mesurer le budget exact de sessions simultanées avec la clé
      WireGuard partagée (6, 7, 8, 10 ?) sans perturber la production.
      **Critère** : limite exposée dans le GUI ; stations au-delà en
      `STANDBY`.

**Gate 0** : baseline et tests rouges validés.

---

## Phase 1 — Quick wins (arrêter l'hémorragie)

- [x] **T010** — Supprimer `PROVEN_HOSTS` et la priorité `proven` du chemin
      de sélection. **Fichiers** : `scripts/fresh_chain.py:42-52,728-740`.
      **Critère** : aucun hostname albanais prioritaire.
- [x] **T011** — Supprimer `_is_gold_ip` et la protection `87.120.102.*`.
      **Fichiers** : `scripts/fresh_chain_supervisor.py:696-699,756-787`.
      **Critère** : plus aucune IP privilégiée par préfixe.
- [x] **T012** — Corriger le classement par qualité observée, API et Gluetun
      mélangés : la source ne départage qu'à score égal, pas de pénalité de
      source. **Fichiers** : `scripts/fresh_chain.py:633-858`, surtout
      `add()` et `_base_key()`. **Critère** : un host Gluetun-only à 90 % de
      200 passe avant un host API à 50 %.
- [x] **T013** — Vérifier le manifeste du conteneur cible avant chaque bind ;
      synchroniser, re-vérifier, puis marquer `manifest_missing`.
      **Fichiers** : `scripts/fresh_chain.py:287-333,1436-1455`,
      `free/ip_registry/adapters/compose_binder.py`. **Critère** : plus de
      crash loop `hostname not valid`.
- [x] **T014** — Porter le lease de claim à 300 s et le renouveler pendant le
      bind. **Fichiers** : `scripts/fresh_chain.py:1324-1338`,
      `free/catalog_claims.py:123-210`. **Critère** : aucun hostname repris
      par une autre station pendant un bind.
- [x] **T015** — Désactiver `ip_rotation.station_repair.enabled` (le
      superviseur devient le seul propriétaire). **Fichiers** :
      `config.yaml:292-315`, `scripts/fresh_chain_supervisor.py:392-403`.
      **Critère** : le manager ne recrée plus une station en réparation.
- [x] **T016** — Arrêter et retirer la station 7 (hors `station_count`), ou la
      déclarer explicitement `STANDBY`. **Fichiers** :
      `docker-compose.yml:471-548`, `config.yaml:286`, `.env`.
      **Critère** : exactement 6 conteneurs actifs.
- [x] **T017** — Archiver/purger les 3 286 lignes `hostname=''` du registre
      après sauvegarde ; ajouter `hostname != ''` à `eligible()`.
      **Fichiers** : `free/ip_registry/sqlite_registry.py:538-579`,
      `logs/ip_registry.db`. **Critère** : plus de candidat non bindable.
- [~] **T018** — Sur 429 quota, ne plus `return` quand la station est pinnée :
      publier un besoin de réparation dans la file du superviseur (ne pas
      appeler `connect_next()` directement). **Fichiers** :
      `free/pool.py:3982-3990,4459`. **Critère** : un 429 pinné lance un job,
      et le pool n'exécute plus de rotation concurrente.
- [x] **T019** — Exposer `client_usable` par station et par modèle dans
      `/api/vpn-status` et le dashboard. **Fichiers** : `dashboard/api.py`,
      `free/pool.py`. **Critère** : `connected` ≠ `client_usable`.
- [ ] **T020** — Test de charge : 20 candidats, mesurer bind success et temps
      moyen avant/après T010–T019. **Critère** : amélioration mesurable.
- [x] **T021** — Retirer le périmètre pays de la sélection : `server_countries`
      vide = tous les pays ; une allowlist GUI reste optionnelle. Aucune
      porte pays par défaut. **Fichiers** : `config.yaml:254-268`,
      `vpn/manager.py:5976-6040`. **Critère** : un host de n'importe quel pays
      peut être sélectionné.
- [x] **T022** — Au boot, vérifier les stations existantes, conserver celles
      qui répondent 200 avec contenu, compléter les manquantes avec les
      meilleures IP, arrêter les extras. **Fichiers** :
      `scripts/fresh_chain_supervisor.py`, `free/pool.py`,
      `vpn/manager.py:2090-2137`. **Critère** : aucune station utilisable
      recréée au démarrage.
- [x] **T023** — Chaîne de fallback IP : SOCKS5h → SOCKS5 → HTTP proxy →
      API contrôle Gluetun `/v1/publicip/ip` → `docker exec`. Si tout échoue
      mais probe 200 : `ip_unknown`, station conservée. **Fichiers** :
      `free/ip_registry/adapters/egress.py`,
      `free/ip_registry/adapters/compose_binder.py`. **Critère** : site
      principal KO → station toujours utilisable.
- [x] **T024** — Chaîne de fallback DNS : DoH Cloudflare → DoH Google → DoT
      Quad9 → plain 1.1.1.1 → plain 8.8.8.8 → resolver hôte, avec
      journalisation. **Fichiers** : `docker-compose.yml`,
      `vpn/manager.py`, `free/ip_registry/adapters/egress.py`.
      **Critère** : resolver principal KO → fallback, pas `no-egress`.
- [~] **T025** — Contrat GUI : manifeste `option → clé → consommateur →
      effet → test` ; `config_audit.py --strict` ; test par option.
      **Fichiers** : `config/gui_contract.py`,
      `scripts/config_audit.py`, `tests/test_gui_contract.py`.
      **Critère** : 0 option inerte.
- [x] **T026** — Transport auto tri-stack : WireGuard → OpenVPN UDP →
      OpenVPN TCP, par serveur, boucle, budget AUTH ; supprimer les slots
      fixes `_MIXED_SLOTS`. **Fichiers** : `vpn/manager.py:6951-7157`,
      `scripts/fresh_chain.py`,
      `free/ip_registry/adapters/compose_binder.py`. **Critère** : serveur
      WG KO/OV TCP OK → bind OV TCP ; trois KO → serveur suivant ; chaque
      station peut essayer les trois.

**Gate 1** : plus de hardcode Albania, plus de crash loop manifeste, plus de
double réparation, 429 pris en charge, fallbacks IP/DNS opérationnels,
tri-stack auto, contrat GUI vert.

---

## Phase 2 — Sélecteur catalogue

- [x] **T030** — Créer le schéma `catalog_server`, `endpoint_test`,
      `egress_ip`, `lease`, `warm_spare`, `sweep_cursor`, `repair_job`,
      `observation`, `incident`. **Fichiers** :
      `free/ip_registry/sqlite.py`, `free/ip_registry/sqlite_registry.py`.
      **Critère** : migration idempotente, index en place.
- [x] **T031** — Implémenter `CatalogService` : fusion API + Gluetun,
      provenance, fraîcheur, endpoint, clé WG. **Fichiers** :
      `free/ip_registry/catalog.py`, `free/ip_registry/sources/*`.
      **Critère** : 9 787 hosts avec source et dates.
- [x] **T032** — Implémenter `Selector` : score composite (IP connue et
      hostname non bindé), tous pays, unicité d'IP, exploration de découverte.
      **Fichiers** : `free/ip_registry/scoring.py`, `allocator.py`.
      **Critère** : la meilleure IP disponible est prise ; deux stations n'ont
      jamais la même IP ; aucun pays n'est interdit par défaut.
- [~] **T033** — Implémenter `WarmPool` : TTL, promotion atomique,
      réapprovisionnement. **Fichiers** :
      `free/ip_registry/sqlite_registry.py`, `free/pool.py`. **Critère** :
      pool ≥ 6 en régime nominal.
- [~] **T034** — Implémenter `RepairOrchestrator` : états station, un job par
      station, jobs parallèles entre stations, leases, réconciliation au
      boot. **Fichiers** : `scripts/fresh_chain_supervisor.py`.
      **Critère** : deux 429 simultanés → deux jobs parallèles, aucun gel
      global.
- [x] **T035** — Gérer les 429 par modèle et par IP (`daily`, `transient`,
      `rate_limit`, `unknown`) avec `reset_at`. **Fichiers** :
      `free/pool.py`, `free/quota_ledger.py`, `free/ip_quota.py`.
      **Critère** : daily → `EXHAUSTED` jusqu'au reset ; transient → retry.
- [x] **T036** — Implémenter `SweepCursor` persistant et la reprise au boot.
      **Fichiers** : `free/ip_registry/sqlite_registry.py`,
      `scripts/fresh_chain_supervisor.py`. **Critère** : redémarrage sans
      reset à zéro.
- [x] **T037** — Tests unitaires : score composite, unicité d'IP, 429,
      leases, warm pool, sweep, reset. **Fichiers** :
      `tests/test_ip_registry_*`, `tests/test_station_allocator_supervisor.py`.
      **Critère** : couverture des branches critiques.
- [x] **T038** — Test d'intégration binder/probe factices : 100 hosts,
      vérifier l'ordre par score, l'unicité d'IP et le temps de remplacement.
      **Fichiers** : `tests/test_free_multi_attempt.py`. **Critère** : 429 →
      remplaçante < 60 s simulée ; deux 429 → deux jobs parallèles.

**Gate 2** : sélection mondiale, pool chaud, réparation < 60 s, zéro hardcode.

---

## Phase 2.5 — Architecture et base de données

- [x] **T080** — Écrire les ADR et la carte des modules (domaine,
      application, ports, adapters, interfaces) ; définir la règle d’import.
      **Fichiers** : `docs/adr/`, `architecture.md`. **Critère** : ADR
      validés, test d’import rouge sur le code actuel.
- [x] **T081** — Créer `registry.db` v1 : `servers`, `server_transports`,
      `endpoints`, `egress_ips`, `ip_server_map`, `observations`,
      `quota_buckets`, `leases`, `warm_pool`, `sweep_cursor`, `repair_jobs`,
      `fallback_events`, `incidents`, `station_state`, `config_snapshots`,
      `score_snapshots`, `schema_migrations`. **Fichiers** :
      `rotation/infrastructure/db/schema.sql`, migrations.
      **Critère** : `PRAGMA integrity_check=ok`, index et contraintes.
- [x] **T082** — Implémenter les repositories + unit of work ; clock/rng
      injectés. **Fichiers** : `rotation/infrastructure/db/*`.
      **Critère** : tests repository sur DB temporaire, WAL, concurrence.
- [x] **T083** — Migrer `ip_registry.db`, `catalog_claims.db` et les JSON
      legacy vers `registry.db` ; archiver les lignes `hostname=''`.
      **Fichiers** : `rotation/infrastructure/db/migrate_legacy.py`.
      **Critère** : 0 perte utile, 0 hostname sans provenance, rollback testé.
- [x] **T084** — Séparer `telemetry.db` (requêtes) et `registry.db`
      (décisions) ; rétention et agrégats. **Fichiers** :
      `rotation/infrastructure/db/retention.py`.
      **Critère** : `registry.db` < 250 Mo, purge quotidienne.
- [x] **T085** — Backup `VACUUM INTO`, `integrity_check` quotidien,
      restauration testée. **Fichiers** : `rotation/infrastructure/db/backup.py`.
      **Critère** : backup restauré dans un test, 0 corruption.
- [x] **T086** — Matérialiser `score_snapshots` (IP/host/transport) depuis
      `observations` ; recalcul périodique. **Critère** : score déterministe,
      pas de calcul lourd par requête.
- [x] **T087** — Test d’import : domaine sans I/O, pas de cycle, application
      sans adapter concret. **Fichiers** : `tests/test_import_rules.py`.
      **Critère** : test vert sur le nouveau package.
- [x] **T088** — Schéma de config typé + validation ; `config_audit.py
      --strict` + contrat GUI. **Fichiers** : `rotation/infrastructure/config/`.
      **Critère** : 0 clé morte, 0 option inerte.
- [x] **T089** — Registre de dette `debt.md` + gate par phase : 0 P0 ouverte,
      P1 planifiée. **Critère** : gate automatisé.

**Gate 2.5** : DB migrée, intégrité OK, 0 perte utile, test d’import vert,
0 P0 ouverte.

---

## Phase 3 — Performance et préflight

- [x] **T040** — Implémenter `PreflightProber` : DNS, endpoint, handshake
      WireGuard, RTT, timeout 2 s. **Fichiers** : nouveau module
      `free/ip_registry/preflight.py`, `free/ip_registry/adapters/egress.py`.
      **Critère** : 1 000 hosts testés en < 2 min avec 64 workers.
- [x] **T041** — Paralléliser le préflight (64 workers) avec budget
      `hosts_per_min`. **Critère** : pas de saturation CPU/réseau.
- [~] **T042** — Bind rapide pour les préflightés : `settle_s=6`,
      `egress_wait_s=20` ; garder 45 s pour les non-préflightés.
      **Fichiers** : `config.yaml`, `scripts/fresh_chain.py`.
      **Critère** : bind réussi < 30 s p95.
- [x] **T043** — Vérification du manifeste par conteneur et sync atomique
      (tmp + rename + vérification `docker exec`). **Fichiers** :
      `scripts/fresh_chain.py:287-333`. **Critère** : zéro `hostname not
      valid`.
- [x] **T044** — Épingler l'image Gluetun et tester la compatibilité du
      manifeste. **Fichiers** : `docker-compose.yml`, `ops/updater.py`.
      **Critère** : version fixe, test de lecture du manifeste.
- [~] **T045** — Mesurer le taux de préflight OK et de bind OK sur un
      échantillon de 200 hosts. **Critère** : ≥ 60 % de bind OK sur
      préflightés.
- [~] **T046** — Balayage sec de 1 000 hosts, puis 9 787, avec progression
      persistée. **Critère** : 100 % visités, < 24 h.
- [x] **T047** — Dashboard : sweep progress, ETA, prochain candidat, raison
      par station. **Fichiers** : `dashboard/api.py`,
      `dashboard-ui/`. **Critère** : visible sans logs.

**Gate 3** : balayage complet < 24 h, bind réussi ≥ 60 % sur préflightés.

---

## Phase 4 — Fiabilité et SLO

- [x] **T050** — Rotation préventive **optionnelle** (GUI, désactivée par
      défaut) : si activée, seuil `rotation_threshold_pct` ; si désactivée,
      aucune rotation préventive. **Fichiers** : `free/pool.py`,
      `free/ip_quota.py`, `dashboard/api.py`. **Critère** : option off →
      aucune rotation proactive.
- [x] **T051** — Journal d'incidents + alertes : `client_usable < 5`,
      `warm_pool < 3`, `capacity_degraded`, `duplicate_ip`,
      `sweep_stalled`. **Fichiers** : `observability/db.py`,
      `dashboard/api.py`. **Critère** : alerte dans les 60 s.
- [x] **T052** — Re-test programmé des hosts/pools en cooldown après
      expiration, sans condamnation définitive. **Critère** : un host mort
      redevient éligible.
- [x] **T053** — Purge/renouvellement des leases orphelins au boot et
      périodiquement. **Critère** : aucun lease bloqué > TTL.
- [~] **T054** — Tests de panne : tuer une IP, un pays, un /24, le
      superviseur, couper un conteneur. **Critère** : reprise automatique.
- [x] **T055** — Test de redémarrage : reprise au curseur, pas de double
      bind, pas de perte d'assignation. **Critère** : état cohérent.
- [x] **T056** — Test d'unicité et de démarrage : 6 stations, 6 IP
      distinctes, 0 IP partagée ; 2 stations utilisables conservées, 4
      complétées. **Critère** : 0 `duplicate_ip`, démarrage sans recréation
      inutile.
- [~] **T057** — Test de fallback IP/DNS : couper le site principal, le DNS
      principal, puis les deux ; vérifier que la station reste utilisable ou
      `DEGRADED`, jamais `BROKEN` sur une panne externe. **Critère** : 0
      `no-egress` causé par un site/DNS.
- [x] **T058** — Test tri-stack : serveur WG KO/OV UDP OK, serveur WG+UDP
      KO/OV TCP OK, serveur tout KO → suivant. **Critère** : les trois
      technologies sont essayées et la boucle avance.
- [~] **T059** — Test contrat GUI : modifier chaque option et vérifier
      l'effet runtime ; `config_audit.py --strict`. **Critère** : 100 % des
      options testées, 0 inerte.

**Gate 4** : 6/6 routable ≥ 99 %, 0 station connectée non utilisable.

---

## Phase 5 — Validation et déploiement

- [~] **T060** — Shadow mode 24 h : le nouveau sélecteur calcule et
      journalise, sans binder. **Critère** : décisions comparables à
      l'ancien chemin.
- [ ] **T061** — Canary 1 station 24 h. **Critère** : 0 régression, 429
      remplacé < 60 s.
- [ ] **T062** — Canary 3 stations, 3 IP distinctes, 24 h. **Critère** :
      aucune IP partagée, 3 stations indépendantes.
- [ ] **T063** — Bascule 6 stations. **Critère** : 6/6, 6 IP distinctes,
      pays choisis par performance.
- [ ] **T064** — Soak 72 h avec trafic réel. **Critère** : SLO tenus, 0
      intervention manuelle.
- [x] **T065** — Documentation opérateur : dashboard, causes, actions,
      rollback. **Fichiers** : `docs/ALLOCATOR_ALL_STATIONS.md`,
      `README.md`.
- [~] **T066** — Revue post-déploiement : ajuster poids, seuils, TTL.
      **Critère** : décisions documentées.

**Gate 5** : 6/6, 0 IP partagée, zéro intervention manuelle sur 72 h.

---

## Phase 6 — Décommissionnement et dette

- [~] **T070** — Supprimer `fresh_chain*` et `catalog_claims` (après shims) ;
      archiver les JSON d’état legacy. **Critère** : aucun chemin de décision
      ne lit un JSON.
- [~] **T071** — Extraire les monolithes par lots (`vpn/manager.py`,
      `free/pool.py`, `opencode.py`) ; aucun nouveau module > 1 500 lignes.
      **Critère** : taille réduite, tests de non-régression verts.
- [~] **T072** — Fermer les dettes P1/P2 de `debt.md` ; mettre à jour le
      registre. **Critère** : 0 P0/P1 ouverte.
- [x] **T073** — Revue d’architecture post-migration : ADR, imports, tests,
      documentation. **Critère** : architecture cible stable.
- [x] **T074** — Runbook opérateur final (dashboard, causes, fallback,
      rollback, migration). **Critère** : un opérateur peut diagnostiquer sans
      logs.
- [~] **T075** — Nettoyage final : code mort, flags legacy, logs/DB de test.
      **Critère** : `config_audit.py --strict`, `debt.md` à jour.

**Gate 6** : 0 P0/P1 ouverte, legacy supprimé, architecture cible stable.

---

## Décisions

### Actées

- [x] **D1** — Toutes les stations peuvent utiliser n'importe quel pays ;
      aucun quota par pays ni par continent.
- [x] **D2** — Rotation par station, sur 429 ou manuelle, en parallèle.
- [x] **D3** — Superviseur seul propriétaire.
- [x] **D4** — Station 7 supprimée.
- [x] **D5** — Clé WireGuard commune à toutes les stations.
- [x] **D6** — Rotation proactive optionnelle, désactivée par défaut.
- [x] **D7** — Préflight accepté.
- [x] **D8** — Démarrage : conserver les stations utilisables, compléter les
      manquantes.
- [x] **D9** — Reset : réutilisation à la prochaine rotation qui en a besoin,
      sans bascule proactive.
- [x] **D10** — Score composite.
- [x] **D12** — Maximum de fallback sur toutes les dépendances (IP, DNS,
      catalogue, manifeste, bind, probe, superviseur).
- [x] **D13** — Transport auto : WireGuard → OpenVPN UDP → OpenVPN TCP,
      boucle par serveur, chaque station indépendante.
- [x] **D14** — Chaque option GUI doit avoir un effet réel et un test ;
      aucune option inerte.

- [x] **D11** — Poids du score : 40 % taux de 200, 25 % latence, 20 % débit,
      10 % stabilité, 5 % fraîcheur (ajustables GUI).

---

## Ordre recommandé

1. Phase 0 (T001–T009)
2. Phase 1 (T010–T026)
3. Phase 2 (T030–T038)
4. Phase 2.5 (T080–T089) — architecture/DB
5. Phase 3 (T040–T047)
6. Phase 4 (T050–T059)
7. Phase 5 (T060–T066)
8. Phase 6 (T070–T075) — décommissionnement

La Phase 2.5 peut commencer en parallèle de la Phase 2, mais le schéma de
données (T081) doit être stable avant la migration (T083) et le préflight
(T040).

