# Research — Résilience de l'app aux crashes et zombies

**Date** : 2026-11-15
**Source principale** : `docs/audits/2026-11-15-app-crash-audit.md`

## Question

Comment l'application `opencode.py` a-t-elle pu mourir, et comment garantir qu'elle ne reste plus injoignable ?

## Faits mesurés

| Fait | Preuve |
|---|---|
| Socket d'écoute invalidé | `ERROR:asyncio:Accept failed on a socket` + `WinError 64` dans `logs/app-stderr.log` |
| Processus vivant mais muet | port 4000 `connection refused` alors que `pythonw.exe` tourne |
| Aucun superviseur d'app en 18 | `app_supervisor.py` absent après le rollback `5d409b9` |
| Indisponibilité prolongée | 09:23 → 09:25 le 15/11, relance manuelle |
| Churn réseau | `[warp-rotate] on_request enabled=True mode=vpn` en boucle |
| Config hors bornes | `rotation_storm_cooldown_s=5` clampé à 30 |
| Pas de trace native | aucun événement Windows pour `pythonw.exe` |
| Autre processus | événements `python.exe` + `postflop_solver_py` écartés |

## Décisions

1. **Le superviseur d'app est la source de vérité du cycle de vie.** `/health` est vérifié toutes les 0,5 s ; 45 s d'échec → kill + restart.
2. **Le guard socket est une première ligne de défense**, pas un substitut au superviseur. Deux échecs de recréation → sortie volontaire pour laisser le superviseur relancer.
3. **Le mode `vpn` ne touche pas aux WARP.** La rotation WARP n'a de sens qu'en `socks5`/`auto_egress`.
4. **Le crashguard est réintroduit** pour conserver la pile des crashes natifs.
5. **Le démarrage au boot est obligatoire** : un superviseur lancé à la main ne survit pas à un reboot.
6. **Les clamps de configuration sont loggés**, jamais silencieux.

## Alternatives écartées

- **Recréer le socket dans un thread séparé** : ne résout pas la perte du handle IOCP ; le guard asyncio + restart est plus fiable.
- **Un watchdog externe (NSSM) seul** : ne détecte pas le zombie (processus vivant, port mort).
- **Désactiver les rotations VPN** : hors périmètre et contraire à l'objectif free.
- **Attendre un correctif upstream asyncio** : pas de contrôle sur la version Python.

## Questions ouvertes

- Le service Windows doit-il tourner en compte système ou en session utilisateur ? (impact sur l'accès aux conteneurs Docker)
- Le guard socket doit-il être dans `opencode.py` ou dans un module `observability/socket_guard.py` ? (le plan retient le module)
- Faut-il conserver `app-crash.log` en rotation ou l'écrire dans `telemetry.db` ? (le plan retient fichier + rotation)
