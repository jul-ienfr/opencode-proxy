# Tasks — Zéro 429 client — réserve d'IPs prouvées

**Feature** : `022-zero-429-client-reserve`
**Input** : `spec.md`, `plan.md`
**Date** : 2026-11-15

Légende : `[P]` = parallélisable ; `P0` = bloquant ; `P1` = cible ; `P2` = qualité.
Statut : `fait`, `en cours`, `à faire`.

---

## Phase 0 — Mesure et séparation (P0)

### T001 — Table `client_reserve` (P0) — fait
- **Fichiers** : `free/quota_ledger.py`, migration `registry.db`.
- **Action** : créer `client_reserve(ip, hostname, station, verified_at, expires_at, source)` avec index unique sur `ip`.
- **Acceptation** : migration idempotente, table lisible par le pool.
- **Test** : `tests/test_client_reserve.py::test_migration`.

### T002 — Marqueur `reserved_for_client` (P0) — fait
- **Fichiers** : `free/quota_ledger.py`, `warm_pool`.
- **Action** : une IP entre dans `client_reserve` après un 200 confirmé ; elle est retirée du pool probe.
- **Acceptation** : une IP réservée n'est jamais proposée aux probes.
- **Test** : unitaire.

### T003 — Probes interdites sur la réserve (P0) — fait
- **Fichiers** : `free/ip_registry/adapters/probe.py`, `scripts/fresh_chain.py`.
- **Action** : refuser `client_reserve` dans `StationProbe` et dans la sélection de candidats.
- **Acceptation** : 100 probes → 0 sélection d'IP réservée.
- **Test** : unitaire + compteur.

### T004 — Pool client réserve-only (P0) — fait
- **Fichiers** : `free/pool/__init__.py`.
- **Action** : `_station_usable` exige une entrée `client_reserve` valide.
- **Acceptation** : aucune requête client sur une IP non réservée.
- **Test** : `test_client_reserve.py::test_pool_reserve_only`.

---

## Phase 1 — Remplissage et budget (P1)

### T005 — Remplissage de la réserve (P1) — fait
- **Fichiers** : `scripts/fresh_chain_supervisor.py`.
- **Action** : en arrière-plan, qualifier des IPs non réservées jusqu'à `reserve_target` (proposé 6, configurable).
- **Acceptation** : `client_reserve_ips >= 2` stable.
- **Test** : live + métrique. Le 2026-11-15, la réserve est passée de 1 à 8 entre 11:19 et 12:35, sans descendre sous 4 après 12:25.

### T006 — Budget probe (P1) — fait
- **Fichiers** : `free/client_reserve.py`, `scripts/fresh_chain.py`.
- **Action** : `probe_budget_pct` (défaut 50 %) ; les probes ne consomment pas plus.
- **Acceptation** : `client_used` reste 0 sur les IPs réservées.
- **Test** : `test_probe_budget_blocks_half_spent_ip`, `test_probe_budget_remaining_counts_reserve_only`. Les probes excluent la réserve **et** les IPs à 50 % de leur quota ; métrique `probe_budget_remaining`.

### T007 — Remplacement d'une IP consommée (P1) — fait
- **Fichiers** : `free/client_reserve.py`, `free/pool/__init__.py`, superviseur.
- **Action** : une IP de réserve dont le bucket tombe sous le seuil sort et est remplacée.
- **Acceptation** : la réserve ne sert jamais une IP épuisée.
- **Test** : unitaire (`test_zero_429.py`). Sortie immédiate sur 429 actif, budget atteint ou poison ; remplacement par le prochain 200 du superviseur.

---

## Phase 2 — Fallback avant 429 (P1)

### T008 — Ordre de fallback (P1) — fait
- **Fichiers** : `free/pool/__init__.py`, `opencode.py`.
- **Action** : réserve → WARP → proxys → direct (si autorisé) → 429.
- **Acceptation** : un WARP disponible est utilisé avant le 429.
- **Test** : `test_direct_provider_is_gated` vérifie l'ordre et le fait que `direct` est exclu sauf si `proxy_mode=direct` ou `direct_allowed=1` ; `test_reason_reports_dead_fallbacks` / `test_reason_reports_upstream_429` prouvent les trois issues (`reserve_empty`, `all_fallbacks_dead`, `upstream_429`).

