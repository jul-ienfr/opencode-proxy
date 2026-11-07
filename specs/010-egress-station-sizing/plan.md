# Implementation Plan: Dimensionnement des stations par provider et par mode

**Branch**: `010-egress-station-sizing` | **Date**: 2026-11-07 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/010-egress-station-sizing/spec.md`.

## Summary

Ajouter un **conseiller de capacité** qui répond à une question simple :
« avec mes providers cochés, mes poids et mon mode, ai-je bien six stations
client-utilisables, et combien d'endpoints faut-il par provider ? »

Le plan sépare deux notions qui ont été mélangées jusqu'ici :

| Notion | Ce que ça contrôle | Où c'est configuré |
|---|---|---|
| **Mode** (`fallback` / `balance`) | Qui sert la requête | `egress_strategy` |
| **Poids** | Part de trafic par provider | `egress_weights` |
| **Capacité** | Combien de stations/endpoints existent | `station_count`, `socks5_proxies`, `target_slots` |
| **Réserve** | Combien de providers peuvent couvrir seuls les 6 slots | `reserve_policy` |

La capacité n'est **pas** déduite des poids. Un poids de 50 % ne veut pas dire
« 3 stations ». C'est la politique de réserve qui décide si l'on veut 3+3,
4+2 ou 6+6.

## Réponse chiffrée (base de la conception)

Hypothèse : `target_slots = 6`.

### Implementation note (MVP 2026-11-07)

Le conseiller read-only est livré : `free/capacity.py`,
`GET /api/egress/capacity`, `VpnCapacity.svelte` et les tests. Il lit les
compteurs live (NordVPN via `usable_station_count`, WARP/proxys via
`egress_endpoint_counts`) et n'arrête/démarre aucun conteneur. Les actions
T016/T023–T029 restent différées.

### Fallback

| NordVPN | WARP | Politique | Verdict | Bascule |
|---:|---:|---|---|---|
| 6 | 6 | `full` | `ok` | 6 slots après panne NordVPN |
| 6 | 3 | `full` | `degraded` | 3 slots après panne |
| 6 | 6 | `shared` | `ok` | 6 slots (le secondaire est plein) |
| 6 | 3 | `shared` | `degraded` | 3 slots après panne |

En Fallback, les poids ne servent à rien. Le secondaire ne consomme pas de
quota tant qu'il n'est pas appelé, mais ses conteneurs doivent être **chauds**
pour une bascule immédiate.

### Répartition 50/50

| NordVPN | WARP | Total | Charge/endpoint | Verdict | Réserve |
|---:|---:|---:|---|---|---|
| 6 | 6 | 12 | 8,3 % | `ok` | totale (6+6) |
| 4 | 4 | 8 | 12,5 % | `ok` | totale (4+4) |
| 3 | 3 | 6 | 16,7 % | `ok` | aucune |
| 6 | 3 | 9 | 8,3 % / 16,7 % | `warn` | asymétrique |

Les deux premières lignes sont équivalentes en **part de trafic** ; elles
diffèrent en **capacité**. 6+6 double la réserve et divise par deux la vitesse
d'épuisement des quotas par endpoint.

### Répartition 2:1 (67 / 33)

| NordVPN | WARP | Total | Charge/endpoint | Verdict |
|---:|---:|---:|---|---|
| 4 | 2 | 6 | 16,7 % / 16,7 % | `ok` |
| 6 | 3 | 9 | 11,1 % / 11,1 % | `ok` |
| 6 | 6 | 12 | 11,1 % / 5,6 % | `warn` (WARP sous-utilisé) |

La politique `proportional` choisit 4+2 pour 6 slots, ou 6+3 pour 9 slots.
Elle égalise la charge **par endpoint**, pas seulement la part par provider.

### Direct

Direct vaut toujours **1 endpoint**. Avec un poids de 20 %, cette unique IP
reçoit 20 % du trafic : elle brûlera son quota beaucoup plus vite. Le GUI doit
le signaler. Direct ne doit jamais être compté comme 6 stations.

## Architecture

### 1. Module pur `free/capacity.py`

Aucune I/O, aucun Docker, testable seul.

```python
@dataclass(frozen=True)
class ProviderCapacity:
    name: str
    configured: int
    enabled: int
    usable: int
    weight: float | None
    expected_share: float | None
    per_endpoint: float | None
    deficit: int
    status: str

