# Runbook — supervision de l'application

**Feature** : 021-app-crash-resilience
**Date** : 2026-11-15

## Rôle

`scripts/app_supervisor.py` est le seul propriétaire du cycle de vie de
`opencode.py`. Il lance l'app, pompe stdout/stderr, vérifie `/health` toutes
les 0,5 s et remplace le processus après 45 s d'échec consécutif. Un socket
d'écoute invalidé (`WinError 64`) est traité par `observability/socket_guard.py`,
qui sort volontairement pour un redémarrage en ~2 s.

## Démarrage manuel

```powershell
pythonw scripts\app_supervisor.py
```

Logs :

- `logs/app-supervisor.log` — cycles de surveillance ;
- `logs/app-stdout.log` / `logs/app-stderr.log` — sortie de l'app ;
- `logs/app-crash.log` — faulthandler + exceptions non gérées.

## Vérification

```powershell
curl.exe -sS http://127.0.0.1:4000/health
Get-Content logs\app-supervisor.log -Tail 10
```

Attendu : `{"status":"ok"|"degraded", ...}` et un cycle `pid=... /health 200`.

## Diagnostic « port 4000 refusé »

1. `Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*opencode.py*' }`
   — le processus est-il vivant ?
2. `curl.exe http://127.0.0.1:4000/health` — le socket répond-il ?
3. Si le processus est vivant et le port refuse : c'est un zombie
   (`WinError 64`). Vérifier `logs/app-stderr.log`.
4. Le superviseur doit le remplacer sous 45 s. Sinon :
   `Stop-Process -Id <pid> -Force` puis relancer `app_supervisor.py`.

## Arrêt propre

Créer `logs/app.stop` : le superviseur termine l'enfant et sort sans le
relancer.

```powershell
New-Item logs\app.stop -ItemType File -Force
```

Supprimer le fichier avant le prochain démarrage.

## Installation au logon

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_app_service.ps1
```

La tâche planifiée `OpenCodeProxyAppSupervisor` lance le runner au logon, sans
fenêtre (`pythonw` + `WindowStyle Hidden`).

## Ne pas faire

- Ne pas lancer `python opencode.py` directement en production : sans
  superviseur, un zombie reste muet.
- Ne pas tuer le superviseur de flotte (`fresh_chain_supervisor.py`) lors d'un
  redémarrage de l'app : il doit continuer à réparer les stations.
- Ne pas ouvrir de console : tout sous-processus runtime doit porter
  `CREATE_NO_WINDOW` (test `tests/test_no_visible_spawn.py`).
