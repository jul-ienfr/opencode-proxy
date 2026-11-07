# Contract — Capacité et dimensionnement

## Entrées

```yaml
ip_rotation:
  egress_capacity:
    target_slots: 6
    reserve_policy: full   # full | shared | proportional
  egress_providers: [nordvpn, warp]
  egress_strategy: fallback
  egress_weights: {nordvpn: 50, warp: 50}
```

## Notions

| Terme | Définition |
|---|---|
| `target_slots` | Nombre de stations client-utilisables visées (défaut 6) |
| `configured` | Endpoints présents dans la configuration |
| `enabled` | Endpoints non désactivés |
| `usable` | Endpoints sains, non parked, avec quota (MVP : `enabled`) |
| `weight` | Part de trafic (Répartition uniquement) |
| `per_endpoint` | `weight / usable` — vitesse relative d'épuisement du quota |
| `full` | Chaque provider sélectionné doit couvrir seul `target_slots` |
| `shared` | La somme des providers doit couvrir `target_slots` |
| `proportional` | Les endpoints suivent les poids, pour égaliser `per_endpoint` |

## Formules

### Full

```text
pour chaque provider sélectionné i :
  requis_i = target_slots
total_requis = target_slots * nombre_de_providers_sélectionnés
```

Fallback : le primaire est prioritaire, mais chaque provider doit pouvoir
servir seul en cas de panne.

### Shared

```text
total_requis = target_slots
capacité_après_panne_i = total_usable - usable_i
```

Fallback : le primaire doit avoir `>= target_slots` ; le secondaire peut être
inférieur, mais la capacité après bascule est affichée.

### Proportional (Répartition uniquement)

```text
minimiser max(weight_i / n_i) / min(weight_i / n_i)
sous contrainte sum(n_i) = target_slots, n_i >= 1 si weight_i > 0
```

Recherche exhaustive car `target_slots <= 10` et `providers <= 4`.

Exemples :

| Cible | Poids | Allocation | Charge/endpoint |
|---:|---|---|---|
| 6 | 50/50 | 3+3 | 16,7 % / 16,7 % |
| 6 | 67/33 | 4+2 | 16,7 % / 16,7 % |
| 6 | 75/25 | 4+2 | 18,75 % / 12,5 % |
| 6 | 33/33/33 | 2+2+2 | 16,7 % chacun |
| 6 | 40/40/20 | 3+2+1 | 13,3 % / 20 % / 20 % |

Direct est **fixe** : `n_direct = 1` si coché, et l'allocation des autres
providers se fait sur `target_slots - 1`. Si Direct a un poids > 0, un
avertissement de concentration est émis.

## Verdict

```text
si total_usable < target_slots                     -> insufficient
sinon si full et un provider requis a usable < target -> degraded
sinon si shared et capacité_après_panne < target   -> degraded
sinon                                              -> ok
```

`degraded` signifie : « les 6 slots sont couverts maintenant, mais pas après
la panne d'un provider ». `insufficient` signifie : « moins de 6 slots dès
maintenant ».

## Warnings

- `weight_i > 0` et `usable_i == 0` → provider demandé mais indisponible.
- `per_endpoint_max / per_endpoint_min > 2` → déséquilibre de quota.
- Direct avec `weight > 0` et `target_slots > 1` → une seule IP.
- `proportional` en Fallback → policy traitée comme `shared`.
- `target_slots` hors 1–10 → erreur de validation.

## API

### GET `/api/egress/capacity`

Retourne le rapport complet (voir `plan.md`). Aucun effet de bord.

### POST `/api/egress/capacity` (Phase 2)

```json
{"target_slots": 6, "reserve_policy": "full"}
```

Persiste uniquement les paramètres du conseiller. Ne modifie ni
`station_count`, ni les endpoints WARP : l'application est une action
distincte.

## GUI

- Carte `Capacité` dans `free-models/egress`.
- Tableau par provider + total + verdict.
- Barre d'endpoints (pas de poids) avec `target_slots` comme largeur de
  référence.
- Recommandation textuelle localisée.
- En Fallback : colonne Poids masquée.
- En Répartition : colonne Poids et `Charge/endpoint` visibles.
- Un clic sur `Appliquer` (Phase 2) demande confirmation.

## Non-objectifs

- Ne pas créer de compte payant.
- Ne pas provisionner automatiquement des conteneurs WARP dans le MVP.
- Ne pas déduire `station_count` des poids.
- Ne pas modifier le routage des requêtes dans la Phase 1.
