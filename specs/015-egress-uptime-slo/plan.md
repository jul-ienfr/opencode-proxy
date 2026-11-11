# Implementation Plan: Disponibilité mesurée et bascule rapide des egress

**Branch**: `015-egress-uptime-slo` | **Date**: 2026-11-11 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/015-egress-uptime-slo/spec.md` et
audit exhaustif `specs/015-egress-uptime-slo/audit.md`.

## Summary

Le proxy sert déjà les modèles free, mais les six stations NordVPN sont à
0/6 `client_usable` parce que quatre boucles se cumulent : un gagnant frais est
effacé par le nettoyage des pins, une panne AUTH OpenVPN gèle toute la flotte
900 s, deux réparateurs se disputent la même station, et chaque candidat brûle
~22 requêtes de quota avant d'être prouvé. Le plan attaque le problème en trois
propriétés, dans cet ordre :

1. **Cohérence** — un seul gagnant, un seul réparateur, une seule vérité de
   disponibilité, un ledger qui ne ressuscite pas une IP épuisée.
2. **Indépendance** — une station, un transport ou une IP ne peut plus geler
   les autres ; les pauses sont locales, bornées et décroissantes.
3. **Vitesse** — warm pool d'IP N+2 préqualifié, cutover make-before-break,
   sondes à zéro quota, mesure du SLO pour prouver le 99,99 %.

Le plan est exécuté par strangler : chaque brique est ajoutée à côté du chemin
existant, testée en shadow/canary, puis branchée. `registry.db` reste
l'autorité. Aucune jambe payante, aucun second compte, aucune attente.

**Contrainte de taille (opérateur, 2026-11-11)** : exactement `station_count`
stations (6). Le système ne crée, ne répare et ne compte **jamais** une
septième station. `target_slots` vaut 6, le canary n'est pas une septième
session, et le warm pool est une réserve d'**IP** pour ces six stations — pas
des stations supplémentaires. WARP/proxys sont des egress de secours du
service, hors 6/6. Dès qu'une des six stations n'est plus `client_usable`, sa
rotation démarre automatiquement, sans attendre les autres.

## Technical Context

**Language/Version**: Python 3.12, SQLite 3 WAL, asyncio.

**Primary Dependencies**: `vpn/manager.py`, `vpn/auth.py`, `station_repair.py`,
`free/station_health.py`, `free/ip_registry/` (domain, registry_db, binders,
probes), `free/ip_quota.py`, `free/quota_ledger.py`, `free/catalog_claims.py`,
`free/warm_pool.py`, `free/rotation_planner.py`, `free/repair_queue.py`,
`free/egress_*`, `free/qualification/`, `scripts/fresh_chain.py`,
`scripts/fresh_chain_supervisor.py`, `opencode.py`, `dashboard/`.

**Storage**: `logs/registry.db` — autorité unique. Tables concernées :
`stations`, `station_state`, `egress_ips`, `leases`, `quota_buckets`,
`warm_pool`, `repair_requests`, `incidents`, `observations`,
`qualification_*`, `egress_options`, `egress_sources`, `egress_catalog_meta`.
`logs/station_slo.jsonl` reste un journal brut append-only, pas une seconde
vérité.

**Testing**: `pytest` ; tests de domaine purs, tests SQLite temporaires, tests
de binders/probes avec adapters factices, tests de boucle superviseur,
tests de chaos (kill tunnel, blocage transport, épuisement quota, reboot),
soak 24 h/72 h.

**Target Platform**: Windows, Linux, macOS (constitution XV) ; processus
silencieux (XVI).

**Project Type**: application Python existante (proxy + free leg + dashboard) ;
ajout de modules de domaine et branchement progressif.

**Performance Goals**:
- Cutover avec standby : p95 ≤ 20 s, p99 ≤ 60 s.
- Détection panne tunnel : ≤ 10 s (probe active) ; décision ≤ 2 s.
- Rotation sans standby : p95 ≤ 60 s, dégradation explicite au-delà.
- Superviseur : poll 5 s ; 6 stations indépendantes ; 3–4 slots.
- Sondes : zéro quota pour la liveness ; ≤ 5 % du budget quotidien pour les
  probes modèle d'une IP serving.
- API dashboard : p95 < 500 ms ; UI p95 < 2 s.

**Constraints**:
- Six sessions NordVPN maximum ; WARP/proxys hors sessions.
- Quota free par IP ; `reset_at` fournisseur autoritaire.
- Aucune pause globale > 60 s ; aucune station ne gèle les autres.
- Aucune IP partagée entre deux stations ; claim exclusif.
- Aucune preuve, aucun succès : 200 + contenu.
- Aucune jambe payante, aucun second compte, aucune attente.
- Modules < 1 500 lignes ; domaine pur sans I/O ; pas de nouveau monolithe.
- Config : chaque clé a un consommateur et un test.

**Scale/Scope**: 6 stations, 3 transports, des milliers de hostnames NordVPN,
un warm pool de 8 IP, jusqu'à 3 egress WARP/proxy actifs, 30 jours de SLO.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principe | Vérification | Statut |
|---|---|---|
| I. Disponibilité mesurée | SLO calculé depuis `station_slo.jsonl` + `incidents` bruts ; `client_usable` = 200 avec contenu ; fenêtres 1 h/24 h/7 j/30 j. | PASS |
| II. Trois ressources | Station, session, IP ont des machines à états séparées ; le cooldown appartient à l'IP ; le lease appartient à la station. | PASS |
| III. Source de vérité | `registry.db` pour les décisions ; JSON en lecture seule ; un seul journal d'incidents. | PASS |
| IV. Reset fournisseur | `reset_at` vient du 429 ; aucune IP retestée avant ; jitter après reset ; 429 sans 200 = mauvaise IP. | PASS |
| V. IP unique par station | Claim exclusif, index unique, libération explicite, refus des doublons. | PASS |
| VI. Indépendance, transports | Cycle WG → OVPN UDP → TCP → WG par hostname ; aucune pause globale ; pas de quota pays. | PASS |
| VII. Aucune preuve, aucun succès | Promotion sur 200 + contenu ; un bind sans egress n'est pas un succès ; une IP historique doit être revalidée. | PASS |
| VIII. Autonomie, un seul réparateur | Superviseur seul propriétaire ; le pool enqueue ; le manager ne recrée pas en mode superviseur. | PASS |
| IX. Score ordonne, portes dures | Score pour l'ordre ; `reset_at`, unicité, preuve et dernière station sont des portes dures. | PASS |
| X. Dégradation explicite, free | 503 + `Retry-After`, raison et prochaine échéance ; jamais de 6/6 fabriqué ; jamais de payant. | PASS |
| XI. Fallbacks multiples | NordVPN → WARP → proxys ; binder control API → compose → docker ; DNS DoT → plain ; probe multiples sources. | PASS |
| XII. Option GUI = effet réel | Chaque clé touchée a un consommateur et un test ; `config_audit --strict`. | PASS |
| XIII. Données traçables | Migrations versionnées, index, rétention, backup, intégrité. | PASS |
| XIV. Modularité | Nouveaux modules `free/transport_ladder.py`, `free/standby.py`, `free/availability.py` ; < 1 500 lignes ; domaine pur. | PASS |
| XV. Multiplateforme | Aucun chemin OS dans le domaine ; adapters plateforme ; CI Windows/Linux. | PASS |
| XVI. Silencieux | `core.process` pour tout processus ; pas de fenêtre. | PASS |
| XVII. Gates qualité | Tests, ruff, config_audit, platform_audit, option-map, soak. | PASS |

Aucune violation. Le plan ne nécessite pas de justification de complexité.

## Architecture

### Vue d'ensemble

```text
                    ┌──────────────────────────────┐
                    │  Mesure (availability.py)     │
                    │  échantillons + incidents     │
                    └──────────────┬───────────────┘
                                   │ SLI/SLO, burn
                    ┌──────────────▼───────────────┐
                    │  Décision (health + score)    │
                    │  client_usable, priorité      │
                    └──────────────┬───────────────┘
                                   │ besoins
        ┌──────────────────────────▼──────────────────────────┐
        │  Superviseur (seul réparateur, 6 stations, N slots)  │
        └───────┬───────────────────────┬─────────────────────┘
                │                       │
    ┌───────────▼──────────┐   ┌────────▼─────────────┐
    │ Transport ladder      │   │ Standby / warm pool  │
    │ WG→UDP→TCP→WG         │   │ claim, prove, cutover│
    └───────────┬──────────┘   └────────┬─────────────┘
                │                       │
    ┌───────────▼───────────────────────▼─────────────┐
    │  Ressources : sessions (6), IP/leases, quota     │
    │  registry.db, reset_at, /24, /64                 │
    └───────────┬───────────────────────┬─────────────┘
                │                       │
    ┌───────────▼──────────┐   ┌────────▼─────────────┐
    │ NordVPN (Gluetun)     │   │ WARP + proxylist     │
    │ 6 stations            │   │ réserve de standby   │
    └──────────────────────┘   └──────────────────────┘
