# Checklist — Zéro 429 client — réserve d'IPs prouvées

**Feature** : `022-zero-429-client-reserve`
**Date** : 2026-11-15

## Réserve

- [x] T001 — table `client_reserve` créée (migration idempotente).
- [x] T002 — une IP 200 confirmée entre dans la réserve et sort du pool probe.
- [x] T005 — réserve maintenue `>= 2` IPs prouvées (4 à 8 le 2026-11-15).
- [x] T007 — une IP consommée sort de la réserve et est remplacée.
- [x] La réserve survit à un redémarrage de l'app.

## Séparation probe/client

- [x] T003 — `StationProbe` refuse toute IP réservée.
- [x] T004 — le pool client ne sélectionne que la réserve.
- [x] T006 — budget probe respecté (`probe_budget_pct`), `probe_budget_remaining` exposé.
- [ ] 100 probes → `client_used = 0` sur la réserve (simulation seule).

## Fallback

- [x] T008 — ordre des fallbacks testé, `direct` utilisé uniquement si autorisé.
- [x] T009 — 429 JSON avec `Retry-After`, `reason` et `X-Free-Reason`, jamais vide.
- [ ] Chaque bascule est loggée avec sa cause.

## Observabilité

- [x] T010 — `client_reserve_ips`, `client_429_total{reason}`, `probe_budget_remaining`.
- [x] T011 — alerte `reserve < 2`, loggée au changement.
- [ ] Tableau de bord réserve/consommation.

## Tests

- [x] T012 — 1000 sélections simulées, réserve non vide → 0 429 (soak live borné au budget).
- [x] T013 — 100 probes simulées + clients → réserve intacte.
- [x] T014 — runbook `ops/runbooks/zero-429-reserve.md`.
- [ ] Test de non-régression : les 6 stations restent `client_usable`.

## Preuves live

- [x] `client_429_total = 0` sur une fenêtre de charge avec réserve (12/12 200, 12:24–12:31).
- [x] Réserve `>= 2` pendant 10 min (4 à 8 IPs, 12:25–12:35).
- [x] Un 429 client porte toujours un `Retry-After` (`Retry after 85s.` observé).
- [ ] Aucune probe sur une IP réservée dans les logs.

## Alignement projet

- [x] T015 — aucune station `client_usable` recréée pendant le remplissage (`_refresh_allowed`).
- [x] T016 — aucune IP `probe-format-poisoned` dans la réserve.
- [x] T017 — 6/6 sur un même cycle (12:35:04) et `warm_pool >= 6` (8 IPs) ; encore fluctuant.
- [x] T018 — options effectives (API dashboard, DB partagée), backup `client_reserve`.
- [ ] Direct utilisé uniquement si la configuration l'autorise (`direct_allowed`/`proxy_mode`).
- [ ] Pas de septième station.
- [ ] Aucune fenêtre console pendant le remplissage.

## Angles morts production

- [x] T019 — sans objet : la garde de rotation libère le refill sans shadow ni 7ᵉ station.
- [x] T020 — `client_budget_pct` respecté, aucune IP servie jusqu'à épuisement.
- [x] T021 — TTL 3600 s (`RESERVE_TTL_S`), 0 IP périmée servie.
- [x] T022 — sans objet : `_refresh_allowed` empêche le rebind d'une IP servie (test dédié).
- [x] T023 — chaos + pas CI bloquant « Contrat zéro 429 » (6/6 live manuel).
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
