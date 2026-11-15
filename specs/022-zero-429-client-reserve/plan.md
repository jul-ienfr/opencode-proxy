# Implementation Plan: Zéro 429 client — réserve d'IPs prouvées

**Branch**: `022-zero-429-client-reserve` | **Date**: 2026-11-15 | **Spec**: [spec.md](./spec.md)

**Input**: `spec.md`, `free/pool/__init__.py`, `free/quota_ledger.py`, `free/ip_registry/adapters/probe.py`, `scripts/fresh_chain.py`, `scripts/fresh_chain_supervisor.py`, `logs/registry.db`, `config.yaml`.

## Summary

Le 429 client vient d'un partage de bucket entre les probes et le trafic client. Le plan sépare physiquement les deux usages : une **réserve client** d'IPs prouvées 200, jamais utilisées par les probes, et un **pool probe** qui consomme le reste. Le pool client ne pioche que dans la réserve ; si elle est vide, il bascule sur WARP/proxys avant de renvoyer un 429 explicite.

**Honnêteté** : avec un quota fini par IP, « jamais » signifie « jamais tant que la réserve est non vide ». Le plan garantit qu'une réserve est maintenue et alerter avant zéro ; il ne peut pas créer du quota upstream.

## Décision d'architecture

**Deux pools, une seule vérité.**

- `client_reserve` : table dédiée dans `registry.db` (`ip`, `hostname`, `station`, `verified_at`, `expires_at`, `source`). Une IP n'entre qu'après un 200 réel et une confirmation.
- `probe_pool` : le `warm_pool` actuel, réservé aux probes et à la qualification.
- Le pool client (`free/pool`) ne sélectionne que `client_reserve`.
- `StationProbe` / `fresh_chain` refusent toute IP de `client_reserve` (sauf remplacement explicite d'une IP expirée).
- Le budget probe (`probe_budget_pct`, défaut 50 %) limite la consommation des probes par IP.
- Si `client_reserve` est vide, le routeur essaie WARP, puis les proxys, puis renvoie un 429 avec `Retry-After` et une cause.

## Contexte technique

- `free/pool/__init__.py` : sélection de station, `_ledger_usable`, `_warm_pool_pick`, fallback WARP/proxys.
- `free/quota_ledger.py` : bucket `(IP, jour, bucket)`, `reserve_pct` (10 % aujourd'hui).
- `free/ip_registry/adapters/probe.py` : `StationProbe`, point d'entrée des probes.
- `scripts/fresh_chain.py` : qualification, rebind, probe, écriture des 429.
- `scripts/fresh_chain_supervisor.py` : remplissage du warm pool, refresh, réparation.
- `logs/registry.db` : `quota_buckets`, `warm_pool`, `observations`, `egress_ips`.
- `config.yaml` : `ip_quota.reserve_pct`, `allocator.*`.

## Phases

### Phase 0 — Mesure et séparation (P0)

1. Ajouter `client_reserve` (migration idempotente) et une colonne `reserved_for_client` sur `warm_pool` si la table est conservée.
2. Compter `client_429_total{reason}` et `client_reserve_ips` dans `/metrics`.
3. Interdire à `StationProbe` et `fresh_chain` les IPs `client_reserve`.

### Phase 1 — Routage client vers la réserve (P0)

1. `free/pool` : `_station_usable` exige une entrée `client_reserve` valide.
2. Si la réserve est vide, ne pas router vers une IP non prouvée : passer au fallback.
3. Le client ne doit jamais recevoir un 429 si `client_reserve` contient une IP valide.

### Phase 2 — Remplissage et budget (P1)

1. Le superviseur remplit `client_reserve` en arrière-plan (200 + confirmation) sur des IPs non réservées.
2. `probe_budget_pct` : les probes ne consomment qu'une fraction du bucket ; le reste est réservé au client.
3. Une IP de réserve consommée sous le seuil sort de la réserve et est remplacée.

### Phase 3 — Fallback avant 429 (P1)

1. Ordre : `client_reserve` → WARP → proxys → direct (si autorisé) → 429.
2. Chaque bascule est loggée avec sa cause.
3. Le 429 final porte `Retry-After` (reset le plus proche) et `reason`.

### Phase 4 — Observabilité et alertes (P1)

1. `client_reserve_ips`, `client_429_total{reason}`, `probe_budget_remaining`.
2. Alerte `reserve < 2`, critique `reserve = 0`.
3. Tableau de bord : réserve, consommation client, consommation probe, resets.

### Phase 5 — Tests de charge (P2)

1. 1000 requêtes client avec réserve non vide → 0 429.
2. 100 probes + 100 requêtes client → la réserve reste intacte.
3. Réserve vide + WARP disponible → 200 via WARP.
4. Réserve vide + tous fallbacks morts → 429 avec `Retry-After`.

## Fichiers touchés

| Fichier | Rôle |
|---|---|
| `free/quota_ledger.py` | `client_reserve`, budget probe, seuils |
| `free/pool/__init__.py` | routage client réserve-only, fallback |
| `free/ip_registry/adapters/probe.py` | refus des IPs réservées |
| `scripts/fresh_chain.py` | exclusion réserve, qualification |
| `scripts/fresh_chain_supervisor.py` | remplissage réserve, alertes |
| `opencode.py` | métriques client 429, fallback |
| `config.yaml` | `probe_budget_pct`, `reserve_min`, `reserve_target` |
| `tests/test_client_reserve.py` | tests unitaires |
| `tests/test_zero_429.py` | test de charge |
| `specs/022-.../` | artefacts SpecKit |

## Risques et mitigations

| Risque | Mitigation |
|---|---|
| Réserve vide malgré tout | alerte < 2, fallback WARP/proxys, 429 explicite |
| Probes qui vident la réserve | refus explicite + test |
| Réserve périmée (IP 429 après 200) | TTL court, re-vérification avant usage |
| Famine de probes (réserve trop grande) | budget probe configurable, priorité au client |
| Redémarrage qui perd la réserve | table persistée dans `registry.db` |

## Rollback

- Désactiver `client_reserve` par config (`client_reserve.enabled: false`) : le pool revient au comportement actuel.
- Conserver `warm_pool` et les probes existantes.
- Aucune migration destructive : la table peut rester vide.
