# Feature Specification: Zéro 429 client — réserve d'IPs prouvées

**Feature Branch**: `022-zero-429-client-reserve`

**Created**: 2026-11-15

**Status**: Draft

**Input**: « Qu'est-ce qu'il faut modifier pour que mes clients ne tombent jamais en 429 ? Fais-moi un plan. »

## Objectif

Un client ne doit **jamais** recevoir un 429 tant qu'au moins une IP de réserve est prouvée 200. Le quota free par IP est fini : la seule façon de tenir cette promesse est de séparer strictement la consommation des probes de celle des clients, et de maintenir une réserve d'IPs déjà prouvées, jamais touchées par les probes.

**Définition de « jamais »** : zéro 429 client sur le trafic servi, tant que `reserve_ips >= 1`. Si la réserve tombe à zéro, le proxy bascule sur un fallback (WARP/proxys) avant de renvoyer un 429 ; si tous les fallbacks sont épuisés, le 429 est inévitable et porte un `Retry-After` exact.

## État constaté (2026-11-15)

- `0/6` stations `client_usable` ; toutes les IPs à `FreeUsageLimitError` avec des resets de 2 à 15 min.
- Les probes (`StationProbe`, `fresh_chain`, tests manuels) consomment le **même bucket** que les clients.
- Aucune réserve dédiée : le warm pool contient des IPs vérifiées, pas nécessairement des 200, et les probes le consomment.
- Le pool envoie le client vers une station dont le bucket peut être vide ; le 429 upstream est relayé.
- Le fallback WARP/proxys est tenté, mais il est lui aussi soumis au quota et sans garantie.
- La clé Go pausée 401 est sans rapport avec le free.

## User Scenarios & Testing

### User Story 1 — Réserve d'IPs prouvées (Priority: P1)

En tant qu'opérateur, je veux une réserve d'IPs déjà prouvées 200, jamais utilisées par les probes, afin que le client ait toujours une IP prête.

**Independent Test**: vider la réserve, lancer une requête client, vérifier qu'elle ne part pas sur une IP non réservée.

**Acceptance Scenarios**:

1. **Given** `reserve_ips >= 1`, **When** un client appelle, **Then** il est routé vers une IP de réserve.
2. **Given** une IP de réserve est consommée, **When** le quota tombe sous le seuil, **Then** elle sort de la réserve et le superviseur en prépare une autre.
3. **Given** une probe tourne, **When** elle choisit une IP, **Then** elle ne peut jamais choisir une IP de réserve.

---

### User Story 2 — Séparation stricte probe/client (Priority: P1)

En tant qu'opérateur, je veux que les probes aient un budget séparé, afin qu'elles ne consomment jamais le quota réservé aux clients.

**Independent Test**: lancer 100 probes et vérifier que `client_used` reste à 0 sur les IPs de réserve.

**Acceptance Scenarios**:

1. **Given** une IP marquée `reserved_for_client`, **When** `StationProbe` la sélectionne, **Then** elle est refusée.
2. **Given** le budget probe est épuisé, **When** une probe veut tourner, **Then** elle attend le reset sans toucher à la réserve.

---

### User Story 3 — Fallback avant 429 (Priority: P1)

En tant que client, je veux que le proxy tente WARP/proxys avant de me renvoyer un 429, afin de ne pas voir d'erreur si une autre sortie est disponible.

**Independent Test**: vider la réserve, laisser un WARP disponible, envoyer une requête, vérifier un 200.

**Acceptance Scenarios**:

1. **Given** réserve vide et WARP disponible, **When** un client appelle, **Then** le proxy sert via WARP.
2. **Given** réserve vide et tous les fallbacks épuisés, **When** un client appelle, **Then** le 429 porte un `Retry-After` exact.

---

### User Story 4 — Alerte de réserve basse (Priority: P2)

En tant qu'opérateur, je veux une alerte quand `reserve_ips < 2`, afin d'agir avant la panne.

**Acceptance Scenarios**:

1. **Given** `reserve_ips = 1`, **When** le superviseur tourne, **Then** une alerte est émise.
2. **Given** `reserve_ips = 0`, **When** le superviseur tourne, **Then** une alerte critique et un incident sont ouverts.

---

### User Story 5 — Mesure du taux de 429 client (Priority: P2)

En tant qu'opérateur, je veux mesurer le taux de 429 réellement renvoyé aux clients, distinct des 429 de probe.

**Acceptance Scenarios**:

1. **Given** 1000 requêtes client, **When** la réserve est non vide, **Then** `client_429_total = 0`.
2. **Given** un 429 client, **When** il est renvoyé, **Then** il est compté et attribué à une cause (`reserve_empty`, `all_fallbacks_dead`, `upstream_429`).

## Exigences fonctionnelles

