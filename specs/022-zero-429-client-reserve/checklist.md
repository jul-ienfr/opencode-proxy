# Checklist — Zéro 429 client — réserve d'IPs prouvées

**Feature** : `022-zero-429-client-reserve`
**Date** : 2026-11-15

## Réserve

- [x] T001 — table `client_reserve` créée (migration idempotente).
- [x] T002 — une IP 200 confirmée entre dans la réserve et sort du pool probe.
- [ ] T005 — réserve maintenue `>= 2` IPs prouvées.
- [x] T007 — une IP consommée sort de la réserve et est remplacée.
- [x] La réserve survit à un redémarrage de l'app.

## Séparation probe/client

- [x] T003 — `StationProbe` refuse toute IP réservée.
- [x] T004 — le pool client ne sélectionne que la réserve.
- [ ] T006 — budget probe respecté (`probe_budget_pct`).
- [ ] 100 probes → `client_used = 0` sur la réserve.

## Fallback

- [ ] T008 — WARP utilisé avant le 429 quand la réserve est vide.
- [ ] T009 — 429 JSON avec `Retry-After` et `reason`, jamais vide.
- [ ] Chaque bascule est loggée avec sa cause.

## Observabilité

- [x] T010 — `client_reserve_ips`, `client_429_total` dans `/metrics`.
- [x] T011 — alerte `reserve < 2`, loggée au changement.
- [ ] Tableau de bord réserve/consommation.

## Tests

- [x] T012 — 1000 sélections simulées, réserve non vide → 0 429 (soak live borné au budget).
- [x] T013 — 100 probes simulées + clients → réserve intacte.
- [x] T014 — runbook `ops/runbooks/zero-429-reserve.md`.
- [ ] Test de non-régression : les 6 stations restent `client_usable`.

## Preuves live

- [ ] `client_429_total = 0` sur une fenêtre de charge avec réserve.
- [ ] Réserve `>= 2` pendant 10 min.
- [ ] Un 429 client porte toujours un `Retry-After`.
- [ ] Aucune probe sur une IP réservée dans les logs.

## Alignement projet

- [ ] T015 — partiel : garde ≥3 usables ; 6/6 + refill exige le shadow (T019).
- [x] T016 — aucune IP `probe-format-poisoned` dans la réserve.
- [ ] T017 — 6/6 sur un même cycle et `warm_pool >= 6`.
- [ ] T018 — options GUI effectives, backup `client_reserve`, cross-platform.
- [ ] Direct utilisé uniquement si la configuration l'autorise (`direct_allowed`/`proxy_mode`).
- [ ] Pas de septième station.
- [ ] Aucune fenêtre console pendant le remplissage.

## Angles morts production

- [ ] T019 — `reserve_target = 6` et probes shadow sans station immobilisée.
- [x] T020 — `client_budget_pct` respecté, aucune IP servie jusqu'à épuisement.
- [x] T021 — TTL 3600 s (`RESERVE_TTL_S`), 0 IP périmée servie.
- [ ] T022 — claim atomique, aucun doublon sous concurrence.
- [ ] T023 — chaos tests et nightly `slow` verts.
- [ ] SLO : p95 < 2 s, bascule < 500 ms, 0 requête bloquée > 5 s.
- [ ] Migration `client_reserve` avec backup préalable.

## Capacité

- [ ] Capacité mesurée : `6 × quota_estimate × client_budget_pct`.
- [ ] Priorité client : le refresh proactif garde ≥3 usables ; à 6/6, shadow requis (T019).
- [ ] Alerte `capacity_shortfall` avant épuisement.
- [ ] Le plan ne promet pas l'infini : 429 explicite hors capacité.

## Décisions

- [ ] `reserve_target = 6` configurable.
- [ ] `direct_allowed` false par défaut, direct seulement si autorisé.
- [ ] Capacité mesurée (960 req/jour) et documentée.
- [ ] Client prioritaire sur les probes.
- [ ] Les 6 IPs actuelles entrent dans la réserve.
- [ ] 401/403 et erreurs transport ne consomment pas la réserve.
