# Audit — mort de l'application OpenCodeProxy

**Date** : 2026-11-15
**Périmètre** : processus `opencode.py` (API/dashboard sur `127.0.0.1:4000`), son superviseur d'app et l'environnement réseau de l'hôte.
**Objet** : expliquer comment l'application a pu mourir, distinguer les crashes réels des redémarrages opérateur, et vérifier que le comportement est corrigé.

---

## 1. Résumé exécutif

L'application n'a pas été victime d'un crash natif documenté. Deux phénomènes distincts se sont combinés :

1. **Le socket d'écoute asyncio est devenu invalide** après un changement réseau. Le processus est resté vivant, mais `accept()` a levé `OSError: [WinError 64]` et l'API a cessé de répondre (`connection refused`). C'est l'état « zombie » observé à plusieurs reprises.
2. **Aucun superviseur d'app n'était actif** après le retour à la spec 18. Une app zombie n'était donc jamais relancée, ce qui a prolongé l'indisponibilité (dernier cas : 09:23 → 09:25 le 15/11).

Les sorties `code=4294967295` (`-1`) enregistrées le 14/11 à 17:26, 17:29 et 17:30 sont corrélées aux **redémarrages manuels** effectués pour appliquer des correctifs, pas à des crashes spontanés.

---

## 2. Sources et méthode

| Source | Usage |
|---|---|
| `logs/app-supervisor.log` | chronologie des démarrages, sorties et codes de retour |
| `logs/app-stderr.log` (523 Ko) | traces asyncio, `WinError 64`, `Accept failed` |
| `logs/app-stdout.log` | avertissements de démarrage |
| `logs/debug.log` | activité pool/WARP autour des incidents |
| `Get-WinEvent Application` | crashes natifs Windows (`python.exe` / `pythonw.exe`) |
| `Get-CimInstance Win32_Process` + `Get-Process` | état, mémoire et handles des processus |
| `git log` / `git status` | corrélation avec les redémarrages et le rollback 18 |

---

## 3. Chronologie mesurée

| Horodatage | Événement | Preuve |
|---|---|---|
| 14/11 17:26:50 | pid 32900 sort `code=4294967295` après 743,5 s | `app-supervisor.log` |
| 14/11 17:26:53 | relance `restart#1`, pid 36172, `/health 200` après 6,5 s | `app-supervisor.log` |
| 14/11 17:29:30 | pid 36172 sort `code=4294967295` après 156,8 s | `app-supervisor.log` |
| 14/11 17:29:33 | relance `restart#2`, pid 31792, `/health 200` après 7,5 s | `app-supervisor.log` |
| 14/11 17:30:17 | pid 31792 sort `code=4294967295` après 43,7 s | `app-supervisor.log` |
| 14/11 17:30:20 | relance `restart#3`, pid 17704 | `app-supervisor.log` |
| 15/11 09:23:51 | superviseur flotte : `control state stale — scans paused (vpn-status unavailable)` | `fresh_chain_supervisor.log` |
| 15/11 09:25:00 | même message, l'API ne répond toujours pas | `fresh_chain_supervisor.log` |
| 15/11 09:25:18 | relance manuelle de `app_supervisor.py` (pid 32528) | processus |
| 15/11 09:25:19 | app relancée (pid 28100), `/health 200` après 7,5 s | `app-supervisor.log` |

Les trois sorties `-1` du 14/11 surviennent pendant la fenêtre où l'opérateur appliquait des correctifs et redémarrait l'app (`Stop-Process`), ce qui produit habituellement un code de sortie négatif. Elles ne sont pas des crashes spontanés.

---

## 4. Signatures techniques

Dans `logs/app-stderr.log` :

```text
ERROR:asyncio:Task exception was never retrieved
future: <Task finished name='Task-781540'
  coro=<IocpProactor.accept.<locals>.accept_coro() ...>
Traceback (most recent call last):
OSError: [WinError 64] Le nom réseau spécifié n'est plus disponible
ERROR:asyncio:Accept failed on a socket
```

Comptage :

- `Accept failed on a socket` : **1**
- `WinError 64` : **2**

