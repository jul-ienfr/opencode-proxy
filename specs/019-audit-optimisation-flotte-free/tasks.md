# Tasks — Audit et optimisation de la flotte free

**Feature** : `019-audit-optimisation-flotte-free`
**Input** : `spec.md`, `plan.md`, `audit.md`
**Date** : 2026-11-14

Légende : `[P]` = parallélisable ; `P0` = débloque le 6/6 ; `P1` = fiabilité ;
`P2` = qualité/dette. Statut : `à faire`, `en cours`, `fait`.
Aucune tâche n'a été appliquée : l'audit est en lecture seule.

---

## Phase 0 — Mesure et sécurité

### T301 — Lanceur supervisé de l'app (P0) — à faire
- **Fichiers** : nouveau `ops/service/opencode-proxy.xml` (NSSM) ou
  `scripts/install_app_service.ps1`, `logs/app-stdout.log`,
  `logs/app-stderr.log`.
- **Action** : enregistrer `opencode.py` comme service `restart=always`,
  rediriger stdout+stderr horodatés, ajouter `faulthandler` et
  `sys.excepthook`.
- **Test** : `taskkill /F` sur le PID → `/health` 200 en < 30 s ; traceback
  présent dans le fichier de capture.
- **Acceptation** : US3 ; uptime 99,9 % sur 24 h.

### T302 — `/metrics` + SLO minute (P0) — à faire
- **Fichiers** : `app/routes/` (nouveau routeur), `observability/`,
  `registry.db` (table `slo_samples`).
- **Action** : exposer `station_client_usable`, `warm_pool_live`,
  `ip_200_rate`, `rotate_latency_seconds`, `app_up`, `code_reload_total`,
  `auth_attempts_total`, `incidents_open`, `rotation_jobs_orphans` ; job minute qui écrit une ligne SLO.
- **Test** : `curl /metrics` contient les 9 métriques ; une ligne `slo_samples`
  par minute sur 5 min.
- **Acceptation** : §9.3 du plan de mesure alimenté sans log.

### T303 — Token dashboard et auth proxy (P1) — à faire
- **Fichiers** : `config.yaml` (`require_token`, `client_auth`), `opencode.py`,
  `README.md`.
- **Action** : `require_token=true` hors loopback, `client_auth.mode=token`,
  token depuis `.env`, 401 sur les routes de contrôle.
- **Test** : `curl` sans token → 401 ; avec token → 200.
- **Acceptation** : aucune route de contrôle accessible sans token.

### T304 — Purge registre + checkpoint WAL (P1) — à faire
- **Fichiers** : `free/qualification/store.py`, `observability/db.py`,
  `scripts/` (job purge).
- **Action** : supprimer `warm_pool` expirés, `claims` et `incidents` > 7 j ;
  checkpoint si WAL > 4 Mio ; `integrity_check` quotidien.
- **Test** : injecter 1 000 lignes expirées, vérifier 0 après purge ;
  `integrity_check=ok`.
- **Acceptation** : `registry.db` < 50 Mio, WAL < 4 Mio.

---

## Phase 1 — Autorité unique de l'état

### T305 — `station_state` read-model de l'app (P0) — à faire
- **Fichiers** : `free/qualification/store.py:702`, `free/pool/__init__.py`,
  `app/routes/` (API).
- **Action** : l'app écrit `station_state` à chaque probe ; l'écriture
  qualification passe derrière `STATION_STATE_SOURCE=shadow` pendant 24 h,
  puis est retirée ; l'API lit la table.
- **Test** : test d'intégration qui compare API/table/fork ; divergence = échec.
- **Acceptation** : P0-02 ; zéro divergence 24 h.

### T306 — Table `pins` et `.env` généré (P1) — à faire
- **Fichiers** : `scripts/fresh_chain_supervisor.py` (`_persist_pin`,
  `_clear_stale_pins`), `free/catalog_claims.py`, `.env`.
- **Action** : pins dans le registre ; génération de `.env` depuis le registre ;
  suppression des `SERVER_HOSTNAMES_STATION7..10`.
- **Test** : deux cycles de pin, vérifier une seule écriture et zéro pin
  orphelin.
- **Acceptation** : P1-01 ; un seul fichier contient l'IP/pin.

### T307 — Retirer le log et le rotate du chemin chaud (P0) — à faire
- **Fichier** : `free/pool/__init__.py:5134-5136`.
- **Action** : log en DEBUG au changement d'état ; `rotate_exhausted_managed()`
  seulement si l'ensemble épuisé est non vide.
- **Test** : 100 requêtes sans IP épuisée → 0 ligne, 0 rotation.
- **Acceptation** : P0-05 ; ≤ 1 ligne/min au repos.

### T308 — Test de non-divergence (P1) — à faire
- **Fichiers** : `tests/test_state_authority.py` (nouveau).
- **Action** : comparer `(station, client_usable, current_ip)` entre API,
  `station_state` et fork, sur un registre factice.
- **Test** : le test échoue si une surface diverge.
- **Acceptation** : garde-fou permanent de US2.

