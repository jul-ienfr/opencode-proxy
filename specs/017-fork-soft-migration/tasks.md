# Tasks — Migration douce vers le fork Gluetun 016

**Feature** : `017-fork-soft-migration`
**Input** : `spec.md`, `plan.md`
**Date** : 2026-11-14

Légende : `[P]` = parallélisable ; `P0` = bloquant la release ;
`P1` = requis pour la cible ; `P2` = qualité et robustesse.
Statut : `fait`, `en cours`, `à faire`.

---

## Phase 1 — Fork : ladder avant tunnel

### T101 — Résoudre l'IP du serveur depuis le catalogue provider (P0) — fait
- **Fichiers** : `internal/vpn/ladder.go`, `internal/vpn/ladder_target_test.go`.
- **Action** : `targetFor` appelle `provider.GetConnection` avec
  `ServerSelection.VPN` et `Mode` par défaut ; l'IP est passée au préflight.
- **Acceptation** : aucun `config_invalid` de résolution sur une station dont
  le DNS est fail-closed avant le tunnel.
- **Test** : `Test_ladderDriver_targetFor` (provider OK, erreur, endpoint
  explicite, hostname vide).
- **Preuve live** : station 1, `wireguard success 5969ms`, egress
  `186.247.58.40`.

### T102 — Court-circuiter le DNS quand l'endpoint est explicite (P0) — fait
- **Fichiers** : `internal/vpn/transport/preflight.go`,
  `internal/vpn/transport/preflight_test.go`.
- **Action** : une `EndpointIP` valide est utilisée directement ; le DNS n'est
  appelé que si elle est absente.
- **Acceptation** : un resolver en erreur ne bloque pas un préflight avec
  endpoint explicite.
- **Test** : `TestPreflightExplicitEndpointSkipsDNS`.

### T103 — Ladder complet quand `VPN_TYPE` n'est pas explicite (P0) — fait
- **Fichiers** : `internal/control/jobs/executor.go`,
  `internal/control/jobs/executor_test.go`,
  `internal/control/jobs/transport_policy_test.go`,
  `internal/server/control.go`.
- **Action** : `VPNExplicit=false` ⇒ `wireguard,openvpn-udp,openvpn-tcp` ;
  une policy explicite reste compatible avec le type courant.
- **Acceptation** : un switch sans policy sur une station auto expose les trois
  transports.
- **Test** : `Test_Executor_transportLadder_FollowsExplicitVPN`.

### T104 — Budgets de switch et rollback compatibles avec le ladder (P0) — fait
- **Fichiers** : `internal/control/jobs/executor.go`,
  `internal/control/jobs/rollback.go`, `config.yaml`.
- **Action** : `switch=180s`, `await=120s`, `verify=60s`, `rollback apply=60s`,
  `await=120s`, `verify=60s` ; client `switch_timeout_s=180`,
  `rotate_timeout_s=300`.
- **Acceptation** : un repli WireGuard → UDP ne produit plus
  `awaiting tunnel readiness: context deadline exceeded`.
- **Test** : tests jobs existants + validation live.

---

## Phase 2 — Superviseur : chemin chaud v2

### T105 — Publier les ports de contrôle et activer le mode (P0) — fait
- **Fichiers** : `logs/compose-control-plane-N.yml`, `config.yaml`.
- **Action** : `127.0.0.1:800N:8000`, `mode=active`,
  `base_url=http://127.0.0.1:800{station}`,
  `api_key_env=VPN_CONTROL_API_KEY`, `transport_owner=fork`.
- **Acceptation** : `/v1/control/capabilities` répond depuis l'hôte sur
  `8001..8006`.
- **Test** : requête live station 2 (`switch=true`, `rotate=true`,
  `transport_ladder=true`).

### T106 — Câbler le binder v2 dans fresh_chain (P0) — fait
- **Fichiers** : `scripts/fresh_chain.py`.
- **Action** : `load_control_plane_config().is_active` ⇒ construire
  `GluetunControlV2Binder` avec cycle unique ; repli legacy si indisponible.
- **Acceptation** : le log contient `control plane active for station N`.
- **Test** : `test_binder_maps_a_committed_job_to_bind_result`.

### T107 — Clé d'idempotence par cible (P0) — fait
- **Fichiers** : `free/ip_registry/adapters/gluetun_control_v2.py`,
  `tests/test_gluetun_control_v2.py`, `tests/test_rotation_contract.py`.
- **Action** : `switch-{station}-{cycle}-{hostname}`, rotation avec hash des
  candidats ; une même requête rejouée garde la même clé.
- **Acceptation** : aucun `idempotency_conflict` sur des cibles successives.
- **Test** : clé par hostname + hash rotate + replay stable.

### T108 — Repli Compose sur station cassée (P0) — fait
- **Fichiers** : `free/ip_registry/adapters/gluetun_control_v2.py`,
  `free/ip_registry/adapters/hybrid_binder.py`.
