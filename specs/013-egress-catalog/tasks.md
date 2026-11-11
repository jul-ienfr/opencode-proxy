# Tasks — Catalogue d'egress et rotation

**Feature**: 013-egress-catalog
**Plan**: [plan.md](./plan.md)
**Spec**: [spec.md](./spec.md)

## Phase 0 — Spécification et recherche

- [x] T000 Écrire `spec.md`, `plan.md`, `research.md`, `data-model.md`,
  `contracts/egress-catalog.md`, `quickstart.md`.
- [x] T001 Vérifier que `scripts/autoscale_max_ips.py` n'est planifié par aucun
  service/tâche Windows et le documenter comme one-shot transitoire.
- [x] T002 Relever la capacité saine NordVPN (sessions simultanées) sur une
  fenêtre de 24 h ; figer `max_active_stations`.
- [x] T003 Relever le nombre de `/64` WARP distincts observés sur 24 h.
- [x] T004 Ajouter `core/process.py` (`run_hidden`/`popen_hidden`) et
  `scripts/platform_audit.py` ; brancher la gate Windows/Linux.
- [x] T005 Ajouter `tests/test_platform_audit.py` : 0 violation, détection
  d'un lancement visible, de `shell=True`, de `os.system` et d'un chemin
  Windows en dur.
- [x] T006 Ajouter `tests/test_option_map.py` : toute clé `egress_catalog.*`
  et tout contrôle GUI du catalogue a une ligne `option-map`.
- [x] T007 Ajouter `free/threat_intel.py` : flux IP/CIDR gratuits, cache
  SQLite, refresh fail-soft, purge des flux retirés.
- [x] T008 Ajouter `egress_catalog.denylist` et le filtrage avant probe
  (IP statique + threat-intel) avec test de non-contact.
- [x] T009 Ajouter `scripts/ip_reputation.py` et `tests/test_ip_reputation.py`
  pour corroborer une alerte antivirus sans contacter l'IP.

## Phase 1 — Catalogue + public (MVP)

- [x] T010 Ajouter la migration `013` dans `registry.db` : `egress_sources`,
  `egress_options`, `egress_observations`, `egress_rotation_slots`,
  `catalog_runs`, `egress_provider_state`.
- [x] T011 Implémenter `free/egress_catalog.py` : clés canoniques, états, TTL,
  gates, scoring, transitions. Tests unitaires sans I/O.
- [x] T012 Implémenter `free/egress_store.py` : CRUD, dédup par egress,
  append-only observations, rétention, export JSON atomique.
- [x] T013 Implémenter `free/egress_sources.py` : registre, parsers
  `public_text`, `public_json`, `geonode_json`, validation des sources.
- [x] T014 Brancher les sources publiques existantes
  (`fetch_proxy_lists.py`/`probe_proxy_pool.py`/`test_proxy_pool.py`) sur
  l'adapter public, via le worker isolé.
- [x] T015 Ajouter `scripts/egress_catalog.py` : `refresh`, `export`,
  `verify`, `--provider`, `--max-candidates`, `--observe-only`.
- [x] T016 Ajouter `GET /api/egress/catalog`,
  `GET /api/egress/catalog/options`, `GET /api/egress/catalog/runs`.
- [x] T017 Tests : dédup 20k, 200/429/dead, TTL, reset_at, sources mortes.
- [x] T018 Test d'intégration : 3 candidats factices → 1 usable, 1 quota,
  1 dead ; aucune option non validée dans le pool.

## Phase 2 — WARP

- [x] T020 Adapter `free/egress_adapters/warp.py` : lecture des conteneurs,
  egress IPv6, `/64`, TTL, historique.
- [x] T021 Ingérer les 12 blocs `/64` actuels (6 conteneurs, IPv6 seuls).
- [x] T022 Rotation WARP : demander une option `usable` au catalogue,
  exclure `quota`/expiré, refuser de brûler une IP sans option.
- [x] T023 Tests : conteneur recréé → ancien `/64` expiré ; 429 → quota ;
  deux conteneurs même `/64` → une option.

## Phase 3 — NordVPN

- [x] T030 Adapter `free/egress_adapters/nordvpn.py` : stations, IP,
  `vpn.status`, `auth_failed`, `quota_lease`, `reset_at`.
- [x] T031 Ingérer l'historique `registry.db` + les 6 IP courantes.
- [x] T032 Rotation NordVPN : préférer une IP fraîche du catalogue ; ne pas
  rouvrir plus de sessions que `max_active_stations`.
- [x] T033 Tests : 200/429/AUTH_FAILED, changement d'IP sur même station,
  rollback si AUTH.

## Phase 4 — Scheduler + UI

- [x] T040 `free/egress_scheduler.py` : boucle asyncio, single-flight par
  provider, jitter, `close()`, fail-soft.
- [x] T041 `POST /api/egress/catalog/refresh` (202 + job) et
  `POST /api/egress/catalog/sources`.
- [x] T042 UI « Catalogue egress » : résumé par provider, options, runs,
  bouton refresh, âge du dernier test.
- [x] T043 Métriques : `catalog_known`, `catalog_usable`, `catalog_quota`,
  `catalog_auth`, `catalog_dead`, `catalog_last_run_age_s`.
- [x] T044 Tests scheduler : cycle échoué conserve le catalogue ; un seul
  cycle par provider ; pas de cycle dans la requête.

## Phase 5 — Convergence

- [x] T050 Brancher `FreeIPPool` sur `egress_rotation.select()` en shadow,
  comparer les choix, puis canary.
- [x] T051 Retirer/archiver `scripts/autoscale_max_ips.py` ; le scheduler
  catalogue devient l'unique automatisation.
- [x] T052 Mettre à jour `PLAN_PROXIES_PUBLICS_2026-11-09.md`, `CHANGELOG.md`,
  README et le dashboard.
- [x] T053 Gate : `pytest -k "not docker"`, `config_audit --strict`,
  `npm run check`, smoke live opt-in.

## Dépendances

```text
T010 → T011 → T012 → T013 → T014 → T015 → T016 → T017/T018
T010 → T020 → T021 → T022 → T023
T010 → T030 → T031 → T032 → T033
T016/T023/T033 → T040 → T041/T042/T043/T044
T050 → T051 → T052 → T053
```

## Definition of Done

- Tous les FR de `spec.md` ont un test ou une preuve live référencée.
- Aucun `dead`/`auth` sélectionné ; aucun 429 avant `reset_at`.
- Le catalogue survit à un redémarrage et à une source morte.
- 64 options publiques, 12 blocs WARP, 6 IP NordVPN connues avec état.
- Aucun secret dans les observations, l'API ou l'UI.
- `platform_audit --strict` = 0 violation ; aucun processus visible sous
  Windows ; la gate passe sur Windows et Linux.
- `test_option_map.py` = 0 option/contrôle sans consommateur ni test.
- `threat_feeds` = 0 IP/CIDR de la denylist contactée ; un flux en panne
  conserve ses indicateurs.
