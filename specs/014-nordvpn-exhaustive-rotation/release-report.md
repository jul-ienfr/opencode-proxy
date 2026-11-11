# Rapport de qualité — feature 014

**Date** : 2026-11-10
**Gate** : `python scripts/quality_gate.py --with-soak`
**Résultat** : 10/10 PASS

## Couverture fonctionnelle

| Domaine | Preuve |
|---|---|
| File classée et claim transactionnel | `tests/test_qualification_store.py`, benchmark claim p95 |
| Score potentiel/mesuré | `tests/test_qualification_domain.py` |
| Cycle des transports | `tests/test_qualification_transport.py`, `tests/test_qualification_sweep.py` |
| Santé et diagnostic | `tests/test_qualification_readmodel.py`, `tests/test_qualification_diagnostics.py` |
| Quota par IP | `tests/test_qualification_readmodel.py`, endpoint `/quota` |
| Rotation et cutover | `tests/test_qualification_rotation*.py`, `tests/test_qualification_cutover.py` |
| Watchdog, découverte, réparation | `tests/test_qualification_watchdog.py`, `tests/test_qualification_discovery.py` |
| API dashboard | `tests/test_qualification_api.py`, routeurs extraits |
| Fautes | `tests/test_qualification_faults.py` |
| Migration/rollback | `tests/test_qualification_store.py` (`VACUUM INTO`, `integrity_check`) |
| Option-map | `tests/test_option_map.py` (27 clés qualification) |

## Mesures

- Tests : **522** (gate qualification + dashboard + option-map + pool/quota) ;
  suite complète hors Docker **5 888 passed, 0 failed**.
- Couverture : **93 %** sur `free/qualification`.
- Modules : **712** fichiers audités, 0 violation > 1 500 lignes.
- Cycles : 0 dans `free/qualification`.
- `config_audit` : 585 clés, 0 dead, 0 missing.
- Benchmark : upsert ~0,019 ms/item ; score p95 ~0,005 ms ; claim p95 ~4,3 ms.
- Soak 10 s : ~4 150 claims, pic ~0,22 MB, DB ~4,5 MB, 0 violation.
- Soak 60 s persistant : 24 850 claims, pic 0,217 MB, DB 4,523 MB,
  0 violation.
- `platform_audit --strict` : 0 violation.
- Gate Linux : Ubuntu 22.04/WSL, Python 3.12 (`uv`), copie native `/tmp`,
  **10/10 PASS**, 411 tests, 92 %. WAL SQLite refuse `/mnt/c` (DrvFs).
  Rejouable via `scripts/linux_gate_wsl.sh`.

## Blocages release

- Canary live exécuté : **2/6** puis **0/6 stations utilisables** après
  épuisement des quotas par IP, 2 incidents ouverts, 0 cutover ; preuve
  `logs/canary-014.json`. Le watchdog tourne désormais via le daemon
  (`daemon_enabled: true`, tick 900 s) et rafraîchit `station_state`.
- Soak 24 h lancé (PID 33280, sortie `logs/qualification-soak-24h.json`).
- Le superviseur de réparation tournait en `allocator mode=off` : en
  `observe`, il identifie bien `broken=[2..6]` (`draining`,
  `daily-bucket-exhausted`) ; le passage en réparation réelle reste à faire.
- Fuite de file corrigée : un hostname `claimed` par un échec reste désormais
  `pending` avec backoff ; la réparation préfère les preuves 200 au backlog
  non mesuré.
- `POST /api/egress/qualification/rotate` : dry-run livré, `apply=true` non branché.
- Slices de démantèlement manager/pool/fresh_chain/opencode restantes.

## Décision

La feature est **prête pour le canary**, pas pour la clôture : tous les gates
statiques et fonctionnels passent, les preuves live manquent encore.

## Mise à jour terrain (2026-11-11)

- **Dedicated IP** : cause racine du `no_egress` massif identifiée. Les
  hostnames `legacy_dedicated_ip` ne peuvent pas aboutir avec un compte
  standard (WireGuard `running`, `rx_packets=0`, `public_ip=""`). 1 538
  lignes passées `skipped/dedicated` ; aucun Dedicated IP ne reste
  `pending`. `upsert_candidates` applique désormais les skips durs
  (`dedicated`, `denylisted`, `unsupported-transport`,
  `removed-from-catalog`) sans écraser `operator-paused` ni un statut mesuré.
- **Arrêt propre** : `run_epoch` ferme l'époque en `cancelled`, relâche le
  host en cours (`claimed`/`probing` → `pending`) ; le manager ferme les
  époques orphelines de sa station au tick suivant
  (`close_running_epochs("qualifier-N-")`).
- **Compteurs** : les colonnes `tested` / `usable` / `quota` / `infra` d'une
  époque sont celles du run, plus les totaux de la flotte. Les 28 époques
  `epoch-*` restées `running` depuis le 2026-11-10 (avant correctif) ont été
  fermées en `partial`.
- **Preuve live** : qualifieur `count=1` sur la station 7, plusieurs époques
  réelles (40 à 150 hosts testés), réserve maintenue autour de 8–12 hosts
  `usable`, 200 réels sur serveurs standard (`uk2433`, `fj3`). Les 429 sont
  des quotas **par IP**, pas un épuisement du compte.
- **Limite connue** : `count=2` porte le total à 8 sessions NordVPN, seuil
  auquel une cascade `AUTH_FAILED` a déjà été observée. La GUI borne à 4 et
  avertit au-delà de `recommended_max=2` ; toute activation à 2 doit être
  surveillée via `auth_failures_30m` et la carte des stations.
- La décision de release ne change pas : **prête pour le canary**, pas pour
  la clôture ; les preuves 6/6 restent à reconstruire par la rotation.