Aucun `MemoryError`, aucun `FATAL`, aucun `app-crash.log` (le crashguard n'existe pas dans la spec 18).

---

## 5. Cause racine

Le serveur HTTP s'appuie sur la boucle asyncio Windows (IOCP proactor). Le socket d'écoute est ouvert sur `0.0.0.0:4000`. Lorsqu'une interface réseau change d'état — montée/descente d'un tunnel, rotation WARP ou VPN, reset de route — le handle du socket d'écoute peut devenir invalide. `accept()` lève alors :

```text
OSError: [WinError 64] Le nom réseau spécifié n'est plus disponible
```

Le processus reste en vie, mais n'accepte plus aucune connexion. C'est exactement le symptôme observé : le port 4000 refuse les connexions alors que `pythonw.exe` tourne encore.

Ce n'est donc pas :

- un crash du processus (aucun événement Windows pour `pythonw.exe`) ;
- une saturation mémoire (472 Mo, 711 handles au dernier relevé) ;
- une saturation disque (`requests.db` 0,79 Go, `registry.db` 0,09 Go) ;
- un crash natif de l'app (les événements `python.exe` + `postflop_solver_py.cp311-win_amd64.pyd` concernent un autre programme, pas `opencode.py`).

---

## 6. Facteurs aggravants

1. **`[warp-rotate] on_request` en mode `vpn`** : le pool exécutait `rotate_exhausted_managed()` à chaque requête, même quand les WARP n'étaient pas l'egress. Le log apparaissait en boucle (`enabled=True mode=vpn`), ajoutant du churn réseau et des changements d'interface — le contexte typique qui invalide le socket d'écoute.
2. **Absence de superviseur d'app après le rollback 18** : `scripts/app_supervisor.py` n'existait plus dans le working tree après le retour à `5d409b9`. Une app zombie restait zombie indéfiniment.
3. **Pas de crashguard/faulthandler** en 18 : aucune pile de crash natif n'était conservée, ce qui a rendu l'audit dépendant des logs asyncio.
4. **`rotation_storm_cooldown_s=5` hors bornes** `[30, 7200]` : la valeur était clampée à 30 à chaque chargement (`vpn-config ... clamp à 30.0`), signe d'une configuration incohérente et bruit inutile dans les logs.

---

## 7. Ce qui est écarté

| Hypothèse | Verdict | Preuve |
|---|---|---|
| Crash natif de l'app | Écarté | aucun événement Windows pour `pythonw.exe` |
| `postflop_solver_py` | Écarté | autre processus `python.exe`, pas `opencode.py` |
| Mémoire insuffisante | Écarté | 472 Mo, 711 handles |
| Disque plein | Écarté | 0,79 Go + 0,09 Go |
| Sortie `-1` = crash | Écarté | corrélée aux `Stop-Process` de maintenance |
| Crashguard en cause | Écarté | absent en spec 18 |

---

## 8. Correctifs appliqués

1. **Superviseur d'app restauré** (`scripts/app_supervisor.py`) :
   - health check `/health` toutes les 0,5 s ;
   - si `/health` échoue pendant **45 s** (`HEALTH_FAIL_LIMIT_S`), le zombie est tué et l'app est relancée ;
   - logs dédiés `logs/app-supervisor.log`, stdout/stderr séparés.
   C'est le correctif qui empêche le scénario « processus vivant, port mort » de durer.
2. **Rotation WARP désactivée en mode `vpn`** (`free/pool/__init__.py`) : plus de `rotate_exhausted_managed()` par requête quand les WARP ne sont pas l'egress. Le churn réseau qui invalidait le socket est supprimé.
3. **Probe officiel apparié 1.19.5** (`free/ip_registry/adapters/probe.py`) : payload, identité et TLS de la même version.
4. **Superviseur de flotte** : pause AUTH globale supprimée, intervalle AUTH ramené à 5 s, `--start 0` sur les known-200, binds WireGuard avec `settle_s=12` / `egress_wait_s=45`, refresh sans kill des chaînes.
5. **Commité et poussé** : `3323b93` (flotte/probe) et `001e2b1` (protocole/tool calls).

---

## 9. Recommandations

1. **Démarrer `app_supervisor.py` au boot Windows** (service NSSM ou tâche planifiée), pas seulement à la main. Sans ça, un reboot laisse l'app non supervisée.
2. **Réintroduire le crashguard/faulthandler** de la spec 19 (récupérable depuis `stash@{0}`) pour conserver la pile des crashes natifs.
3. **Recréer le socket d'écoute sur `WinError 64`** : soit un handler asyncio qui relance le serveur, soit accepter que `app_supervisor.py` fasse le restart (état actuel, 45 s de latence).
4. **Garder `rotate_exhausted_managed()` hors du mode `vpn`** (fait) et remettre `rotation_storm_cooldown_s` dans `[30, 7200]`.
5. **Surveiller `requests.db`** : 0,79 Go, rétention à vérifier ; un checkpoint WAL long peut bloquer l'event loop.
6. **Alerter sur `/health` absent > 60 s** côté superviseur de flotte (déjà partiellement couvert par `control state stale`).

---

## 10. Annexes

### 10.1 Extrait `app-supervisor.log`

```text
2026-11-14T17:26:50Z pid=32900 sorti code=4294967295 après 743.5s; redémarrage dans 2s
2026-11-14T17:29:30Z pid=36172 sorti code=4294967295 après 156.8s; redémarrage dans 2s
2026-11-14T17:30:17Z pid=31792 sorti code=4294967295 après 43.7s; redémarrage dans 2s
2026-11-15T09:25:19Z démarrage pid=? (restart#0)
2026-11-15T09:25:26Z pid=28100 /health 200 après 7.5s
```

### 10.2 Extrait `app-stderr.log`

```text
ERROR:asyncio:Task exception was never retrieved
future: <Task finished name='Task-781540' coro=<IocpProactor.accept.<locals>.accept_coro() ...>
OSError: [WinError 64] Le nom réseau spécifié n'est plus disponible
ERROR:asyncio:Accept failed on a socket
```

### 10.3 Événements Windows écartés

```text
15/11/2026 09:27:36  Application Error 1000
Nom de l'application défaillante : python.exe
Nom du module défaillant : postflop_solver_py.cp311-win_amd64.pyd
```

Ce processus n'est pas `opencode.py` (qui tourne sous `pythonw.exe`).
