# PLAN v10 — ÉTAT CONSOLIDÉ DURABLE (snapshot 2026-08-25 10:30)

> ⚠️ Copie durable dans le dépôt : le fichier `.claude/plans/j-aimerais-que-tu-me-agile-quail.md`
> subit des écrasements par un écrivain concurrent (session parallèle sur le même projet,
> même mode de défaillance que config.yaml — cf. ADR-005). Ce fichier fait foi en cas de divergence.

## ⚡ ÉTAT — 2026-08-25 10:30

Production : flotte **4/4 connectée**, conteneurs healthy, uptime st1 = 7h+ (tenu toute la nuit). Proxy v10 en route (PID relancé proprement, mode --gui). Suite ~740 tests EXIT=0, ruff 0, drift bloquant actif (5 familles : v1/conversion/clients/docker/provider).

| Volet | État |
|---|---|
| Lots ingénierie −1 → 6 | ☑ tous verts |
| Docs §11.1-11.6 (+ ADR-001→005) | ☑ |
| Déploiement §13 | ☑ (observation J+1, très bon pronostic) |
| Features §12.1 + §12.2 | ☑ intégral |
| Annexe §9 prio | 🔄 item 2 ☑ ; restent 5 (MAX_BODY_SIZE middleware ☑ fait Lot 7), 8 (VACUUM hebdo ☑ fait Lot 7) |

## Incidents résolus (post-mortems : ADR-005 + runbook-docker.md)

1. **Nuit 24→25** : rôle control server perdu par overwrite de credentials.env → healthcheck 401 → churn infini. Fixes : rôle restauré (format officiel wiki), upsert anti-perte, DNS plain 1.1.1.1:53, MTU 1280.
2. **Matin 25 (A)** : st3 rm cyclique — boot reconcile ré-exécuté à chaque redémarrage in-process du tray. Fix : garde `_RECONCILE_DONE_THIS_PROCESS` + période de grâce 120 s dans le reconcile.
3. **Matin 25 (B)** : `POST /api/proxy/restart` laisse le serveur mort (shutdown OK, startup jamais relancé) — contournement : kill+relaunch process frais. **Ticket ouvert P1** : investiguer server_manager.restart() en mode --gui.

## Journal condensé (détails : entrées horodatées du plan miroir)

- J0 baseline verte (6715ab9, 1d9b239) · Lot −1 sécurité/auth zéro-friction (88a9e00…) · Lot 0 filet+bench (1f9671f, 8253efb, 2e7d5b2) · Lot D golden V1 (a1bc413, 8205b86) · Lot 1 superviseur (30c6cda, 327114e) · dette ruff 52→0 (30c6cda) · Lot 3 moteur latence (0269516) · Lot 4 observabilité (45eb2ea) · Lot 2 lifecycle+chaos (5a6f9e4) · Lot 5 perf/hedging (40857d9) · Lot 6 config (072516c) · features §12.1+§9.1.2 (6e3f623) · docs provider (52c9b23) · incident nuit+matin (fe4f6fa, b329f14, 02631e6) · /metrics+body-limit+maintenance DB (9904315)

## Restant (optionnel)

- §12.2.9 géo-aware routing proactif : ☑ **déjà implémenté** ([Axe A/C] : _enforce_geo_gate pose _geo_forced_pool de façon proactive, lignes 3490-3496/3808-3809, couvert test_geo_routing.py) — texte plan périmé corrigé
- §12.3 différenciation : reporté volontairement
- Ticket P1 ouvert : server_manager.restart() in-process (mode --gui) ne relance pas le serveur — contournement opérationnel : kill+relaunch process

## Observation §13.5 — critères de clôture J+3

- [ ] healthy=4/4 stable sans intervention
- [ ] Quotas free qui montent et requêtes réelles traitées via stations
- [ ] Zéro churn healthcheck >2 restarts/10min par station
- [ ] /metrics scrapeable (Grafana ou curl périodique)