---

## Phase 2 — Quota et approvisionnement (débloque 6/6)

### T309 — Séparer `ours_used` et `upstream_verdict` (P0) — à faire
- **Fichiers** : `free/pool/__init__.py`, `free/qualification/store.py`,
  `config.yaml` (`quota_per_ip` conservé comme compteur, pas comme verdict).
- **Action** : modèle `unknown/hot/cooling/dead` (`plan.md` D2) ;
  `cooling_until = min(retry_after, last_429+1200)` ; `ours_used` informatif.
- **Test** : IP à `used=1` avec 200 récent → `hot` ; 429 unique → `cooling`,
  pas `dead` ; 3 échecs transport → `dead`.
- **Acceptation** : P0-01 ; 6/6 possible sur un cycle.

### T310 — Consommer le warm pool avant qualification (P0) — à faire
- **Fichiers** : `scripts/fresh_chain_supervisor.py:1240`
  (`_refresh_warm_pool`), `free/pool/__init__.py` (sélection).
- **Action** : `warm_pool_min=6` ; l'affectation lit le pool en premier ;
  suppression à la consommation ; re-probe si `verified_at` > 1 h.
- **Test** : station en `DRAINING` → reçoit une IP du pool au cycle suivant,
  sans qualification.
- **Acceptation** : P0-04 ; `warm_pool_used ≥ 0,8`.

### T311 — Préflight avant bind (P0) — à faire
- **Fichiers** : `scripts/fresh_chain.py`, `free/qualification/`.
- **Action** : DNS + port + egress en 5 s avant tout bind ; un échec de
  préflight ne consomme pas de crédit de rotation.
- **Test** : candidat injoignable → `preflight_failed`, zéro bind.
- **Acceptation** : `bind_error` < 20 % (contre 59,1 %).

### T312 — Respect strict de `reset_at` (P0) — à faire
- **Fichiers** : `scripts/fresh_chain_supervisor.py:1486`
  (`_ledger_reset_at`), `:1708` (`_station_exhausted`).
- **Action** : aucune tentative avant `reset_at` ; plafond `ledger_max_block_s`
  appliqué et visible ; une IP `cooling` n'est re-testée qu'une fois.
- **Test** : IP avec `reset_at=now+600` → zéro tentative avant 600 s ;
  re-test unique à l'échéance.
- **Acceptation** : 100 % des resets respectés sur 24 h.

### T313 — Mesure continue « 6/6 même cycle » (P0) — à faire
- **Fichiers** : `scripts/` (job), `registry.db` (`slo_samples`).
- **Action** : exécuter la requête §9.2 toutes les 2 min ; alerter si < 6
  pendant 10 min ; écrire l'historique.
- **Test** : simuler 6/6 puis 5/6 → alerte après 10 min.
- **Acceptation** : US1 mesurée sans dashboard.

---

## Phase 3 — Fiabilité et performance

### T314 — Un seul écrivain de la pause AUTH (P1) — à faire
- **Fichiers** : `scripts/fresh_chain_supervisor.py:744` (`_auth_gate`),
  `:727`/`:735` (pause), `tests/test_station_allocator_supervisor.py`.
- **Action** : compteurs par station et global dans le registre ; un seul
  écrivain ; test de concurrence (deux chaînes simultanées).
- **Test** : deux tentatives dans la même minute → une seule passe.
- **Acceptation** : P1-02 ; ≤ 1 AUTH/10 min.

### T315 — Aligner les timeouts de rotation (P1) — à faire
- **Fichiers** : `config.yaml:1227`, `free/ip_registry/`.
- **Action** : `rotate_timeout_s = rotation_max_duration + 30` (270 s).
- **Test** : 100 rotations, zéro timeout côté appelant.
- **Acceptation** : P1-09 ; p95 < 240 s.

### T316 — Rotation asynchrone avec `job_id` (P2) — à faire
- **Fichiers** : `free/ip_registry/adapters/gluetun_control_v2.py`, fork
  `internal/control/jobs/`.
- **Action** : l'appel rend un `job_id` ; exposition `position`, `eta_s`.
- **Test** : 4 rotations en file → positions 1..4 visibles.
- **Acceptation** : file observable, pas d'attente aveugle.

### T317 — Regrouper les incidents (P1) — à faire
- **Fichiers** : `scripts/fresh_chain_supervisor.py:1387`
  (`_sync_fleet_incidents`).
- **Action** : clé `(cause, fenêtre 60 s)` ; un incident flotte pour N stations.
- **Test** : 6 `tunnel_down` simultanés → 1 incident.
- **Acceptation** : P1-03 ; ≤ 5 incidents/h.

### T318 — T211 DNS transitoire (P1) — à faire
- **Fichiers** : fork `internal/dns/`, `internal/control/`.
- **Action** : un échec DNS seul ne coupe pas un tunnel dont l'egress est
  prouvé < 60 s ; `doh` prioritaire, `plain` dernier recours.
- **Test** : couper DoH, vérifier que le tunnel reste up et que `plain` prend
  le relais.