@dataclass(frozen=True)
class CapacityReport:
    target_slots: int
    strategy: str
    reserve_policy: str
    providers: dict[str, ProviderCapacity]
    total_usable: int
    total_required: int
    verdict: str          # ok | degraded | insufficient
    warnings: list[str]
    recommendations: list[str]
```

Fonctions :

- `required_by_policy(target, policy, providers, weights)` → dict provider → requis.
- `proportional_allocation(target, weights, fixed)` → dict provider → entier.
  Recherche exhaustive des compositions (N ≤ 10, 4 providers) minimisant
  `max(weight_i / n_i) / min(weight_i / n_i)`.
- `per_endpoint_load(weights, counts)` → dict.
- `evaluate_capacity(...)` → `CapacityReport`.
- `recommendation_text(report, locale)` → phrases FR/EN.

Règles de verdict :

1. `insufficient` si `total_usable < target_slots` ou si un provider requis
   par `full` a `usable < target_slots` **et** que le total ne compense pas.
2. `degraded` si les 6 slots sont couverts maintenant mais qu'une panne de
   provider ferait passer sous 6 (Fallback `shared`, Répartition `shared`).
3. `ok` sinon.
4. Les déséquilibres de charge et Direct sont des `warnings`, pas des verdicts
   de capacité.

### 2. Sources de capacité

| Provider | Configuré | Utilisable (MVP) | Utilisable (Phase 2) |
|---|---|---|---|
| NordVPN | `resolved_station_count(IP_ROTATION)` | stations `enabled` + `connected` | `usable_station_count()` |
| WARP | endpoints `provider=warp` | `enabled` | health + parked + quota |
| Proxys | endpoints non-WARP | `enabled` | health + bad-mark |
| Direct | 1 | 1 | 1 (toujours, si coché) |

Le MVP ne dépend ni de Docker ni d'un probe réseau : il lit la configuration
et les flags `enabled`. La Phase 2 branche `QuotaLedger`, `parked_ips` et les
probes existants.

### 3. Configuration

```yaml
ip_rotation:
  egress_capacity:
    target_slots: 6
    reserve_policy: full        # full | shared | proportional
