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
| 429 client avec réserve ≥ 1 | **0** sur 1 000 sélections simulées ; **0** sur un soak live ≤ budget mesuré |
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
| Migration douce : ne pas recréer une station saine | Garde `_repair_holds` (≥3 usables) ; 6/6 sans shadow = T019 | partiel |
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
5. **Migration douce** : le refresh proactif ne rebinde jamais une IP servable (`_refresh_allowed`) et garde ≥3 stations `client_usable` (`_repair_holds`). Testé et observé avec 6/6 et 8 IPs en réserve.
6. **Critère 6/6 simultané** : la sortie exige six stations `client_usable` sur le même cycle de probe, pas six 200 observés à des heures différentes.
7. **Options GUI** : chaque option (`enabled`, `reserve_target`, `probe_budget_pct`, `reserve_min`) a un consommateur runtime, un effet observable et un test.
8. **Données** : `client_reserve` est dans `registry.db`, donc couverte par le backup `VACUUM INTO` et l'`integrity_check` existants.


## Qualité, fiabilité, performance — angles morts et résolutions

Le plan initial garantit la séparation probe/client, mais il ne règle pas encore ces points :

1. **Conflit 6 réserves / 6 stations.** Avec `reserve_target = 6`, les six stations portent une IP de réserve. Résolution : le refresh proactif ne touche jamais une IP servable (`_refresh_allowed`) ; une IP qui atteint son budget sort de la réserve et libère sa station, donc le refill reprend exactement quand il faut. Aucune 7ᵉ station, aucune connexion éphémère.
2. **Budget client par IP absent.** La réserve peut être vidée par le trafic client avant le refill. Résolution : `client_budget_pct` (défaut 80 %) ; une IP dont le bucket descend sous le seuil sort de la réserve et est remplacée **avant** d'être épuisée.
3. **Pas de re-vérification avant usage.** Une IP de réserve peut avoir été consommée par un autre utilisateur (bucket partagé). Résolution : preuve rafraîchie par le 200 du cycle de probe ; `reserve_ttl_s` (défaut **3600 s**, configurable) ; un 429 actif ou un budget atteint sort l'IP immédiatement. Un TTL de 5 min a été écarté : 6 IPs × 12 re-probes/h = 72 probes/h, soit ~1 700/jour, incompatible avec ~200 requêtes/jour/IP. La fraîcheur vient de l'événement (200/429), pas d'un minuteur qui brûle le quota.
4. **Fallback non vérifié.** WARP/proxys peuvent être épuisés. Résolution : le fallback n'est utilisé que s'il a une preuve 200 récente ; sinon il est ignoré et le prochain fallback est tenté.
5. **Pas de SLO de performance.** Résolution : p95 client < 2 s, bascule de fallback < 500 ms, aucune requête bloquée > 5 s, 0 429 sur 1 000 requêtes avec réserve ≥ 1.
6. **Pas de verrou pendant le service.** Deux clients sur une même IP HTTP ne se nuisent pas ; un claim par requête les sérialiserait. Le risque réel est le rebind d'une IP **pendant** qu'elle sert : `_refresh_allowed` l'interdit et `test_rotation_never_takes_a_reserved_ip` le prouve.
7. **Pas de chaos test ni de CI.** Résolution : tests de panne (IP qui tombe, WARP mort, DB lock), tests `slow` exécutés en nightly, et test de non-régression 6/6.
8. **Pas de rétention/migration explicite.** Résolution : purge des entrées expirées, migration versionnée avec backup `VACUUM INTO` avant application.

Ces points sont convertis en tâches T019–T023 et en critères de checklist.


## Capacité et limite physique

Le free tier est borné par le quota de chaque IP. Le plan ne peut pas créer de quota ; il peut seulement garantir que le client ne consomme pas le quota des probes et qu'une réserve est prête.

Hypothèse de dimensionnement (à mesurer sur le ledger réel) :

- 6 IPs × quota estimé par IP (paramètre `quota_estimate`, défaut 200) = **1 200 requêtes/jour**.
- Réserve client 80 % (`client_budget_pct`) = **960 requêtes/jour** garanties.
- Au-delà, les IPs s'épuisent et le 429 redevient possible : il faut alors plus d'IPs (WARP/proxys) ou un autre provider free.

Règles de capacité :

1. **Priorité client** : le superviseur ne rebinde pas une station qui sert le client tant qu'il reste au moins 3 stations `client_usable` (`_repair_holds`). À 6/6, un refill exige le mode shadow (T019) ou attend qu'une station tombe : c'est une limite réelle, pas un contournement.
2. **Suspension réelle** : suspendre toutes les probes quand la réserve est basse arrêterait le seul mécanisme de refill. Ce n'est donc pas la règle : la règle est « ne pas casser ce qui sert le client », pas « arrêter de qualifier ».
3. **Alerte capacité** : si la demande client dépasse le budget journalier, une alerte `capacity_shortfall` est émise avant l'épuisement.
4. **Honnêteté** : le plan ne promet pas l'infini. Il promet zéro 429 **dans la capacité mesurée**, et une alerte quand la demande approche la limite.

Conséquence sur les tests : un burst réel de 1 000 requêtes dépasse la capacité d'une journée (~960). Le test T012 valide donc **1 000 sélections simulées** (routage, réserve, exclusion) ; le soak live reste borné au budget mesuré, sinon il ne teste plus la réserve mais l'épuisement upstream.


## Décisions tranchées

| Question | Décision |
|---|---|
| `reserve_target` | **6** par défaut (une IP par station), configurable ; validé par l'opérateur |
| `direct_allowed` | Lu depuis la config/GUI ; **false** par défaut ; direct utilisé seulement si explicitement autorisé |
| Capacité | **960 requêtes/jour** avec 6 IPs × 200 × 80 % ; mesurée sur le ledger, pas supposée |
| Priorité | **Client > probe** ; probes suspendues sous `reserve_min` |
| Réserve initiale | Les 6 IPs `client_usable` actuelles entrent dans la réserve au déploiement |
| Autorité | `client_reserve` est l'autorité ; `warm_pool` ne contient que les probes |
| Modèles | Réserve par bucket `default` (partagé), pas par modèle |
| 401/403 | Auth backoff séparé ; ne marque jamais la réserve comme épuisée |
| Erreurs transport | `no-egress`/timeout ne consomment pas la réserve ; retry ou rotation |
| Backpressure | Limite de concurrence 2000 (uvicorn) ; si réserve saturée, attente courte ou 429 explicite |
| TTL de réserve | **3600 s** par défaut (`RESERVE_TTL_S`) ; la preuve est rafraîchie à chaque 200 ; un TTL de 5 min brûlerait le quota en re-probes |
| Priorité / probes | `_refresh_allowed` : jamais de rebind d'une IP servable ; `_repair_holds` garde ≥3 `client_usable` |

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
