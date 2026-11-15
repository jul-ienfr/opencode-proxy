# Checklist — Résilience de l'app aux crashes et zombies

**Feature** : `021-app-crash-resilience`
**Date** : 2026-11-15

## Surveillance

- [x] T001 — `app_supervisor.py` restauré (health check 45 s, kill zombie).
- [ ] T002 — superviseur lancé au boot (service/tâche planifiée).
- [ ] T003 — alerte si `/health` absent > 60 s.
- [ ] Redémarrage après 45 s d'échec vérifié sur un processus réel.
- [ ] `/health` 200 en < 30 s après restart.

## Socket

- [ ] T004 — `WinError 64` sur `accept()` recrée le socket.
- [ ] T005 — escalade après 2 échecs de recréation.
- [ ] Test `tests/test_socket_guard.py` vert.
- [ ] Aucune fenêtre console pendant la recréation.

## Réseau

- [x] T006 — `rotate_exhausted_managed()` absent en mode `vpn`.
- [ ] T007 — `rotation_storm_cooldown_s` dans `[30, 7200]`.
- [ ] 0 `[warp-rotate] on_request` sur 100 requêtes en `vpn`.
- [ ] Chaque clamp de config est loggé.

## Crashs

- [ ] T008 — crashguard installé au boot.
- [ ] T009 — `faulthandler` écrit `logs/app-crash.log`.
- [ ] Test `tests/test_crashguard.py` vert.
- [ ] Une exception non gérée produit une entrée horodatée.

## Qualité

- [ ] T010 — métriques `app_uptime_s`, `app_restarts_total`, `app_health_failures_total`.
- [ ] T011 — runbook `ops/runbooks/app-supervision.md`.
- [x] T012 — `tests/test_no_visible_spawn.py` vert.
- [ ] T013 — CHANGELOG daté.
- [ ] T014 — test d'intégration zombie.
- [ ] T015 — `client_usable` revient après un restart app.

## Preuves live

- [ ] `/health` répond 200 après un reboot.
- [ ] 10 min de trafic sans `Accept failed`.
- [ ] 6/6 `client_usable` après un restart app.
- [ ] `logs/app-supervisor.log` contient un cycle complet (démarrage, sortie, restart).
- [ ] Aucun événement Windows `pythonw.exe` en erreur.