- **FR-001** : une table `client_reserve` (ou un flag `reserved_for_client` dans `warm_pool`) contient les IPs prouvées 200, avec `hostname`, `station`, `verified_at`, `expires_at`.
- **FR-002** : le pool client ne sélectionne **que** des IPs de réserve.
- **FR-003** : `StationProbe` et `fresh_chain` refusent toute IP `reserved_for_client`.
- **FR-004** : le superviseur remplit la réserve en arrière-plan avec des probes sur des IPs non réservées.
- **FR-005** : le budget probe est distinct du budget client (`probe_budget_pct`, défaut 50 %).
- **FR-006** : si la réserve est vide, le pool bascule sur WARP/proxys avant de renvoyer 429.
- **FR-007** : un 429 client porte un `Retry-After` exact et une cause.
- **FR-008** : alerte si `reserve_ips < 2`, critique si `0`.
- **FR-009** : métriques `client_reserve_ips`, `client_429_total{reason}`, `probe_budget_remaining`.
- **FR-010** : tests de charge : 1000 requêtes client avec réserve non vide → 0 429.

## Critères d'acceptation mesurables

| Critère | Cible |
|---|---|
| 429 client avec réserve ≥ 1 | **0** sur 1000 requêtes |
| Réserve maintenue | **≥ 2** IPs prouvées en permanence |
| Probes sur réserve | **0** |
| Fallback avant 429 | 100 % des cas où un fallback est disponible |
| Alerte réserve basse | émise à `reserve_ips < 2` |
| 429 client avec `Retry-After` | 100 % |

## Conformité aux exigences du projet

| Exigence | Réponse du plan | Statut |
|---|---|---|
| Quota free **par IP**, jamais par compte | `client_reserve` est indexée par IP ; aucune clé de compte | conforme |
| Jamais de payant, second compte ou attente comme réponse | Fallback free uniquement (WARP/proxys) ; 429 explicite si tout est épuisé | conforme |
| Six stations maximum, jamais de 7ᵉ | La réserve est répartie sur les 6 stations ; aucune création de station | à expliciter (fait ci-dessous) |
| Une IP d'egress par station | `client_reserve.station` unique + index d'unicité IP | conforme |
| Migration douce : ne pas recréer une station saine | Le remplissage ne touche pas aux stations `client_usable` | à expliciter (fait ci-dessous) |
| Superviseur jamais gelé, seul réparateur | Le superviseur remplit la réserve et répare ; l'app ne crée rien | conforme |
| Wire officiel complet (principe XVIII) | Les entrées de réserve viennent de `StationProbe` 1.19.5 apparié | à expliciter (fait ci-dessous) |
| IP grillée 24 h exclue | Aucune IP `probe-format-poisoned` n'entre dans la réserve | à expliciter (fait ci-dessous) |
| `warm_pool >= 6` avant remontée | `reserve_target = 6` (et non 3) | corrigé |
| 6/6 sur le **même cycle** de probe | Critère de sortie : 6/6 simultané, pas six 200 à des heures différentes | à expliciter (fait ci-dessous) |
| Aucune fenêtre console | Tous les sous-processus runtime portent `CREATE_NO_WINDOW` | conforme (test 021) |
| Cross-platform | Le code reste Python pur ; pas d'API Windows hors adapters | conforme |
| Options GUI effectives | `client_reserve.enabled`, `reserve_target`, `probe_budget_pct` avec consommateur et test | à expliciter (fait ci-dessous) |
| Données traçables, backup, rétention | `client_reserve` migrée, incluse dans le backup `registry.db` | à expliciter (fait ci-dessous) |

### Corrections d'alignement appliquées

1. **`reserve_target` est un paramètre à valider.** Il désigne le nombre d'IPs de réserve maintenues en permanence. Proposition : 6 (une par station), configurable. L'objectif AGENTS `warm_pool >= 6` plaide pour 6, mais la valeur reste un réglage opérateur, pas une constante.
2. **Le mode direct est conditionnel.** Il n'est utilisé que si la configuration l'autorise (`proxy_mode`, checkbox ou `direct_allowed`) ; sinon il est ignoré. Ordre : réserve → WARP → proxys → direct si autorisé → 429.
3. **La réserve est répartie sur les six stations**, une IP par station, sans création d'une septième.
4. **Aucune IP `probe-format-poisoned`** n'entre dans la réserve ; les probes de remplissage utilisent `StationProbe` 1.19.5 apparié.
5. **Migration douce** : le remplissage ne recrée jamais une station `client_usable` ; il attend qu'une station soit libre.
6. **Critère 6/6 simultané** : la sortie exige six stations `client_usable` sur le même cycle de probe, pas six 200 observés à des heures différentes.
7. **Options GUI** : chaque option (`enabled`, `reserve_target`, `probe_budget_pct`, `reserve_min`) a un consommateur runtime, un effet observable et un test.
8. **Données** : `client_reserve` est dans `registry.db`, donc couverte par le backup `VACUUM INTO` et l'`integrity_check` existants.

## Hors périmètre

- Compte payant, workspace, second compte.
- Plus de six stations.
- Création artificielle de quota upstream.
- Refonte du wire free.

## Contraintes dures

- Le client ne doit jamais être routé vers une IP non prouvée 200.
- Les probes ne doivent jamais consommer la réserve.
- La réserve doit survivre à un redémarrage de l'app (persistée dans `registry.db`).
- Un fallback doit être tracé avec sa cause.
- Si la réserve est vide et qu'aucun fallback n'existe, le 429 est explicite, jamais une réponse vide.
