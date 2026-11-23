# Checklist — Audit et optimisation de la flotte free

**Feature** : `019-audit-optimisation-flotte-free`
**Date** : 2026-11-14
**Références** : `audit.md`, `spec.md`, `plan.md`, `tasks.md`

## Audit (fait, lecture seule)

- [x] Documentation inventoriée (`AGENTS.md`, 017, 018, `.specify`, runbook,
      README, Compose, config).
- [x] État live mesuré (`/api/vpn-status`, `docker ps`, `/health`).
- [x] Registre interrogé en lecture seule (`warm_pool`, `station_state`,
      `quota_buckets`, `claims`, `qualification_attempts`, `incidents`).
- [x] Logs analysés (superviseur, debug, stderr app, pauses AUTH).
- [x] Git des deux dépôts vérifié (47 commits/12 h, fork 09:28Z).
- [x] Aucune rotation, aucun redémarrage, aucune écriture de code.
- [x] Constats P0/P1/P2 avec preuve, cause, impact, recommandation, effort,
      risque, critère d'acceptation.

## Mesure (avant clôture)

- [ ] `station_client_usable = 6` sur un même cycle de 120 s.
- [ ] Tenue 30 min consécutives, mesurée par la requête §9.2 (pas le dashboard).
- [ ] `warm_pool_live ≥ 6` sur la même fenêtre.
- [ ] `warm_pool_used / warm_pool_consumed ≥ 0,8`.
- [ ] Taux de 200 > 30 % sur 1 h.
- [ ] `no_egress` dans `claims` < 30 %.
- [ ] `bind_error` < 20 %.
- [ ] 100 % des resets respectés sur 24 h (requête de contrôle = 0).
- [ ] 0 `code reload` sur 15 min (delta du log).
- [ ] `rotate_committed / rotate_total > 50 %`.
- [ ] 0 `rotation_jobs` en `started` depuis plus de 5 min (T324).
- [ ] Latence rotation p95 < 240 s.
- [ ] ≤ 5 incidents/heure hors panne réelle.
- [ ] App uptime 99,9 % sur 24 h, test de kill réussi (< 30 s).

## Propriété de l'état

- [ ] `station_state` n'a qu'un écrivain (l'app).
- [ ] Un seul fichier contient IP/pin (`registry.db`).
- [ ] `.env` est généré, jamais édité à la main.
- [ ] Test de non-divergence API/registre/fork présent et vert.
- [ ] Aucun `vpn_state*.json` actif comme source d'état.
- [ ] Aucune écriture de `client_usable` par la qualification.

## Données

- [ ] `registry.db` < 50 Mio.
- [ ] `warm_pool` ne contient que des lignes vivantes.
- [ ] `registry.db-wal` < 4 Mio.
- [ ] `requests.db` < 500 Mio, zéro ligne > 7 j.
- [ ] `.bak` de 1,3 Go archivé ou supprimé après contrôle.
- [ ] `PRAGMA integrity_check = ok` après purge.
- [ ] Sauvegarde testée avant purge.

## Fiabilité

- [ ] App supervisée (service), `restart=always`, stdout+stderr capturés.
- [ ] Pause AUTH à écrivain unique, test de concurrence vert.
- [ ] Un incident flotte pour N stations dans la même minute.
- [ ] T211 DNS transitoire cochée ; aucun teardown sur DNS seul.
- [ ] `rotate_timeout_s` aligné sur `rotation_max_duration`.
- [ ] File de rotation visible (`position`, `eta_s`).

## Sécurité

- [ ] Dashboard : 401 sans token sur les routes de contrôle.
- [ ] Proxy : `client_auth` actif hors loopback.
- [ ] Aucun secret dans `config.yaml` ni dans les logs.
- [ ] Aucune écoute publique non authentifiée.
- [ ] `.env.bak-*` anciens non exposés (au moins 20 fichiers à la racine).

## Exploitation

- [ ] Six stations exactement (`docker compose config`).
- [ ] Une seule image par station, digest identique.
- [ ] Aucune station saine recréée pour une migration.
- [ ] Rollback testé pour : lanceur app, source `station_state`, modèle quota,
      warm pool, rotation async.
- [ ] WARP borné à 2–3, CPU hôte < 70 %.
- [ ] `git status` propre hors livrables 019.

## Qualité

- [ ] `_auth_gate` testé (existant).
- [ ] `_best_resume_station` testé.
- [ ] `_refresh_warm_pool` testé.
- [ ] `_station_settling` testé.
- [ ] `_remove_extra_stations` testé.
- [ ] `ruff` et `mypy` verts.
- [ ] CI bloquante sur les chemins superviseur.
- [ ] Aucun revert d'urgence nécessaire (pas de `8b493dd` bis).

## Contraintes non négociables (vérifiées)

- [ ] Aucune proposition payante ni second compte.
- [ ] « Attendre » n'est jamais la seule réponse.
- [ ] Six stations maximum.
- [ ] Une station saine n'est jamais recréée pour une mise à jour.
- [ ] Le superviseur n'est jamais gelé.
- [ ] `app/protocol/tool_aliases.py` inchangé.
- [ ] Réglages WARP de `config.yaml` inchangés.
- [ ] Aucune rotation avant `reset_at`.
- [ ] Aucune IP venue d'un 429 re-testée avant son reset.

## Clôture

- [x] `.specify/feature.json` pointe sur 019.
- [x] `audit.md`, `spec.md`, `plan.md`, `tasks.md`, `checklist.md` présents.
- [x] Re-mesure de clôture 11:22Z consignée dans `audit.md` §2.8
      (0/6, warm_pool=15, 6/6 DRAINING, spam corrigé à 98 003 lignes).
- [ ] Toutes les tâches P0 sont `fait`.
- [ ] Les tâches P1 restantes sont documentées avec un propriétaire.
- [ ] Rapport final : chiffres avant/après, preuves SQL, décisions.