### T009 — 429 explicite (P1) — fait
- **Fichiers** : `core/errors.py`, `opencode.py`.
- **Action** : 429 JSON avec `Retry-After` exact et `reason` (`reserve_empty`, `all_fallbacks_dead`, `upstream_429`).
- **Acceptation** : aucun 429 vide.
- **Test** : `test_quota_error_carries_a_reason` + en-tête `X-Free-Reason` + compteur `client_429_total{reason}`.

---

## Phase 3 — Observabilité (P1)

### T010 — Métriques (P1) — fait
- **Fichiers** : `opencode.py::/metrics`.
- **Action** : `client_reserve_ips`, `client_429_total{reason}`, `probe_budget_remaining`.
- **Acceptation** : métriques visibles.
- **Test** : scrape.

### T011 — Alertes (P1) — fait
- **Fichiers** : `scripts/fresh_chain_supervisor.py`, `observability/incidents.py`.
- **Action** : alerte `reserve < 2`, critique `reserve = 0`.
- **Acceptation** : incident ouvert.
- **Test** : simulation.

---

## Phase 4 — Tests de charge (P2)

### T012 — 1000 requêtes client (P2) — fait (simulation)
- **Fichiers** : `tests/test_zero_429.py`.
- **Action** : réserve non vide, 1000 sélections, compter les 429.
- **Acceptation** : 0 429.
- **Test** : `test_thousand_client_picks_stay_in_reserve`. Un burst réel de 1000 dépasse la capacité (~960/jour) : le live est un soak borné, pas 1000 d'un coup.

### T013 — 100 probes + 100 clients (P2) — fait (simulation)
- **Fichiers** : `tests/test_zero_429.py`.
- **Action** : vérifier que les probes ne touchent pas la réserve.
- **Acceptation** : `client_used` = 0 sur la réserve.
- **Test** : `test_hundred_probes_never_touch_the_reserve`.

### T014 — Runbook (P2) — fait
- **Fichiers** : `ops/runbooks/zero-429-reserve.md`.
- **Action** : décrire la réserve, les alertes, le remplacement, le fallback.
- **Acceptation** : un opérateur sait quoi faire quand `reserve < 2`.

## Phase 5 — Alignement explicite (P1)

### T015 — Migration douce (P1) — fait
- **Fichiers** : `scripts/fresh_chain_supervisor.py`.
- **Action** : le remplissage de réserve ne recrée jamais une station `client_usable` ; il attend une station libre.
- **Acceptation** : aucune station saine recréée pendant un remplissage.
- **Test** : `test_rotation_never_takes_a_reserved_ip`. La garde `_refresh_allowed` protège les IPs servables ; le 6/6 du 2026-11-15 a tenu avec 8 IPs en réserve.

### T016 — Exclusion des IPs grillées (P1) — fait
- **Fichiers** : `free/client_reserve.py`.
- **Action** : une IP `probe-format-poisoned` (24 h) ne peut pas entrer dans la réserve.
- **Acceptation** : 0 IP poison dans `client_reserve`.
- **Test** : unitaire (`test_zero_429.py`). Un bucket qui a répondu 200 après le poison n'est pas grillé.

### T017 — Critère 6/6 simultané (P1) — fait (observé, encore fluctuant)
- **Fichiers** : `observability/fleet_slo.py` (ou équivalent), `scripts/fresh_chain_supervisor.py`.
- **Action** : mesurer `client_usable` sur le même cycle ; exiger 6/6 et `warm_pool >= 6`.
- **Acceptation** : le SLO n'est validé que sur un échantillon simultané.
- **Test** : échantillon live. Le 2026-11-15 à 12:35:04, un même `/api/vpn-status` a renvoyé **6/6 `client_usable`** avec **8 IPs en réserve**. La flotte a reflué à 4/6 trente secondes plus tard : le critère est atteint, pas encore tenu en permanence.