- **Acceptation** : T211 de 018 cochée ; zéro teardown sur DNS seul.

---

## Phase 4 — Dette et exploitation

### T319 — Purger `requests.db` et plafonner (P1) — à faire
- **Fichiers** : `opencode.py:3858`, `config.yaml:877-879`.
- **Action** : supprimer les lignes > 7 j (pas seulement les corps),
  `auto_vacuum=INCREMENTAL`, plafond 500 Mio, supprimer le `.bak` de 1,3 Go.
- **Test** : injecter 10 000 lignes anciennes, vérifier 0 après purge ;
  taille < 500 Mio.
- **Acceptation** : P1-05.

### T320 — Nettoyer Compose et `.env` (P2) — à faire
- **Fichiers** : `docker-compose.yml:478-544`, `.env`.
- **Action** : supprimer `opencode-vpn-7..10` et `SERVER_HOSTNAMES_STATION7..10`.
- **Test** : `docker compose config` ne liste que six stations.
- **Acceptation** : P2-02 ; aucune 7ᵉ station possible.

### T321 — Borner WARP (P2) — à faire
- **Fichiers** : `scripts/autoscale_warp*.py`, `docker-compose.yml`.
- **Action** : 2–3 WARP maximum, arrêter les sweep inutilisés.
- **Test** : mesure CPU hôte avant/après ; fallback toujours fonctionnel.
- **Acceptation** : P2-03 ; CPU < 70 %.

### T322 — Tests des 5 fonctions superviseur (P1) — à faire
- **Fichiers** : `tests/test_station_allocator_supervisor.py`,
  nouveaux `tests/test_supervisor_warm_pool.py`,
  `tests/test_supervisor_settling.py`.
- **Action** : un test par fonction (`_auth_gate` existe déjà) :
  `_best_resume_station`, `_refresh_warm_pool`, `_station_settling`,
  `_remove_extra_stations`.
- **Test** : 4 nouveaux tests, CI bloquante.
- **Acceptation** : P1-08 ; couverture des 5 fonctions.

### T323 — Nettoyer le dépôt (P2) — à faire
- **Fichiers** : racine (`check_now.py`, `probe_*.py`, `query_*.py`,
  `_arb.py`), `logs/vpn_state*.json`.
- **Action** : déplacer dans `scripts/diagnostics/` ou supprimer ;
  `vpn_state*.json` en `logs/debug/` avec date.
- **Test** : `git status` propre hors livrables 019.
- **Acceptation** : P2-01, P2-06.

---

### T324 — Réconcilier les `rotation_jobs` orphelins (P1) — à faire
- **Fichiers** : `free/rotation.py`, `free/egress_rotation.py`,
  `scripts/fresh_chain_supervisor.py` (démarrage), `registry.db`
  (`rotation_jobs`).
- **Constat** : 1 041/2 352 jobs en `started` sans fin, plus ancien
  2026-11-07 ; 1 job/heure fuit encore (P1-10).
- **Action** : état terminal garanti (`try/finally`) ; au démarrage et toutes
  les 5 min, marquer `abandoned` tout `started` plus vieux que
  `rotation_max_duration + 60 s`, avec la raison d'origine.
- **Test** : insérer un job `started` daté d'1 h → réconcilié `abandoned` ;
  tuer un job en cours → état terminal écrit.
- **Acceptation** : 0 `started` > 5 min ; `rotate_committed/rotate_total`
  calculable sur 24 h.

## Matrice de traçabilité

| Constat | Tâches | Critère de sortie |
|---|---|---|
| P0-01 quota | T309, T312 | 6/6 même cycle |
| P0-02 état | T305, T308 | divergence 0 |
| P0-03 app | T301 | retour < 30 s |
| P0-04 approvisionnement | T310, T311 | 200 > 30 %, pool consommé |
| P0-05 chemin chaud | T307 | ≤ 1 ligne/min |
| P1-01 pins | T306 | un seul écrivain |
| P1-02 AUTH | T314 | ≤ 1 AUTH/10 min |
| P1-03 incidents | T317 | ≤ 5/h |
| P1-04 registre | T304 | < 50 Mio |
| P1-05 requêtes | T319 | < 500 Mio |
| P1-06 sécurité | T303 | 401 sans token |
| P1-07 observabilité | T302 | 8 métriques |
| P1-08 tests | T322 | 5 fonctions testées |
| P1-09 rotation | T315, T316 | p95 < 240 s |
| P1-10 jobs orphelins | T324 | 0 `started` > 5 min |
| P2-01..06 dette | T320, T321, T323 | dépôt propre |

## Ordonnancement proposé

- **Jour 1** : T301, T302, T303, T304 (parallélisables).
- **Jour 2** : T305, T307, T308 ; T306 en parallèle.
- **Jours 3–4** : T309, T310, T311, T312, T313 (chemin critique).
- **Jours 5–6** : T314, T315, T317, T318 ; T316 si le fork est prêt.
- **Jours 6–8** : T319, T320, T321, T322, T323.