# Feature Specification: Dimensionnement des stations par provider et par mode

**Feature Branch**: `010-egress-station-sizing`
**Created**: 2026-11-07
**Status**: Draft
**Input**: « Combien de stations faut-il si j'active NordVPN et WARP ? En fallback, six de chaque ? En répartition, trois et trois ? »

## Clarifications

### Session 2026-11-07

- Q : Le nombre de stations dépend-il du mode Fallback/Répartition ?
  R : Non. Le mode choisit **qui sert** ; le nombre de stations choisit **combien
  de slots client sont disponibles**. Les deux se combinent.
- Q : En Fallback, faut-il six NordVPN **et** six WARP ?
  R : Seulement si l'objectif est **6 stations disponibles même si le provider
  primaire tombe entièrement**. C'est la politique `full` (redondance totale).
  Avec la politique `shared`, six slots au total suffisent, mais le fallback
  est dégradé.
- Q : En Répartition 50/50, faut-il faire 3 + 3 ?
  R : Pas nécessairement. 6 + 6 avec poids 50/50 donne 12 endpoints et
  deux fois moins de charge par endpoint ; 3 + 3 donne 6 endpoints et zéro
  réserve. Le choix est un arbitrage capacité/redondance, pas une obligation
  du mode.
- Q : Les poids servent-ils à dimensionner les pools ?
  R : Non. Les poids sont une part de **trafic**. Le dimensionnement se fait
  par la politique de réserve et, si on veut équilibrer la charge par endpoint,
  par la politique `proportional`.

## Vocabulaire

- **Slot** : une station client-utilisable, avec une IP et un quota distincts.
  L'objectif permanent est `target_slots = 6`.
- **Endpoint** : un point de sortie concret (station NordVPN, endpoint WARP,
  proxy statique, direct). Un provider possède un pool d'endpoints.
- **Provider** : NordVPN, WARP, Proxys SOCKS5/HTTPS, Direct.
- **Politique de réserve** :
  - `full` : chaque provider sélectionné peut couvrir seul `target_slots`
    (défaut, garantit 6 stations même si un provider tombe).
  - `shared` : la somme des endpoints utilisables doit couvrir `target_slots`
    (économique, mais capacité réduite après panne).
  - `proportional` : les endpoints sont répartis selon les poids (Répartition
    uniquement), pour égaliser la charge par endpoint.
- **Charge par endpoint** : `poids_provider / nombre_endpoints_utilisables`.
  C'est le vrai indicateur de vitesse d'épuisement des quotas.

## Pourquoi six ?

Six est un **objectif produit** (six stations client-utilisables), pas une
contrainte mathématique du moteur. Le moteur peut servir plusieurs requêtes par
endpoint ; ce qui limite, c'est le quota par IP et la concurrence voulue.

Le bon raisonnement est donc :

1. Choisir `target_slots` (défaut : 6).
2. Choisir si chaque provider doit pouvoir servir seul ces 6 slots
   (`full`) ou si la somme suffit (`shared`).
3. En Répartition, choisir si l'on veut que chaque endpoint consomme son quota
   au même rythme (`proportional`) ou si l'on accepte un déséquilibre.

## User Stories

### US1 — Voir la capacité réelle par provider (P1)

En tant qu'opérateur, je veux voir, pour chaque provider coché, le nombre
d'endpoints configurés, activés et utilisables, afin de savoir si mes six
stations sont réellement couvertes.

**Acceptance** :

- Une carte « Capacité » affiche NordVPN, WARP, Proxys, Direct.
- Chaque ligne montre `configurés / activés / utilisables`.
- Le total et le déficit par rapport à `target_slots` sont visibles.
- Un provider décoché est affiché comme désactivé, pas comme en erreur.

### US2 — Comprendre le nombre requis selon le mode (P1)

En tant qu'opérateur, je veux une recommandation chiffrée qui change quand je
bascule Fallback/Répartition ou quand je modifie les poids.

**Acceptance** :

- Fallback + `full` : « 6 NordVPN + 6 WARP pour 6 stations sans dégradation ».
- Fallback + `shared` : « 6 + N, mais N stations seulement après bascule ».
- Répartition 50/50 + `full` : « 6 + 6 (12 endpoints, redondance totale) ».
- Répartition 50/50 + `shared` : « 3 + 3 (6 endpoints, pas de réserve) ».
- Répartition 2:1 + `proportional` : « 4 + 2 pour 6 stations ».
- Direct est compté pour **1** endpoint, jamais plus.

### US3 — Être alerté d'une capacité insuffisante (P1)

En tant qu'opérateur, je veux un verdict clair avant de lancer six stations :

- `ok` : les six slots sont couverts avec la réserve choisie.
- `degraded` : les six slots sont couverts maintenant, mais un provider
  tombé ferait passer sous 6.
- `insufficient` : moins de six slots utilisables dès maintenant.

### US4 — Appliquer les recommandations (P2)

En tant qu'opérateur, je veux pouvoir appliquer un dimensionnement recommandé
sans calculer moi-même :

- NordVPN : ajuster `station_count` (1–10).
- WARP : activer/désactiver des endpoints existants ; la création/suppression
  de conteneurs reste une action explicite avec confirmation.
- Proxys : activer/désactiver des entrées existantes.
- Direct : non redimensionnable (1 slot).

### US5 — Vérifier la charge observée (P3)