- **Action** : `resolution_failed` ⇒ `control-unavailable:resolution-failed` ;
  `crashed`/`awaiting tunnel` ⇒ `control-unavailable:station-degraded` ;
  le binder hybride recrée via Compose.
- **Acceptation** : une station dont la boucle VPN est crashée est recréée au
  lieu de boucler sur des switchs in-place.
- **Test** : `test_binder_maps_resolution_failure_to_a_cold_fallback_hint`,
  `test_binder_maps_a_crashed_station_to_a_compose_fallback_hint`.

### T109 — Override « auto » sans `VPN_TYPE` (P0) — fait
- **Fichiers** : `free/ip_registry/adapters/compose_binder.py`,
  `tests/test_ip_registry_compose_binder.py`.
- **Action** : `vpn_type=auto` écrit `VPN_TYPE=` vide ; le ladder runtime est
  actif après recreate.
- **Acceptation** : un recreate de réparation ne fige pas un transport.
- **Test** : `test_auto_override_leaves_vpn_type_for_the_fork_ladder`.

---

## Phase 3 — Migration douce

### T110 — Garantir qu'une station saine n'est jamais recréée (P0) — en cours
- **Fichiers** : `scripts/fresh_chain_supervisor.py`,
  `tests/test_station_allocator_supervisor.py` (ou équivalent).
- **Action** : vérifier `_needs_chain` (station_usable, model_usable, IP) et
  ajouter un test de non-régression « image plus récente + station saine ⇒
  aucune action ».
- **Acceptation** : `StartedAt` et image inchangés sur 24 h pour une station
  saine.
- **Test** : test superviseur avec station saine et image taguée.

### T111 — Valider une réparation superviseur de bout en bout (P0) — en cours
- **Fichiers** : logs superviseur, `logs/fresh_chain_N.log`, état des stations.
- **Action** : laisser le superviseur détecter une station cassée, tenter v2,
  basculer Compose si nécessaire, prouver l'egress.
- **Acceptation** : station `healthy`, `egress.verified=true`, IP différente de
  l'hôte, sans commande manuelle.
- **Test** : observation live sur une station cassée uniquement.

### T112 — Supprimer les interventions manuelles du runbook (P1) — en cours
- **Fichiers** : `ops/runbooks/control-plane.md`.
- **Action** : documenter le superviseur comme seul acteur ; le mode manuel
  reste un diagnostic d'urgence, pas le chemin normal.
- **Acceptation** : le runbook ne demande jamais de geler le superviseur pour
  une migration.
- **Test** : revue du runbook.

---

## Phase 4 — Observabilité, performance, qualité

### T113 — Mesurer les SLO de réparation (P1) — à faire
- **Fichiers** : `scripts/station_slo_watch.py`, `logs/station_slo.jsonl`,
  métriques superviseur.
- **Action** : mesurer détection, durée de réparation, p95 switch, taux de
  commit, recreates, conflits d'idempotence.
- **Acceptation** : NFR-001..003 mesurés sur 24 h.
- **Test** : échantillons cohérents avec calcul manuel.

### T114 — Test de non-régression « zéro churn » (P1) — à faire
- **Fichiers** : `tests/test_fresh_chain_supervisor.py` (ou équivalent).
- **Action** : station saine + image plus récente ⇒ aucune chaîne, aucun
  recreate.
- **Acceptation** : 0 recreate sur 24 h.
- **Test** : test dédié.

### T115 — Benchmark du chemin chaud (P2) — à faire
- **Fichiers** : `scripts/bench_*.py`, logs.
- **Action** : 20 switches sur une station de test, mesurer p50/p95, vérifier
  l'absence de `docker exec`.
- **Acceptation** : p95 ≤ 45 s sur le premier transport.
- **Test** : rapport de benchmark.

### T116 — Acceptance 6/6 free (P0) — à faire
- **Fichiers** : `config.yaml`, `registry.db`, dashboard.
- **Action** : vérifier 6 stations `client_usable`, IP distinctes, trois
  transports disponibles, aucun payant/second compte/attente.
- **Acceptation** : SC-001..005.
- **Test** : contrôle live + SLO.

---

## Dépendances et ordre

1. T101–T104 (fork) avant tout switch v2 fiable.
2. T105–T109 (superviseur) avant la migration douce.
3. T110–T111 avant de retirer les interventions manuelles.
4. T113–T116 pour la preuve de qualité et la clôture.

## Preuves attendues

```text
go test ./internal/vpn/... ./internal/control/... ./internal/server -count=1
python -m pytest tests/test_gluetun_control_v2.py tests/test_hybrid_binder.py \
  tests/test_ip_registry_compose_binder.py tests/test_control_plane_config.py \
  tests/test_rotation_contract.py -q
ruff check free/ip_registry/adapters scripts/fresh_chain.py
docker exec opencode-vpn-1 wget -qO- http://127.0.0.1:8000/v1/vpn/transport
curl -s http://127.0.0.1:8001/v1/vpn/state
```
