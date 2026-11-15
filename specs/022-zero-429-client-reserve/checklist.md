# Checklist — Zéro 429 client — réserve d'IPs prouvées

**Feature** : `022-zero-429-client-reserve`
**Date** : 2026-11-15

## Réserve

- [ ] T001 — table `client_reserve` créée (migration idempotente).
- [ ] T002 — une IP 200 confirmée entre dans la réserve et sort du pool probe.
- [ ] T005 — réserve maintenue `>= 2` IPs prouvées.
- [ ] T007 — une IP consommée sort de la réserve et est remplacée.
- [ ] La réserve survit à un redémarrage de l'app.

## Séparation probe/client

- [ ] T003 — `StationProbe` refuse toute IP réservée.
- [ ] T004 — le pool client ne sélectionne que la réserve.
- [ ] T006 — budget probe respecté (`probe_budget_pct`).
- [ ] 100 probes → `client_used = 0` sur la réserve.

## Fallback

- [ ] T008 — WARP utilisé avant le 429 quand la réserve est vide.
- [ ] T009 — 429 JSON avec `Retry-After` et `reason`, jamais vide.
- [ ] Chaque bascule est loggée avec sa cause.

## Observabilité

- [ ] T010 — `client_reserve_ips`, `client_429_total{reason}`, `probe_budget_remaining`.
- [ ] T011 — alerte `reserve < 2`, critique `reserve = 0`.
- [ ] Tableau de bord réserve/consommation.

## Tests

- [ ] T012 — 1000 requêtes client, réserve non vide → 0 429.
- [ ] T013 — 100 probes + 100 clients → réserve intacte.
- [ ] T014 — runbook `ops/runbooks/zero-429-reserve.md`.
- [ ] Test de non-régression : les 6 stations restent `client_usable`.

## Preuves live

- [ ] `client_429_total = 0` sur une fenêtre de charge avec réserve.
- [ ] Réserve `>= 2` pendant 10 min.
- [ ] Un 429 client porte toujours un `Retry-After`.
- [ ] Aucune probe sur une IP réservée dans les logs.

## Alignement projet

- [ ] T015 — aucune station `client_usable` recréée pendant le remplissage.
- [ ] T016 — aucune IP `probe-format-poisoned` dans la réserve.
- [ ] T017 — 6/6 sur un même cycle et `warm_pool >= 6`.
- [ ] T018 — options GUI effectives, backup `client_reserve`, cross-platform.
- [ ] Direct utilisé uniquement si la configuration l'autorise (`direct_allowed`/`proxy_mode`).
- [ ] Pas de septième station.
- [ ] Aucune fenêtre console pendant le remplissage.

## Angles morts production

- [ ] T019 — `reserve_target = 6` et probes shadow sans station immobilisée.
- [ ] T020 — `client_budget_pct` respecté, aucune IP servie jusqu'à épuisement.
- [ ] T021 — TTL de réserve, 0 IP périmée servie.
- [ ] T022 — claim atomique, aucun doublon sous concurrence.
- [ ] T023 — chaos tests et nightly `slow` verts.
- [ ] SLO : p95 < 2 s, bascule < 500 ms, 0 requête bloquée > 5 s.
- [ ] Migration `client_reserve` avec backup préalable.
