# Contract — Quality gates

**Feature** : `014-nordvpn-exhaustive-rotation`
**Principe** : le système doit être le plus **fiable, performant, qualitatif,
modulable et maintenable** possible. Ces attributs sont des gates bloquants,
pas des commentaires.

---

## 1. Attributs et cibles

| Attribut | Cible | Mesure | Gate |
|---|---|---|---|
| Fiabilité | Control plane ≥ 99,9 % / 30 j ; 6/6 ≥ 99 % quand une IP 200 existe ; MTTR < 5 min | heartbeat, SLO, incidents, probes | `test_slo.py`, `test_fault_injection.py` |
| Performance | API p95 < 500 ms ; UI p95 < 2 s ; preflight 500 < 5 min ; sweep 7 070 < 24 h ; cutover p95 < 2 s ; rotation 429 < 60 s | benchmarks, soak 24 h | `test_performance.py`, `bench_qualification.py` |
| Qualité | 100 % tentatives classées ; 0 `429` en `dead` ; 0 faux `dead` ; 100 % preuves | audit SQL + tests de classification | `test_qualification_domain.py`, `test_evidence.py` |
| Modularité | 0 I/O domaine ; 0 import provider/SQLite ; modules < 1 500 lignes ; 0 cycle | import-linter, taille, complexité | `test_architecture.py` |
| Maintenabilité | Couverture ≥ 85 % domaine/application ; 100 % critique ; type/lint 0 ; `config_audit` 0 | coverage, ruff, mypy, config_audit | `test_quality_gate.py` |

---

## 2. Gates bloquants

| Gate | Commande / test | Seuil | Bloquant |
|---|---|---|---|
| Domaine pur | `test_architecture.py::test_domain_has_no_io` | 0 import interdit | oui |
| Règle de dépendance | `import-linter` | 0 cycle, 0 violation | oui |
| Taille modules | `scripts/module_size_audit.py` | ≤ 1 500 lignes nouveau ; legacy non grossi | oui |
| Complexité | `radon cc` / seuil repo | aucune fonction > seuil | oui |
| Type checking | `mypy` | 0 erreur nouveaux modules | oui |
| Lint | `ruff` | 0 erreur | oui |
| Couverture | `pytest --cov` | ≥ 85 % domaine/application ; 100 % critique | oui |
| Contract tests | `tests/contract/` | 100 % ports/adapters | oui |
| Fautes | `test_fault_injection.py` | tunnel/DNS/429/AUTH/crash/disk/source | oui |
| Performance | `bench_qualification.py` | budgets du tableau | oui |
| Soak | `test_soak_24h.py` (nightly) | mémoire/disque bornés, 0 fuite | oui |
| Config | `scripts/config_audit.py` | 0 dead / 0 missing | oui |
| Option map | `tests/test_option_map.py` | 100 % clés qualification | oui |
| Plateforme | `scripts/platform_audit.py --strict` | 0 violation | oui |
| Silencieux | test Windows | 0 fenêtre visible | oui |
| Secrets | `gitleaks` / scan repo | 0 secret | oui |
| Migration/rollback | `test_migration_rollback.py` | restore OK, `integrity_check` OK | oui |
| Rewrite ratchet | `scripts/module_size_audit.py --ratchet` | legacy non grossi ; cible < 1 500 | oui |
| Docs/ADR/debt | revue | ADR/runbook/debt à jour ; 0 P0 | oui |

Un gate rouge bloque la release ; il ne peut être contourné que par une
décision opérateur tracée dans `implementation-log.md`.

---

## 3. SLO et error budget

| SLO | Cible | Fenêtre | Alerte |
|---|---|---|---|
| Control plane disponible | 99,9 % | 30 j | burn rate > 2× |
| Heartbeat watchdog | < 2 × intervalle | 5 min | 1 heartbeat manquant |
| Sonde station fraîche | < 3 × intervalle | 15 min | 2 sondes manquantes |
| 6/6 `client_usable` | ≥ 99 % | 24 h | < 99 % 10 min |
| Cutover p95 | < 2 s | 24 h | > 2 s sur 10 rotations |
| Rotation 429 p95 | < 60 s | 24 h | > 60 s sur 10 rotations |
| Découverte | < 6 h + jitter | 24 h | run manquant |
| MTTR incident connu | < 5 min | 30 j | > 5 min |

