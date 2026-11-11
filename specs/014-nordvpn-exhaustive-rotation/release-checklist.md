# Checklist de release — 014

**Feature** : `014-nordvpn-exhaustive-rotation`
**Date** : 2026-11-10
**Règle** : aucune case ne se coche sans preuve mesurée.

## 1. Code et architecture

- [x] `free/qualification` : 0 cycle, domaine pur.
- [x] Modules < 1 500 lignes (`module_size_audit` 0 violation, 712 fichiers ;
  `store.py` 1 493).
- [x] `mypy` 0 erreur sur `free/qualification`.
- [x] `ruff` 0 erreur.
- [x] `country_backoff` absent du chemin de décision.
- [x] rotations concurrentes activées (`RotationBatch`, `max_concurrent=6`).
- [x] pause globale opt-in, désactivée par défaut.

## 2. Tests et couverture

- [x] 100 % des tests qualification + dashboard passent (**522 tests**).
- [x] Suite complète hors Docker : **5 888 passed, 0 failed** (5 skips,
  1 xpass, 8 Docker désélectionnés) ; Docker : **7 passed, 1 xfail**.
- [x] `ruff check .` : **0 anomalie** (29 préexistantes corrigées).
- [x] Gate Linux 3.12 exécutée (WSL, copie native `/tmp`) : 10/10 PASS,
  411 tests, couverture 92 % ; **à relancer après le passage à 522 tests**.
- [x] Couverture ≥ 85 % (mesurée : **93 %**).
- [x] `sweep` ≥ 90 %, `station_probe` ≥ 90 %, `runtime` 100 %.
- [x] Tests de reprise après crash (`os._exit(9)` + lease).
- [x] Tests de rotation/cutover/rollback.
- [x] Tests watchdog (tunnel, DNS, 429, AUTH, probe exception).
- [x] Tests découverte (source indisponible, suppression, stale).
- [x] Test de non-régression pays.

## 3. Données

- [x] `registry.db` : backup `VACUUM INTO` vérifié.
- [x] `integrity_check` OK.
- [x] Migration des nouvelles tables testée sur copie.
- [x] `discovery_runs` peuplé, `server_count > 0` (7 070 shadow).
- [x] `station_state`, `incidents`, `repair_jobs`, `watchdog_state` peuplés
  (tests watchdog + daemon).
- [x] `provider` non vide dans les observations/tentatives.

## 4. Canary

- [ ] Une station, 100 hostnames.
- [ ] 200 promu, 429 en quota, no_egress en infra.
- [ ] Cutover p95 < 2 s avec standby.
- [ ] 0 coupure si second tunnel.
- [ ] 6/6 ≥ 99 % quand une IP 200 existe.
- [ ] Watchdog local : une panne ne touche qu’une station.
- [ ] Découverte 6 h, fallback cache/embedded.

## 5. Sécurité et conformité

- [x] 0 secret dans logs/métriques/API.
- [x] 0 jambe payante, 0 second compte.
- [x] Aucun hostname bloqué par pays.
- [x] Aucune IP partagée entre stations.
- [x] `platform_audit --strict` 0 violation.
- [ ] 0 fenêtre visible (qualification : lancement sans fenêtre ; preuve
  visuelle du balayage complet restante).

## 6. Exploitation

- [x] `runbook.md` à jour.
- [x] ADR-010 à jour.
- [x] `debt.md` : 0 P0, P1 planifiée.
- [x] `rewrite-inventory.md` à jour.
- [x] Rollback testé (cutover + migration/restore).
- [ ] Décision de release tracée dans `implementation-log.md` (après canary).

## 7. Gate

```powershell
python scripts/quality_gate.py --with-soak
```

- [x] 10/10 PASS (pytest, ruff, mypy, config, ratchet, cycles, benchmark,
  soak, coverage run, coverage report).

## 8. Signature

- Opérateur : ____________________
- Date : ____________________
- Version / commit : ____________________
- Décision : release / rollback / hold
