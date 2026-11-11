# Dette technique — état au 2026-11-10

**P0** : aucun. Les gates de release passent (pytest, ruff, mypy, config,
ratchet, cycles, benchmark, soak, couverture).

## P1 — avant clôture de la feature 014

| ID | Dette | Sortie attendue |
|---|---|---|
| D-014-01 | Canary de qualification encore rouge sur les preuves de cutover/transports (T148/T149, T030–T037) alors que la sonde live est **6/6 `model_usable`** | terminer les artefacts de cutover/qualification et ré-exécuter `logs/canary-014.json` ; ne pas confondre manifeste de release et santé live |
| D-014-02 | Soak 24 h en cours (lancé 2026-11-10 13:54, PID 33280) | rapport mémoire/disque/claims, 0 fuite, 0 retry infini |
| D-014-04 | Slices S1–S8 et S11–S13 non extraites ; S6/S7 partiels (`free/pool/selection.py`, `quota.py`, `rotation.py`, `public_pool.py`) | manager, pool, fresh_chain, opencode découpés avec parité |
| D-014-07 | Réparation live : certaines IP d'egress sont déjà épuisées avant le probe, et un hostname legacy peut être re-lié à la même IP épuisée | `claim_next` écarte désormais les candidates dont `last_egress_ip` est épuisée et la chaîne vérifie l'IP connue avant bind ; reste à couvrir les lignes legacy sans IP observée |
| D-014-09 | Boucle de contrôle quota : propriétaire de réparation inactif (`observe`), plafond de concurrence fail-open et tentatives hors ledger | **P0 appliqué le 2026-11-10** : admission fail-closed, garde ledger au dispatch, `enforce` borné ; reste à valider 6/6 en continu |
| D-014-10 | `qualification_tick` exécutait le sweep en synchrone (jusqu'à 180 s) avant chaque cycle du superviseur | **Résolu le 2026-11-10** : `daemon_max_hosts: 1` borne le sweep, tick <1 s ; `daemon_enabled: true` |
| D-014-11 | Binds simultanés au-delà de la limite de sessions NordVPN → `AUTH_FAILED` en cascade (épisode 0/6 du 2026-11-10 22:44Z) | **Résolu** : `_auth_pause_remaining` limite à **une** chaîne pendant la pause ; `auth_fail_30min` espace les démarrages de 60 s ; `rebind_limit_per_min=3` ; retour 6/6 en ~2 min |
| D-014-12 | Capacité free : 429 après 20–40 requêtes par IP partagée, session NordVPN limitée, ~1 bind utile/1–2 min → le 6/6 n'est pas stable sous trafic | **Structurel** : implémenter T076 (standby/second tunnel) ou ajouter une source d'egress ; la boucle de contrôle ne s'auto-détruit plus |

## P2 — amélioration continue

| ID | Dette | Sortie attendue |
|---|---|---|
| D-014-06 | Job CI Linux officiel non encore exécuté sur le runner GitHub | run vert archivé (la gate WSL Linux 3.12 est déjà verte) |

## Règles

- Aucune dette non listée ne doit apparaître aux gates.
- Une dette P1 bloque la clôture ; une dette P2 est planifiée.
- Chaque ligne a un critère de sortie vérifiable, pas une intention.
