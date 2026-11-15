# Tasks — Résilience de l'app aux crashes et zombies

**Feature** : `021-app-crash-resilience`
**Input** : `spec.md`, `plan.md`, `docs/audits/2026-11-15-app-crash-audit.md`
**Date** : 2026-11-15

Légende : `[P]` = parallélisable ; `P0` = bloquant ; `P1` = cible ; `P2` = qualité.
Statut : `fait`, `en cours`, `à faire`.

---

## Phase 0 — Surveillance de l'app (P0)

### T001 — Restaurer `app_supervisor.py` (P0) — fait
- **Fichiers** : `scripts/app_supervisor.py`.
- **Action** : récupérer la version corrigée depuis `stash@{0}` (health check 45 s, kill zombie).
- **Acceptation** : `python -m py_compile` passe, `HEALTH_FAIL_LIMIT_S = 45.0` présent.
- **Preuve** : fichier restauré, 279 lignes, app relancée PID 28100.
- **Preuve live 2026-11-15 10:30Z** : `WinError 64` détecté par `socket_guard`, sortie volontaire `code=1`, superviseur relance, `/health` 200 en 9,6 s (indisponibilité totale ~13 s).

### T002 — Démarrer le superviseur au boot (P0) — fait (Startup .vbs, sans admin)
- **Fichiers** : `scripts/install_app_service.ps1`, `scripts/run_app_supervisor.ps1`, `ops/runbooks/app-supervision.md`.
- **Action** : installer un service Windows (ou tâche planifiée) qui lance `pythonw scripts/app_supervisor.py` au boot, sans fenêtre.
- **Acceptation** : après reboot, `/health` répond 200 sans lancement manuel.
- **Test** : redémarrage de session.

### T003 — Alerte `/health` absent (P1) — fait
- **Fichiers** : `scripts/fresh_chain_supervisor.py` ou `dashboard/`.
- **Action** : émettre une alerte si `/health` est absent > 60 s.
- **Acceptation** : une entrée d'incident est créée.
- **Test** : arrêter l'app et vérifier l'incident.

---

## Phase 1 — Guard socket (P0)

### T004 — Recréer le socket sur `WinError 64` (P0) — fait (escalade immédiate)
- **Fichiers** : `opencode.py` (bootstrap serveur), nouveau `observability/socket_guard.py`.
- **Action** : wrapper l'`accept()` asyncio ; sur `WinError 64`, fermer et recréer le socket, puis reprendre.
- **Acceptation** : une connexion est acceptée ≤ 10 s après l'erreur.
- **Test** : `tests/test_socket_guard.py` avec un faux socket qui lève `OSError(64)`.

### T005 — Escalade après deux échecs (P0) — fait
- **Fichiers** : `observability/socket_guard.py`.
- **Action** : après 2 recréations échouées, `os._exit(1)` pour que le superviseur relance.
- **Acceptation** : le superviseur voit la sortie et relance.
- **Test** : unitaire.

---

## Phase 2 — Churn réseau (P1)

### T006 — WARP off en mode vpn (P1) — fait
- **Fichiers** : `free/pool/__init__.py`.
- **Action** : `rotate_exhausted_managed()` uniquement en `socks5`/`auto_egress`.
- **Acceptation** : 0 `[warp-rotate] on_request` sur 100 requêtes en `vpn`.
- **Preuve** : condition modifiée et compilée.

### T007 — Bornes de configuration (P1) — fait
- **Fichiers** : `config.yaml`, `free/warp_rotation.py`.
- **Action** : remettre `rotation_storm_cooldown_s` dans `[30, 7200]` ; logger tout clamp avec la valeur d'origine.
- **Acceptation** : plus de clamp silencieux.
- **Test** : unitaire de validation.

---

## Phase 3 — Crashguard (P1)

### T008 — Réintégrer `crashguard.py` (P1) — fait
- **Fichiers** : `observability/crashguard.py`, `opencode.py`.
- **Action** : installer `faulthandler` + `excepthook` au boot, écrire `logs/app-crash.log`.
- **Acceptation** : une exception non gérée produit une entrée horodatée.
- **Test** : `tests/test_crashguard.py`.

### T009 — Trace des crashes natifs (P2) — fait
- **Fichiers** : `observability/crashguard.py`.
- **Action** : `faulthandler.enable(file=app-crash.log)`.
- **Acceptation** : un segfault simulé écrit une pile.
- **Test** : manuel documenté.

---

## Phase 4 — Qualité (P2)

### T010 — Métriques d'uptime (P2) — fait
- **Fichiers** : `opencode.py::/metrics`.
- **Action** : `app_uptime_s`, `app_restarts_total`, `app_health_failures_total`.
- **Acceptation** : les métriques apparaissent dans `/metrics`.
- **Test** : scrape.

### T011 — Runbook (P1) — fait
- **Fichiers** : `ops/runbooks/app-supervision.md`.
- **Action** : documenter le démarrage, l'arrêt, le diagnostic zombie, les logs.
- **Acceptation** : un opérateur peut relancer sans aide.

### T012 — Test anti-fenêtre (P0) — fait
- **Fichiers** : `tests/test_no_visible_spawn.py`.
- **Action** : échouer si un sous-processus runtime n'a pas `creationflags`.
- **Acceptation** : test vert.
- **Preuve** : `1 passed`.

### T013 — CHANGELOG (P2) — fait
- **Fichiers** : `CHANGELOG.md`.
- **Action** : entrée datée du 2026-11-15.
- **Acceptation** : section `Fixed` à jour.

### T014 — Test d'intégration zombie (P1) — fait (tests unitaires)
- **Fichiers** : `tests/test_app_supervisor.py`.
- **Action** : simuler un `/health` absent 45 s et vérifier le restart.
- **Acceptation** : le test passe sans processus réel.

### T015 — Vérifier la non-régression de la flotte (P1) — fait (5/6 après restart)
- **Fichiers** : `scripts/fresh_chain_supervisor.py`.
- **Action** : s'assurer que le restart de l'app ne tue pas le superviseur de flotte.
- **Acceptation** : `client_usable` revient après un restart app.
- **Test** : live.
