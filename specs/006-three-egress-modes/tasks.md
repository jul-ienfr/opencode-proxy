# Tasks — Trois modes d'egress

**Feature**: `006-three-egress-modes` | **Statut**: implémenté le 2026-11-06

## Mode et présentation

- [x] **T-001** Remplacer les libellés de mode par `NordVPN`, `WARP`,
  `Direct` (FR/EN), sans changer les valeurs internes `vpn`/`socks5`/`direct`.
- [x] **T-002** Séparer `VpnRouting` en :
  - panneaux NordVPN-only (stack, sélection serveur, nombre de stations) ;
  - panneau **Routage free commun** toujours visible ;
  - mention explicite en mode Direct.
- [x] **T-003** Masquer `VpnServers` (identifiants/serveurs/mode Docker) hors
  mode NordVPN.
- [x] **T-004** `VpnAdvanced` :
  - HTTPS NordVPN visible seulement en mode NordVPN ;
  - stations WARP visibles seulement en mode WARP ;
  - `max_load` visible seulement en mode NordVPN ;
  - probe post-rotation visible seulement en mode WARP ;
  - qualité/latence communes toujours visibles.
- [x] **T-005** `VpnStatus` :
  - capacité et lignes adaptées au mode ;
  - station Docker masquée en WARP/Direct ;
  - hint WARP / hint Direct ;
  - rotation proactive masquée hors NordVPN.

## Mode switch et fraîcheur

- [x] **T-006** Sur clic de mode, invalider le tag `vpn` pour rafraîchir
  `/api/vpn-config` dans `VpnRouting`, `VpnServers` et `VpnAdvanced` sans
  rechargement manuel.
- [x] **T-007** Vérifier la persistance du mode après rechargement.

## Backend

- [x] **T-008** `FreeIPPool.get_status()` retourne, en mode SOCKS5, les
  endpoints WARP comme lignes de statut, une capacité `usable/configured`
  réelle et `with_quota` par modèle (cohérent avec `/health`), plus de
  « Error 0/1 ».
- [x] **T-009** Conserver `capacity_snapshot()` mode-aware pour `/health`.

## Validation

- [x] **T-010** `npm run check` : 0 erreur, 0 warning.
- [x] **T-011** `npm run build` : build Svelte OK.
- [x] **T-012** Validation navigateur :
  - WARP actif → capacité 7/7, section commune, stations WARP, probe visibles,
    NordVPN config/HTTPS absents ;
  - NordVPN actif → sections NordVPN visibles, WARP absent ;
  - Direct actif → section commune + mention « non applicable », aucune
    section NordVPN/WARP.
- [x] **T-013** `pytest -k "not docker"` : exit 0.

## Suivi

- `post_rotation_probe` reste WARP-only (le probe Docker n'existe pas).
- `quality_preselection.max_load` reste NordVPN-only.
- `rotation_planner` reste NordVPN-only.
- `socks5_fallback_enabled` reste un actif WARP utilisé comme fallback en mode
  NordVPN.
