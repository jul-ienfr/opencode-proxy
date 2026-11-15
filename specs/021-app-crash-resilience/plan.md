# Implementation Plan: Résilience de l'app aux crashes et zombies

**Branch**: `021-app-crash-resilience` | **Date**: 2026-11-15 | **Spec**: [spec.md](./spec.md)

**Input**: `spec.md`, `docs/audits/2026-11-15-app-crash-audit.md`, `scripts/app_supervisor.py`, `opencode.py`, `free/pool/__init__.py`, `free/warp_rotation.py`, `core/process.py`.

## Summary

L'audit du 15/11 a isolé deux causes : un socket d'écoute asyncio invalidé par un changement réseau (`WinError 64`) qui laisse le processus vivant mais muet, et l'absence de superviseur d'app après le rollback 18. Le plan rend la détection **obligatoire** (health check 45 s), la réparation **rapide** (restart < 30 s, socket recréé), le churn réseau **disparu** (WARP off en mode vpn), et les crashes **traçables** (crashguard).

## Décision d'architecture

**Le superviseur d'app est le seul propriétaire du cycle de vie du processus.**

- `scripts/app_supervisor.py` lance `opencode.py`, pompe stdout/stderr, vérifie `/health` toutes les 0,5 s et tue le zombie après 45 s.
- `observability/crashguard.py` (récupéré du stash 19) installe `faulthandler` + `excepthook` et écrit `logs/app-crash.log`.
- `opencode.py` reste inchangé sur le plan métier ; seul le guard de socket est ajouté pour recréer l'écoute sans redémarrer.
- Le pool n'appelle `rotate_exhausted_managed()` qu'en `socks5`/`auto_egress`, jamais en `vpn`.
- La configuration est validée au chargement ; un clamp est loggé, jamais silencieux.
- Le démarrage au boot passe par un service Windows ou une tâche planifiée qui lance `app_supervisor.py`.

**Invariant** : si `/health` ne répond pas, le processus est remplacé. Aucun état métier n'est perdu : `registry.db` reste l'autorité.

## Contexte technique

- **Langage** : Python 3.12, asyncio/IOCP proactor sur Windows.
- **Serveur** : FastAPI/uvicorn-like sur `0.0.0.0:4000`.
- **Superviseur** : `scripts/app_supervisor.py` (health check, restart, logs).
- **Crashguard** : `observability/crashguard.py` (stash `spec19-wip-2026-11-14`).
- **Pool** : `free/pool/__init__.py` (WARP on_request).
- **Config** : `config.yaml` (`allocator`, `ip_rotation`, `rotation_storm_cooldown_s`).
- **Tests** : pytest, `tests/test_no_visible_spawn.py`, nouveaux tests superviseur/crashguard/socket.

## Phases

### Phase 0 — Rétablir la surveillance (P0)

1. Restaurer `scripts/app_supervisor.py` dans le dépôt (fait, récupéré du stash).
2. Vérifier `HEALTH_FAIL_LIMIT_S = 45` et le kill du zombie.
3. Démarrer `app_supervisor.py` au boot via un service Windows ou une tâche planifiée.
4. Documenter la procédure dans `ops/runbooks/app-supervision.md`.

### Phase 1 — Réparer le socket sans redémarrage (P0)

1. Ajouter un guard autour de l'`accept()` asyncio : sur `WinError 64`, fermer le socket, en recréer un, reprendre la boucle.
2. Si la recréation échoue deux fois, laisser le superviseur redémarrer le processus.
3. Tester avec une simulation de `WinError 64`.

### Phase 2 — Supprimer le churn réseau (P1)

1. `free/pool/__init__.py` : `rotate_exhausted_managed()` uniquement en `socks5`/`auto_egress` (fait).
2. Vérifier qu'aucun `[warp-rotate] on_request` n'apparaît en mode `vpn` sur 100 requêtes.
3. Remettre `rotation_storm_cooldown_s` dans `[30, 7200]` et logger les clamps.

### Phase 3 — Tracer les crashes (P1)

1. Réintégrer `observability/crashguard.py` et l'installer au démarrage de `opencode.py`.
2. Écrire `logs/app-crash.log` avec horodatage, thread, traceback.
3. Ajouter un test qui lève une exception et vérifie l'entrée.

### Phase 4 — Qualité et observabilité (P2)

1. Alerte si `/health` absent > 60 s (superviseur flotte ou dashboard).
2. Métrique `app_uptime_s`, `app_restarts_total`, `app_health_failures_total`.
3. Mettre à jour `CHANGELOG.md`, `docs/audits/2026-11-15-app-crash-audit.md` et le runbook.

## Fichiers touchés

| Fichier | Rôle |
|---|---|
| `scripts/app_supervisor.py` | health check, kill zombie, restart, logs |
| `observability/crashguard.py` | faulthandler + excepthook |
| `opencode.py` | installation crashguard, guard socket |
| `free/pool/__init__.py` | WARP off en mode vpn |
| `config.yaml` | bornes et valeurs valides |
| `tests/test_app_supervisor.py` | tests health/restart |
| `tests/test_crashguard.py` | test trace de crash |
| `tests/test_socket_guard.py` | test recréation socket |
| `ops/runbooks/app-supervision.md` | procédure |
| `CHANGELOG.md` | entrée datée |

## Tests

- **Unitaire** : health check (échec 45 s → kill), parsing des logs, guard socket.
- **Intégration** : tuer le socket et vérifier le retour à `/health 200` en < 60 s.
- **Non-régression** : `tests/test_no_visible_spawn.py` (aucune fenêtre console).
- **Live** : 100 requêtes en mode `vpn`, zéro `[warp-rotate] on_request`.
- **Boot** : redémarrage de session, `/health` 200 sans lancement manuel.

## Risques et mitigations

| Risque | Mitigation |
|---|---|
| Redémarrage en boucle si `/health` est lent | seuil 45 s, backoff 2 → 15 s, log de chaque cycle |
| Perte d'état en mémoire | `registry.db` reste l'autorité ; le pool recharge au boot |
| Guard socket qui masque un crash | compteur de recréations, escalate au superviseur après 2 échecs |
| Service Windows qui ouvre une fenêtre | `CREATE_NO_WINDOW` + test `test_no_visible_spawn` |
| Crashguard qui ralentit le boot | import différé, installation en < 10 ms |

## Rollback

- Retirer le guard socket et le crashguard : l'app revient au comportement 18.
- Arrêter le service Windows et lancer `app_supervisor.py` manuellement.
- Le superviseur de flotte reste inchangé et continue de réparer les stations.