```

### Décisions structurantes

#### D1 — Gagnant atomique et pin protégé

- Le winner est un enregistrement unique :
  `{station, ip, host, transport, proven_at, valid, content_hash}`.
- `_clear_stale_pins` ne peut effacer un pin que si :
  - `winner_at` est absent ou > 180 s, **et**
  - l'IP live diffère de l'IP du winner, **et**
  - aucun 200 n'a été observé dans les 180 dernières secondes.
- `_winner_for` lit d'abord le transport effectif, puis `_winner_any`, puis le
  transport configuré. Un winner `auto`, `wireguard` ou `openvpn-*` est toujours
  retrouvé.
- L'adoption (pin + lease + état) est une transaction unique dans
  `registry.db` ; le manager ne peut pas voir un pin sans lease.

**Fichiers** : `scripts/fresh_chain_supervisor.py:1314-1343`,
`scripts/fresh_chain.py:1452-1528`, `free/ip_registry/registry_db.py:111-213`,
`free/chain_state.py`.

#### D2 — Transport ladder first-class

Nouveau module pur `free/transport_ladder.py` :

```text
attempts(station, host, config) -> [wireguard, openvpn-udp, openvpn-tcp]
budget(transport) -> cycle_timeout_s
on_result(transport, error_kind) -> next decision (retry/next/stop)
breaker(station, transport) -> open/half-open/closed
```

- `error_kind` typé : `auth`, `no_egress`, `bind_error`, `timeout`, `quota`,
  `host_gone`.
- Un `auth` n'ouvre le breaker que pour OpenVPN ; WireGuard reste disponible.
- Un `quota` ne condamne pas le hostname : il condamne l'IP et passe au
  candidat suivant.
- `transport_cycle_timeout_s` et `transport_auth_backoff_s` sont consommés.
- `fresh_chain` et le manager utilisent le même module (pas de fork de logique).

**Fichiers** : nouveau `free/transport_ladder.py` ; `scripts/fresh_chain.py`
(cycle host/transport), `vpn/manager.py` (auto/emergency), `vpn/auth.py`.

#### D3 — Auth guard local, borné, décroissant

- `GlobalAuthGuard` devient `TransportAuthGuard` :
  - clé `(transport, scope)` ; OpenVPN seulement.
  - `pause_s` configurable (défaut 30 s, max 60 s).
  - décroissance : `level` baisse après `2 × pause_s` sans échec.
  - `clear()` sur tout 200 réel, pas seulement sur la réparation manager.
  - événements exposés : niveau, pause restante, stations concernées.
- Le superviseur ne réduit plus `max_parallel` à 1 sur une pause OpenVPN ; il
  réduit seulement les starts OpenVPN, jamais WireGuard.

**Fichiers** : `station_repair.py:290-345`, `vpn/manager.py:103,8483-8495`,
`scripts/fresh_chain_supervisor.py:641-652,1986-1994`, `config.yaml:676`.

#### D4 — Warm pool réel et cutover make-before-break

- `free/standby.py` : service de warm pool au-dessus de la table existante.
  - `reserve(n=station_count+2)` : maintient N **IP** spares préqualifiées
    pour les six stations ; ce ne sont pas des stations.
  - `take(station, need)` : claim atomique, lease court.
  - `promote(spare)` : preuve de contenu puis lease 6 h.
  - `replenish()` : job de fond, budget de probes, jamais sur une IP serving.
- Le qualifier utilise une sonde zéro quota pour la liveness, puis **un seul**
  200 réel pour la promotion.
- Cutover : claim spare → bind → egress → probe 200 → bascule du routage →
  libération de l'ancienne IP. L'ancienne IP n'est libérée qu'après le 200.
- Si aucun spare : 503 + `Retry-After`, incident `standby_empty`, pas d'attente
  bloquante.

**Fichiers** : nouveau `free/standby.py` ; `free/warm_pool.py`,
`free/rotation_planner.py`, `free/repair_queue.py`,
`scripts/fresh_chain_supervisor.py`.

#### D5 — Un seul réparateur, slots et progression

- `repair_owner=supervisor` devient une porte dure : le pool ne fait que
  `enqueue_repair(priority)` sur 429, qualité ou doublon.
- Le superviseur ne répare que les **six stations configurées** ; aucune station
  7, aucun canary traité comme station. `target_slots=6`.
- `max_parallel_repairs` passe à 3 (4 en soak) ; les six stations sont
  éligibles, mais une station serving n'est jamais préemptée sans spare prouvé.
- Dès qu'une station n'est plus `client_usable` (2 probes négatives ou un 429
  bloquant), elle entre en `draining` et sa rotation est enqueueée
  immédiatement, sans attendre les autres.
- Budget de chaîne fondé sur la progression : `max_binds_without_200` (défaut
  12) ou `chain_deadline_s` (défaut 300 s), au lieu du cap fixe 1200 s.
- Backoff par station : 5 s après un échec de bind, 60 s après un échec de
  transport, 300 s après un slice épuisé.
- Poll superviseur : 5 s (au lieu de 20 s) ; les demandes prioritaires passent
  devant les refreshs.

**Fichiers** : `scripts/fresh_chain_supervisor.py`, `free/repair_queue.py`,
`free/pool/__init__.py` (`on_429_action`), `config.yaml:232,1263`.

#### D6 — Quota et ledger sans résurrection

- `assign_observed` exige une preuve fraîche (`proven_at > last_429_at`,
  `valid=true`) ; sinon il refuse et laisse l'IP en `exhausted`.
- Un 429 écrit `reset_at` ; aucun chemin ne remet `reset_at=NULL` sans preuve.
- Scope 429 :
  - `quota` avec `Retry-After` → `reset_at`.
  - `unknown` → cooldown 60–120 s, pas minuit.
  - `bad_ip` sans 200 antérieur → erreur courte, pas quota.
- Le ledger ne reteste pas avant `reset_at` ; `max_block_s` ne tronque plus le
  reset fournisseur.
- `/24` (IPv4) et `/64` (IPv6) sont corrélés pour l'alerte et l'ordre, pas une
  porte dure sauf preuve d'épuisement partagé.
- Les leases sont renewés à chaque poll du superviseur ; TTL 6 h ; `reconcile`
  au boot.
- Les états temporels persistés passent en wall-clock (ou sont invalidés).

**Fichiers** : `free/ip_registry/registry_db.py`, `free/ip_quota.py`,
`free/quota_ledger.py`, `free/catalog_claims.py`, `fresh_chain.py`,
`opencode.py:15400-15419,24906-24913`.

#### D7 — WARP et proxylist comme réserve

- WARP : catalogue live (adapter qui lit l'état des conteneurs à chaque cycle),
  probe par famille, healthcheck actif avec deadline, rotation hors lock global
  pour la partie resolve/apply.
- Proxylist : `infra` vs `dead`, denylist sur l'IP d'egress, persistance des
  erreurs de source, workers stables, budget de probes par option.
- Réserve : quand 6/6 NordVPN est sain, WARP/proxys ont un poids de standby
  (0 trafic) ou une capacité réservée ; en incident, ils passent en actif.
- Test de bascule : couper NordVPN 15 min, vérifier 6/6 via les réserves.

**Fichiers** : `free/egress_runtime.py`, `free/egress_adapters/warp.py`,
`free/egress_adapters/public.py`, `free/warp_rotation.py`,
`free/egress_store.py`, `free/pool/__init__.py`.

#### D8 — Une seule mesure de disponibilité

- `free/availability.py` :
  - échantillonne `client_usable` par station toutes les 30 s ;
  - écrit un échantillon brut `{ts, station, usable, ip, transport, reason,
    source, last_200_age_s}` ;
  - calcule 1 h/24 h/7 j/30 j depuis les échantillons, sans filtre `>0` ;
  - calcule le burn rate et le budget d'erreur.
- Un seul journal d'incidents (`registry.db.incidents`) ; le sampler et le
  watchdog écrivent dedans, pas dans une seconde base.
- `client_usable` = 200 réel avec contenu, âge ≤ 120 s pour le routing ;
  l'affichage peut tolérer 900 s mais l'indique.
- API : `/api/availability?window=1h|24h|7d|30d` ; `/metrics` expose
  `availability_1h/24h`, `six_of_six`, `incidents_open`, `cutover_p95`.
- Alertes : burn rate > 2× sur 1 h, 0/6 > 60 s, standby vide, pause > 60 s.
- Dashboard : station, IP, transport, dernier 200, incident, cutover.

**Fichiers** : nouveau `free/availability.py` ; `scripts/station_slo_watch.py`,
`free/qualification/readmodel.py`, `free/qualification/store.py`,
`dashboard/routers/qualification_routes.py`, `scripts/vpn_alerts.py`,
`dashboard-ui/src/pages/vpn/QualificationStatus.svelte`.

#### D9 — Crash, reboot, locks

- Tous les timestamps persistés sont wall-clock (`time.time()`), jamais
  `monotonic` ; migration des états existants au boot.
- `acquire_lock` atomique (`O_EXCL` + `os.replace`), vérification du PID et de
  la ligne de commande ; `_stop_chain` vérifie le propriétaire avant de
  supprimer.
- `reconcile` au boot : leases actifs, IP live, état station ; libère les
  orphelins, ne duplique jamais une IP.
- Un redémarrage ne perd pas le warm pool ni les `reset_at`.

**Fichiers** : `free/chain_repair.py`, `scripts/fresh_chain.py`,
`free/ip_registry/registry_db.py`, `free/ip_quota.py`,
`free/station_health.py`, `opencode.py` (boot reconcile).

## Phases d'implémentation

### Phase 0 — Arrêt de l'hémorragie (J0–J1)

Objectif : casser la boucle 0/6 et obtenir une baseline fiable.

1. **P0-1** : protéger le gagnant (`D1`). Test : winner frais + host live
   différent → pin conservé 180 s, station adoptée.
2. **P0-2** : guard AUTH local et borné (`D3`). Test : deux AUTH → pause ≤ 60 s,
   WireGuard continue, retour 6/6 ≤ 2 min.
3. **P0-3** : un seul réparateur (`D5`). Test : 429 → une seule demande de
   réparation, aucun recreate pool.
4. **P0-4** : ledger sans résurrection (`D6`). Test : 429 → `reset_at`
   respecté ; `assign_observed` sans preuve → refus.
5. **P0-5** : config sanity (`D5`, `D6`) : `no_egress_backoff_max_s` ≥ base,
   `auth_fallback_transport=wireguard`, `auth_global_pause_s` câblé,
   `transport_cycle_timeout_s`/`transport_auth_backoff_s` consommés ou
   supprimés.
6. **P0-6** : probes budget (`D4`) : liveness zéro quota ; confirmation
   seulement après un premier 200 ; max 2 modèles de secours.
7. **P0-7** : baseline `availability.py` (`D8`) : échantillons + incidents +
   fenêtre 1 h, avant/après.
8. **P0-8** : alignement de taille : `station_count=6`, `target_slots=6`,
   canary hors session (pas de 7e station), refus explicite de toute station 7.
9. **P0-9** : rotation immédiate par station : 2 probes négatives ou un 429
   bloquant → `draining` + `enqueue_repair(priority)` ; budget de progression
   12 binds/300 s ; suppression de l'attente du cap 1200 s. Test : station down
   → chaîne démarrée ≤ 5 s.

**Exit criteria** : 0/6 < 2 min ; 6/6 ≥ 95 % sur 1 h ; aucun pin frais effacé ;
aucune pause > 60 s ; station down → rotation démarrée ≤ 5 s ; baseline SLO
publiée.

### Phase 1 — Indépendance des transports (S1)

Objectif : la cascade WG → OVPN UDP → TCP → WG fonctionne par station, sans
pause globale.

1. `free/transport_ladder.py` + tests de domaine.
2. Branchement `fresh_chain` (host/transport, budgets, erreurs typées).
3. Branchement manager (auto/emergency borné, pas de fork).
4. Circuit breaker par `(station, transport)` et métriques.
5. Sérialisation cross-process des binds (lease DB, 12–15 s, 30 s par station).
6. Session budget : exactement six sessions, une par station ; le canary est
   supprimé ou exécuté hors session NordVPN ; aucun 7e bind silencieux.

**Exit criteria** : AT-01, AT-02, AT-03 verts ; 6/6 ≥ 99 % sur 24 h ; cutover
transport p95 ≤ 45 s.

### Phase 2 — Warm pool et cutover rapide (S2)

Objectif : rotation sans attente, quota préservé.

1. `free/standby.py` (reserve/take/promote/replenish) + migration.
2. Qualifier de standby (liveness zéro quota + un 200, TTL, score).
3. Cutover make-before-break (claim → bind → 200 → switch → release).
4. Superviseur : poll 5 s, priorité serving, 3 slots, budget de progression.
5. Chemin requête : standby immédiat ou 503 + `Retry-After` ≤ 2 s.
6. Lease renewal, claim atomique, `reset_at` proof-gated.
7. `/24` et `/64` : corrélation, ordre, alerte.
8. Warm pool N+2 et test de vidage.

**Exit criteria** : AT-04, AT-05, AT-06, AT-07 verts ; cutover p95 ≤ 20 s ;
6/6 ≥ 99,9 % sur 24 h ; zéro 429 causé par nos probes.

### Phase 3 — WARP et proxylist en réserve (S3)

Objectif : NordVPN coupé → pas de coupure client.

1. Catalogue WARP live + probe par famille + healthcheck fallback.
2. Rotation WARP découpée (resolve/apply hors lock global) + budget.
3. Proxylist : `infra` vs `dead`, denylist IP, source health, workers stables,
   probe budget.
4. Réserve WARP/proxys : poids standby, capacité réservée, bascule.
5. Test de coupure NordVPN 15 min.

**Exit criteria** : AT-08 vert ; service maintenu pendant 15 min sans NordVPN,
6/6 stations explicitement 0/6, aucune 7e station ; aucun
egress de réserve consommé avant l'incident.

### Phase 4 — SLO, alertes, crash (S4)

Objectif : le 99,99 % est mesuré, alerté et survit à un reboot.

1. `free/availability.py` complet (fenêtres, burn rate, raw samples).
2. Un seul journal d'incidents ; migration des deux journaux.
3. `/metrics` + `/api/availability` + dashboard.
4. Alertes burn rate, 0/6 > 60 s, standby vide, pause > 60 s.
5. Crash/reboot : wall-clock, reconcile, locks atomiques.
6. Alignement cible 99,99 %, docs, DEBT, option-map.

**Exit criteria** : AT-09, AT-10 verts ; SLO calculé depuis les bruts ; reboot
sans état fantôme ; alertes testées.

### Phase 5 — Chaos, soak, release (S5)

Objectif : prouver la tenue et livrer.

1. Suite chaos : kill tunnel, blocage WG/UDP/TCP, épuisement IP, orage AUTH,
   reboot, source morte, WARP mort, proxy mort.
2. Soak 24 h puis 72 h ; mesure 6/6, cutover, probes, incidents.
3. Gates : pytest, ruff, config_audit, platform_audit, option-map, CI
   Windows/Linux.
4. Runbook, CHANGELOG, release checklist, mise à jour DEBT.
5. Canary station puis flotte complète ; rollback par flag.

**Exit criteria** : SC-001 à SC-008 ; 72 h sans P0 ; release signée.

## Workstreams et fichiers

| Workstream | Fichiers principaux | Responsable |
|---|---|---|
| Cohérence gagnant | `scripts/fresh_chain_supervisor.py`, `fresh_chain.py`, `free/chain_state.py`, `registry_db.py` | P0 |
| Transport ladder | `free/transport_ladder.py` (nouveau), `fresh_chain.py`, `vpn/manager.py`, `vpn/auth.py` | P1 |
| Warm pool / cutover | `free/standby.py` (nouveau), `free/warm_pool.py`, `rotation_planner.py`, `repair_queue.py` | P1 |
| Quota / ledger | `free/ip_quota.py`, `quota_ledger.py`, `catalog_claims.py`, `registry_db.py` | P0/P1 |
| WARP / proxys | `free/egress_runtime.py`, `egress_adapters/*`, `warp_rotation.py`, `egress_store.py` | P1/P2 |
| Mesure / SLO | `free/availability.py` (nouveau), `station_slo_watch.py`, `readmodel.py`, `dashboard/` | P0/P1 |
| Superviseur | `fresh_chain_supervisor.py`, `config.yaml`, `repair_queue.py` | P0/P1 |
| Robustesse | `chain_repair.py`, `opencode.py`, migrations | P1/P2 |

## Testing Strategy

- **Domaine** : `transport_ladder`, `availability`, `standby` sans I/O ; cas
  limites (breaker ouvert, budget épuisé, dernière station).
- **SQLite** : claims atomiques, lease renewal, `reset_at`, migration
  monotonic → wall-clock, concurrence 2 writers.
- **Adapters factices** : binder, probe, WARP, proxys ; tests de cutover sans
  réseau.
- **Boucle superviseur** : six stations, slots, priorité, winner/pin, auth
  locale ; test process-level avec un faux `fresh_chain`.
- **Live canary** : une station en shadow, puis deux, puis six.
- **Chaos** : scripts `scripts/chaos_egress.py` (nouveau) ; scénarios AT-01 à
  AT-10.
- **Soak** : 24 h en CI nightly, 72 h avant release ; rapport automatique.

## Risks & Mitigations

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Limite de 6 sessions NordVPN | Haute | AUTH en cascade | Sérialiser, supprimer le canary comme 7e session, refuser le 7e bind, alerter. |
| Quota free par IP trop petit | Haute | 429 fréquents | Warm pool, drain à 80 %, probes zéro quota, réserve WARP/proxys. |
| Pause AUTH mal calibrée | Moyenne | Coupure prolongée | Pause ≤ 60 s, locale, décroissante, clear sur 200. |
| Cutover plus lent que le budget | Moyenne | 503 client | Make-before-break, standby préqualifié, 503 court. |
| WARP/proxys non fiables | Moyenne | Pas de secours | Catalogue live, probe famille, réserve, test de coupure. |
| Régression du proxy | Basse | Service down | Strangler, flags, canary, rollback, tests. |
| Mesure SLO biaisée | Moyenne | Faux 99,99 % | Échantillons bruts, fenêtres, tests de calcul, audit externe. |

## Rollout

1. **Shadow** : `availability.py` et le ladder en observation, aucun changement
   de routage. Comparer avec l'existant.
2. **Canary** : une des six stations (s6) sur le nouveau chemin ; s1 témoin.
   Aucune station 7 n'est créée pour le canary.
3. **Ramp** : 2 stations, puis 4, puis 6 ; chaque étape 24 h.
4. **Flags** : `allocator.transport_ladder`, `allocator.standby_cutover`,
   `availability.slo_v2`, `egress.reserve_policy`. Rollback par flag, sans
   migration destructive.
5. **Release** : gates, soak 72 h, runbook, DEBT à jour.

## Open Questions

1. Le SLO primaire est-il **request-weighted** (99,99 % des requêtes) ou
   **time-weighted** (99,99 % du temps à 6/6) ? Le plan propose le premier comme
   cible contractuelle et le second comme cible stretch.
2. Le warm pool cible `station_count + 2` (8 IP) ou +4 (10 IP) pour les six
   stations ? Le plan part sur +2, ajustable après mesure du taux de 429.
3. WARP/proxys : réserve de service hors 6/6, jamais stations 7/8 (décision
   prise).
4. Le canary WireGuard est **supprimé** comme septième session : la contrainte
   « six stations exactement » l'interdit. Un probe de santé sans session
   NordVPN peut le remplacer.
5. La cible 99,99 % de temps à 6/6 est-elle acceptée comme stretch (et non
   contractuelle) tant que le quota par IP reste à 20–40 requêtes ? Le plan
   l'expose explicitement dans le dashboard.