En tant qu'opérateur, je veux comparer la part de trafic attendue (poids) à la
part observée par provider, et la charge par endpoint, pour détecter un
déséquilibre de quota.

## Functional Requirements

- **FR-001** Le système DOIT exposer `target_slots` (entier 1–10, défaut 6).
- **FR-002** Le système DOIT exposer une politique de réserve
  `full | shared | proportional` (défaut `full` : chaque provider sélectionné
  peut couvrir seul `target_slots`).
- **FR-003** La capacité configurée DOIT être calculée depuis les sources
  existantes : `station_count` pour NordVPN, endpoints WARP activés pour WARP,
  proxys activés pour Proxys, `1` pour Direct.
- **FR-004** La capacité utilisable DOIT exclure les endpoints désactivés,
  parked, en quarantaine auth/403 ou sans quota. En MVP, « activé » et
  « utilisable » peuvent être égaux ; la Phase 2 branche la santé réelle.
- **FR-005** En Fallback + `full`, chaque provider sélectionné DOIT avoir
  `>= target_slots` endpoints utilisables.
- **FR-006** En Fallback + `shared`, le provider primaire DOIT avoir
  `>= target_slots` ; les suivants peuvent être en nombre inférieur, mais le
  verdict DOIT indiquer la capacité après bascule.
- **FR-007** En Répartition + `full`, chaque provider sélectionné DOIT avoir
  `>= target_slots`.
- **FR-008** En Répartition + `shared`, la somme des endpoints utilisables
  DOIT être `>= target_slots`.
- **FR-009** En Répartition + `proportional`, le système DOIT proposer une
  répartition entière des `target_slots` qui minimise l'écart de charge par
  endpoint, puis laisser l'opérateur appliquer.
- **FR-010** Les poids NE DOIVENT PAS être interprétés comme un nombre
  d'endpoints. Un poids de 50 % ne signifie pas « 3 stations ».
- **FR-011** Le système DOIT afficher `charge_par_endpoint = poids / endpoints`
  et signaler un écart > 2× entre le provider le plus chargé et le moins chargé.
- **FR-012** Direct DOIT compter pour exactement 1 endpoint ; un poids Direct
  élevé avec une seule IP DOIT déclencher un avertissement de quota.
- **FR-013** Le verdict DOIT être `ok`, `degraded` ou `insufficient` selon la
  politique et l'état des pools.
- **FR-014** La carte Capacité DOIT être visible dans
  `Modèles Gratuits > Egress & providers`, sans masquer les sliders.
- **FR-015** Les recommandations DOIVENT être calculées côté serveur (`/api/egress/capacity`)
  et rendues en français/anglais ; le GUI ne recalcule pas la vérité.
- **FR-016** Le dimensionnement appliqué DOIT être persistant et relu au
  redémarrage.
- **FR-017** Aucune création de ressource payante ou de second compte ne DOIT
  être proposée ; WARP/proxys restent les seuls leviers.
- **FR-018** Une action d'application DOIT être idempotente et ne jamais
  supprimer un endpoint sans confirmation explicite.

## Key Entities

- **CapacityPlan** : `target_slots`, `strategy`, `reserve_policy`, providers,
  verdict, recommandations.
- **ProviderCapacity** : `configured`, `enabled`, `usable`, `weight`,
  `expected_share`, `per_endpoint_load`, `deficit`.
- **ReservePolicy** : `full`, `shared`, `proportional`.
- **CapacityReport** : snapshot sérialisable pour l'API et le GUI.

## Success Criteria

- **SC-001** 6 NordVPN + 6 WARP en Fallback `full` → `ok`, 12 endpoints,
  6 de réserve.
- **SC-002** 6 NordVPN + 3 WARP en Fallback `full` → `degraded` (3 slots après
  bascule).
- **SC-003** 6 + 6 en Répartition 50/50 → chaque endpoint reçoit ~8,3 % du
  trafic, verdict `ok`.
- **SC-004** 3 + 3 en Répartition 50/50 `shared` → 6 slots, verdict `ok`,
  mention « aucune réserve ».
- **SC-005** 4 + 2 en Répartition 2:1 `proportional` → 6 slots, charge par
  endpoint égale (~16,7 %).
- **SC-006** 6 + 3 en Répartition 50/50 → alerte de déséquilibre : WARP brûle
  ses quotas 2× plus vite.
- **SC-007** Direct à 20 % avec 1 endpoint → avertissement de concentration.
- **SC-008** 5 endpoints utilisables pour `target_slots=6` → `insufficient`,
  déficit 1.
- **SC-009** Le changement de mode ou de poids met à jour la recommandation en
  moins de 500 ms côté API.
- **SC-010** 100 % des tests de calcul passent sans réseau ni Docker.

## Assumptions

- L'objectif de six stations est un paramètre (`target_slots`), pas une
  constante codée en dur.
- `station_count` reste la source de vérité pour NordVPN.
- Les endpoints WARP existent déjà dans `socks5_proxies` ; le MVP ne crée pas
  de conteneurs.
- Les quotas sont par IP/IPv6 `/64` ; un endpoint = un bucket de quota.
- Le mode Répartition répartit des **requêtes**, pas des stations physiques.
- Le fallback chaud (conteneurs WARP déjà lancés) est requis pour une bascule
  sans dégradation ; le mode froid est une évolution.