```

- `target_slots` : 1–10, défaut 6.
- `reserve_policy` : défaut `full` (6 stations garanties même après panne
  d'un provider) ; `shared` est le mode économique ; `proportional` n'a de
  sens qu'en Répartition et est traité comme `shared` en Fallback avec un
  avertissement.
- Aucune duplication de `station_count` : il reste la source NordVPN.

### 4. API

`GET /api/egress/capacity`

```json
{
  "target_slots": 6,
  "strategy": "fallback",
  "reserve_policy": "full",
  "providers": {
    "nordvpn": {
      "configured": 6,
      "enabled": 6,
      "usable": 6,
      "weight": null,
      "expected_share": null,
      "per_endpoint": null,
      "deficit": 0,
      "status": "ok"
    },
    "warp": {
      "configured": 6,
      "enabled": 6,
      "usable": 6,
      "weight": null,
      "expected_share": null,
      "per_endpoint": null,
      "deficit": 0,
      "status": "ok"
    }
  },
  "total_usable": 12,
  "total_required": 12,
  "verdict": "ok",
  "warnings": [],
  "recommendations": [
    "Fallback sans dégradation : 6 NordVPN + 6 WARP. Vous avez 6 + 6."
  ]
}
```

`POST /api/egress/capacity` (Phase 2) : `{target_slots, reserve_policy}`.
L'application des comptes (`station_count`, activation WARP) reste une action
distincte et confirmée.

### 5. GUI

Carte « Capacité » dans `Modèles Gratuits > Egress & providers`, entre le
segmented control et la liste des providers.

```text
Capacité : 6 stations requises                    [ok]
┌──────────┬────────────┬──────────┬─────────┬──────────────┐
│ Provider │ Configurés │ Utilisables │ Poids │ Charge/endpoint │
├──────────┼────────────┼──────────┼─────────┼──────────────┤
│ NordVPN  │ 6          │ 6        │ 50 %    │ 8,3 %        │
│ WARP     │ 6          │ 6        │ 50 %    │ 8,3 %        │
│ Proxys   │ 0          │ 0        │ —       │ —            │
│ Direct   │ 1          │ 1        │ 0 %     │ secours      │
└──────────┴────────────┴──────────┴─────────┴──────────────┘
[████ NordVPN 6 slots ████][████ WARP 6 slots ████]  12 endpoints
Recommandation : 6 + 6 = redondance totale ; 3 + 3 suffirait pour 6 slots
mais perdrait la réserve.
```

- La barre empilée montre les **endpoints** (pas les poids).
- En Fallback, la colonne Poids est masquée.
- Un badge `Déficit 3` apparaît si `usable < requis`.
- Les boutons `Appliquer` (Phase 2) sont à droite, avec confirmation.

### 6. Fichiers touchés

| Fichier | Rôle |
|---|---|
| `free/capacity.py` | calculs purs, dataclasses |
| `config/settings.py` | lecture `egress_capacity` |
| `dashboard/api.py` | GET/POST `/api/egress/capacity` |
| `dashboard-ui/src/pages/vpn/VpnCapacity.svelte` | carte Capacité |
| `dashboard-ui/src/pages/vpn/VpnStatus.svelte` | intégration dans Egress & providers |
| `dashboard-ui/src/lib/locales.ts` | FR/EN |
| `config.yaml` | valeurs par défaut |
| `tests/test_capacity.py` | formules, verdicts, allocation |
| `tests/test_vpn_endpoints.py` | API |
| `specs/010-.../contracts/capacity.md` | contrat |

### 7. Tests

- `proportional_allocation(6, {50:50}) == {3,3}`.
- `proportional_allocation(6, {2:1}) == {4,2}`.
- `proportional_allocation(6, {3:1}) == {4,2}` (minimise la charge/endpoint).
- `proportional_allocation(6, {1:1:1}) == {2,2,2}`.
- `full` 6+6 Fallback → `ok`, 6 réserve.
- `full` 6+3 Fallback → `degraded`.
- `shared` 3+3 Répartition 50/50 → `ok`, warning « aucune réserve ».
- `shared` 2+3 → `insufficient`, déficit 1.
- Direct poids 20 % → warning.
- 6+3 Répartition 50/50 → warning déséquilibre 2×.
- API : GET renvoie le rapport ; POST persiste sans appliquer.
- GUI : `svelte-check` 0 erreur, build OK.

## Décisions par défaut

1. **Fallback** : `reserve_policy=full` → 6 NordVPN + 6 WARP. C'est la seule
   configuration qui garantit 6 stations après une panne complète du primaire.
2. **Répartition 50/50** : `reserve_policy=full` → 6 + 6 si les ressources le
   permettent. Sinon `shared` → 3 + 3, avec un avertissement explicite.
3. **Répartition avec poids inégaux** : `proportional` → répartition 4+2,
   6+3, etc., pour que chaque endpoint consomme son quota au même rythme.
4. **Direct** : jamais plus d'1 slot ; poids faible ou secours.
5. **Aucune application automatique** dans le MVP : le conseiller affiche,
   l'opérateur applique.

## Phases

### Phase 1 — Conseiller read-only (MVP)

Calculs purs + API GET + carte GUI + tests. Aucun démarrage/arrêt de
conteneur, aucun probe réseau. C'est la réponse visuelle à la question
« combien faut-il ? ».

### Phase 2 — Application contrôlée

- Appliquer `station_count` NordVPN depuis la cible.
- Activer/désactiver des endpoints WARP existants.
- Confirmation obligatoire pour toute création/suppression de conteneur.
- Brancher la santé réelle (parked, 403, quota).

### Phase 3 — Télémétrie et auto-équilibrage

- Part observée vs attendue par provider.
- Charge par endpoint en temps réel.
- Alerte si un endpoint dépasse 2× la charge médiane.
- Proposition de reweight ou de redimensionnement, jamais silencieuse.

## Risques

- **Confondre poids et capacité** : traité par la séparation explicite
  mode/poids/capacité/réserve.
- **Croire que 3+3 est obligatoire en Répartition** : faux ; 3+3 est un choix
  de capacité, pas une conséquence du mode.
- **Croire que 6+6 double le débit** : non, cela double la réserve et réduit
  la charge par endpoint ; le débit reste limité par la concurrence et les
  quotas.
- **WARP froid** : une bascule vers un conteneur arrêté n'est pas immédiate ;
  le MVP signale les endpoints désactivés, la Phase 2 introduit `hot/warm`.
- **Direct à poids élevé** : une seule IP ; warning obligatoire.
