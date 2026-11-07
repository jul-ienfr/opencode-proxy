# Evidence — 010 Capacité par provider et par mode

**Date**: 2026-11-07
**Feature**: `specs/010-egress-station-sizing/`
**Scope**: conseiller read-only ; aucun changement de routage.

## Build / tests

```text
python -m pytest tests/test_capacity.py tests/test_vpn_endpoints.py
  -> 64 passed
python -m ruff check free/capacity.py free/pool.py dashboard/api.py tests/...
  -> OK
npm --prefix dashboard-ui run check -> 0 errors, 0 warnings
npm --prefix dashboard-ui run build -> OK
```

## État final validé — WARP seul

Après les tests, le provider actif est revenu à **WARP seul** (`proxy_mode=socks5`,
`egress_providers=["warp"]`) : c'est l'état stable qui sert réellement les six
stations.

```json
{
  "target_slots": 6,
  "strategy": "fallback",
  "reserve_policy": "full",
  "verdict": "ok",
  "total_usable": 6,
  "total_required": 6
}
```

| Provider | Configurés | Activés | Utilisables | Requis | Statut |
|---|---:|---:|---:|---:|---|
| NordVPN | 6 | 6 | 0 | — | Décoché |
| WARP | 6 | 6 | 6 | 6 | OK |
| Proxys | 2 | 0 | 0 | — | Décoché |
| Direct | 1 | 1 | 1 | — | Décoché |

Résultat GUI :

```text
6 / 6 stations
Requis : 6 endpoints
Verdict : OK
Redondance totale : WARP 6. Chaque provider peut couvrir seul 6 stations.
```

NordVPN reste visible avec `0` utilisable, mais **décoché** : il ne dégrade pas
le verdict. C'est exactement la sémantique attendue — un provider non coché
n'est ni une erreur ni une capacité requise.

### Cas dégradé (test précédent)

Avec `NordVPN + WARP + Proxys` cochés et NordVPN à 0/6 :

```text
Verdict : Dégradé
Requis : 18 endpoints (3 providers × 6)
```

La carte distingue donc bien « 6 stations couvertes maintenant » de
« redondance complète ».

## API

`GET /api/egress/capacity` retourne `target_slots`, `strategy`,
`reserve_policy`, les compteurs `configured/enabled/usable` par provider,
`required`, `per_endpoint`, le verdict et les messages FR/EN.

`POST /api/egress/capacity` persiste uniquement
`{"target_slots": 1..10, "reserve_policy": "full|shared|proportional"}`.
Testé : `full` → `shared` → le requis passe de 18 à 6 endpoints, puis retour
à `full`. Aucun conteneur ni endpoint n'est modifié.

État live testé :

```json
{
  "target_slots": 6,
  "strategy": "fallback",
  "reserve_policy": "full",
  "verdict": "degraded",
  "total_usable": 6,
  "total_required": 18
}
```

Détail :

| Provider | Configurés | Activés | Utilisables | Requis | Statut |
|---|---:|---:|---:|---:|---|
| NordVPN | 6 | 6 | 0 | 6 | Indisponible |
| WARP | 6 | 6 | 6 | 6 | OK |
| Proxys | 2 | 0 | 0 | 6 | Indisponible |
| Direct | 1 | 1 | 1 | — | Décoché |

Lecture : 6 stations sont couvertes **maintenant** par WARP, mais la
redondance `full` n'est pas satisfaite (NordVPN et Proxys ne peuvent pas
couvrir seuls 6 stations). C'est exactement le cas que le conseiller doit
rendre visible.

## Répartition testée

Poids 50/30/20, mêmes providers :

```text
NordVPN  utilisable 0  poids 50 %  charge/endpoint —
WARP     utilisable 6  poids 30 %  charge/endpoint 5,0 %
Proxys   utilisable 0  poids 20 %  charge/endpoint —
Direct   décoché       poids —     charge/endpoint —
```

Le provider indisponible est retiré du calcul de charge ; le poids reste
affiché pour montrer la configuration demandée. Direct décoché n'affiche pas
« Secours » (poids `null`, pas `0`).

## GUI

Carte `Capacité` dans `Modèles Gratuits > Egress & providers` :

- En-tête : titre + verdict (`OK`, `Dégradé`, `Insuffisant`).
- Contrôles : cible `target_slots` (1–10), politique de réserve
  (`Redondance totale`, `Capacité partagée`, `Proportionnelle aux poids`),
  bouton `Appliquer`.
- Résumé : `6 / 6 stations`, `Requis : 18 endpoints`, déficit éventuel.
- Tableau : Provider, Configurés, Activés, Utilisables, Poids,
  Charge/endpoint, Requis, Statut.
- Barre d'endpoints : segments par provider + marqueur de cible.
- Recommandation : « Pour la redondance totale, il manque : NordVPN 6,
  Proxys SOCKS5/HTTPS 6. »
- Alertes : endpoints désactivés, provider sélectionné sans endpoint
  utilisable, redondance incomplète, déséquilibre > 2×, Direct à poids élevé.
- En Fallback, les colonnes Poids et Charge/endpoint sont masquées ; en
  Répartition elles apparaissent.

## Non-objectifs respectés

- Aucun conteneur démarré/arrêté par le conseiller.
- Aucun probe réseau supplémentaire : `usable` vient de
  `usable_station_count()` et des marques bad/quota existantes.
- Aucune modification du routage des requêtes.
- Aucun provider payant, aucun second compte.
