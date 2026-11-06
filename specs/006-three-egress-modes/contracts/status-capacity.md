# Contract — Status & capacity per mode

**Feature**: `006-three-egress-modes`

## Réponse

`StatusSnapshot` est calculé sur le backend du mode actif. Aucun mélange
Docker/WARP.

| Mode | `source` | `configured` | `usable` | Lignes |
|---|---|---|---|---|
| NordVPN | `stations` | nombre de tunnels Docker | stations `station_usable` | managers Docker |
| WARP | `endpoints` | endpoints activés | endpoints sélectionnables | labels WARP, egress, bloc |
| Direct | `none` | 0 | 0 | aucune |

## Règles

- En WARP, `capacity_snapshot()` ne lit jamais `_stations` (Docker vide).
- En WARP, `configured` compte les endpoints `enabled` (managés ou non).
- `usable` applique les bad-marks et le plancher de secours ; un endpoint
  `degraded_override` reste compté usable.
- `unique_ips` exclut les identifiants logiques `socks5:<label>`.
- En Direct, `capacity_degraded=false` et `capacity_reason=null` ; la GUI
  affiche « aucune station » plutôt que 0/1.
- Un `duplicate_stations > 0` reste signalé dans le mode concerné.
- `/health` ne doit pas être `degraded` à cause d'une capacité Docker vide en
  WARP.

## Tests d'acceptation

1. WARP 7/7 ⇒ `configured=7`, `usable=7`, `capacity_degraded=false`.
2. WARP 3/7 ⇒ `capacity_reason="3/7 endpoints usable"`.
3. NordVPN 2/2 ⇒ capacité Docker 2/2.
4. Direct ⇒ aucune capacité de station, pas de faux `0/1`.
5. Un endpoint WARP avec `bad_remaining>0` mais `degraded_override=true` reste
   compté usable.
