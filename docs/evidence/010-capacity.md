# Evidence — 010 Capacité par provider et par mode

**Date**: 2026-11-07
**Feature**: `specs/010-egress-station-sizing/`
**Scope**: conseiller read-only ; aucun changement de routage.

## Build / tests

```text
python -m pytest tests/test_capacity.py tests/test_vpn_endpoints.py
  -> 62 passed
python -m ruff check free/capacity.py free/pool.py dashboard/api.py tests/...
  -> OK
npm --prefix dashboard-ui run check -> 0 errors, 0 warnings
npm --prefix dashboard-ui run build -> OK
```

## API

`GET /api/egress/capacity` retourne `target_slots`, `strategy`,
`reserve_policy`, les compteurs `configured/enabled/usable` par provider,
`required`, `per_endpoint`, le verdict et les messages FR/EN.

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