### T018 — Options, backup, cross-platform (P2) — fait (API dashboard)
- **Fichiers** : `free/reserve_settings.py`, `dashboard/routers/free_routes.py`, `free/client_reserve.py`, `scripts/fresh_chain_supervisor.py`.
- **Action** : `enabled`, `target`, `min`, `ttl_s`, `client_budget_pct`, `probe_budget_pct` persistés dans `registry.db` (partagés app/superviseur), avec précédence env > GUI > défaut. Route `GET/POST /api/free/client-reserve` (token dashboard) + tests.
- **Pourquoi pas `config.yaml`** : le runtime réécrit ce fichier et perd les clés hors schéma (constaté deux fois). Une base partagée est la seule source fiable pour deux processus.
- **Acceptation** : `config_audit --strict` sans option morte (0 dead, 0 missing), backup `VACUUM INTO` contient `client_reserve` et `client_reserve_settings`.

## Phase 6 — Angles morts production (P1)

### T019 — Qualification shadow sans station de réserve (P1) — sans objet
- **Fichiers** : `scripts/fresh_chain.py`, `scripts/fresh_chain_supervisor.py`.
- **Action** : les probes de remplissage utilisent le mode shadow ; aucune station de réserve n'est immobilisée.
- **Pourquoi sans objet** : le besoin venait du refresh proactif à 6/6. Depuis `_refresh_allowed`, le superviseur ne rebinde que les IPs **non servables** ; une IP qui atteint son budget sort de la réserve et libère sa station. Le refill reprend donc exactement quand il faut, sans 7ᵉ station ni connexion éphémère. À 6/6 sans IP épuisée, ne pas sonder est le comportement voulu : sonder brûlerait le quota client.

### T020 — Budget client par IP (P1) — fait
- **Fichiers** : `free/client_reserve.py`.
- **Action** : `client_budget_pct` (80 %) ; une IP sort de la réserve sous le seuil.
- **Acceptation** : aucune IP servie jusqu'à l'épuisement.
- **Test** : unitaire (`test_zero_429.py`). `CLIENT_BUDGET_PCT` × `CLIENT_QUOTA_ESTIMATE` = 160 requêtes/IP par défaut.

### T021 — TTL et re-vérification (P1) — fait (TTL corrigé à 3600 s)
- **Fichiers** : `free/client_reserve.py`.
- **Action** : `reserve_ttl_s` (défaut **3600 s**, `RESERVE_TTL_S`) ; une IP plus vieille sort. Le 300 s initial a été écarté : 6 IPs × 12 re-probes/h ≈ 1 700 probes/jour, incompatible avec le quota free.
- **Acceptation** : 0 IP périmée servie.
- **Test** : unitaire (`test_zero_429.py`).

### T022 — Claim atomique (P1) — sans objet (remplacé par la garde de rotation)
- **Fichiers** : `scripts/fresh_chain_supervisor.py`.
- **Action initiale** : `BEGIN IMMEDIATE` + `UPDATE ... RETURNING` ; une IP = un client à la fois.
- **Décision** : « un client par IP » sérialiserait les requêtes et n'apporte rien à l'HTTP amont. Le risque réel est qu'une IP soit rebindée **pendant** qu'elle sert ; `_refresh_allowed` l'interdit et `test_rotation_never_takes_a_reserved_ip` le prouve. Un claim par requête pourra revenir si un besoin de comptabilité exacte apparaît.

### T023 — Chaos et CI (P2) — fait (live 6/6 manuel)
- **Fichiers** : `tests/test_zero_429.py`, `.github/workflows/ci.yml`.
- **Action** : tests IP qui tombe, WARP mort, DB lock ; nightly `slow`.
- **Acceptation** : les tests passent et la non-régression 6/6 est vérifiée.
- **État** : DB corrompue, DB verrouillée et WARP mort couvertes. Pas de nightly : les tests 022 sont rapides et tournent dans un pas CI bloquant dédié (`Contrat zéro 429`). La non-régression 6/6 reste une vérification live (runbook), pas un test CI : elle dépend du quota upstream.