L'error budget est calculé depuis les sondes et incidents, jamais déclaré.

---

## 4. Monolith rewrite milestones

| Monolithe | Baseline | Cible | Parité | Rollback |
|---|---:|---|---|---|
| `opencode.py` | 31 347 | composition root < 1 500 | shadow/canary | flag |
| `vpn/manager.py` | 11 385 | adapteur < 1 500 + modules `free/station/` | tests de caractérisation | flag |
| `free/pool.py` | 8 190 | < 1 500 + modules `free/pool/` | tests de caractérisation | flag |
| `dashboard/api.py` | 6 845 | routers/read models < 1 500 | mêmes réponses | flag |
| `scripts/fresh_chain.py` | 2 535 | CLI < 500 + sweep applicatif | mêmes décisions | flag |
| `fresh_chain_supervisor.py` | 1 934 | < 1 500 + watchdog/repair | mêmes décisions | flag |

Chaque slice doit : écrire les tests de caractérisation avant, passer shadow +
canary, prouver p95 ≤ +5 %, faire décroître le fichier legacy, et fermer D-02/
D-20 à la fin. Un flag permanent qui masque un échec est interdit.

---

## 5. Matrice de tests
| Niveau | Contenu | Fréquence |
|---|---|---|
| Unitaire | domaine pur, scoring, classification, quotas, transports | chaque commit |
| Contract | ports/adapters, store, binder, prober, ledger | chaque commit |
| Intégration | sweep + store + binder + prober factices | chaque PR |
| Fautes | tunnel, DNS, no_egress, 429, AUTH, crash, disque, source | chaque PR |
| Performance | benchmarks budgets | nightly |
| Soak | 24 h, mémoire/disque/files | nightly |
| Migration | copie registry.db, backup, restore | chaque PR |
| Canary | une station, un pays, six stations | release |

---

## 6. Checklist de release

- [ ] Tous les gates du §2 sont verts.
- [ ] Les SLO du §3 sont instrumentés et le dashboard est à jour.
- [ ] Le canary une station / un pays / six stations est passé.
- [ ] Le cutover p95 < 2 s et la rotation 429 < 60 s sont mesurés.
- [ ] La découverte 6 h et le fallback cache/embedded sont testés.
- [ ] Le watchdog local et le heartbeat sont testés.
- [ ] 0 `429` en `dead`, 0 pays bloqué, 0 IP partagée.
- [ ] 0 secret, 0 paid, 0 second compte.
- [ ] ADR, runbook, `debt.md`, `implementation-log.md` à jour.
- [ ] Rollback testé sur copie.
- [ ] Décision de release tracée.

---

## 7. Métriques obligatoires

- `qualification_tested`, `qualification_200`, `qualification_429`,
  `qualification_infra`, `qualification_skipped`, `qualification_queue_depth`,
  `qualification_epoch_duration_s`.
- `qualification_transport_attempts`, `qualification_transport_success`,
  `qualification_transport_degraded`.
- `qualification_station_availability_1h`, `_24h`, `six_of_six`.
- `qualification_cutover_ms`, `qualification_rotation_429_ms`.
- `watchdog_heartbeat_age_s`, `watchdog_incidents_station`,
  `watchdog_incidents_fleet`, `watchdog_repair_attempts`.
- `discovery_runs_total`, `discovery_last_success_age_s`,
  `discovery_stale`, `discovery_server_count`, `discovery_transport_count`.
- `repair_jobs_pending`, `repair_jobs_failed`, `repair_jobs_escalated`,
  `repair_mttr_s`.
- `quality_gate_failures_total`, `quality_coverage_pct`,
  `quality_debt_p0`, `quality_debt_p1`.
- `module_lines{module}`, `legacy_reduction_pct`, `parity_p95_delta_pct`,
  `extraction_rollbacks_total`, `debt_d02_open`, `debt_d20_open`.
