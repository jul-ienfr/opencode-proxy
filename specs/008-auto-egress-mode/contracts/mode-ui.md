# Contract — Mode UI

## Section « Egress providers »

Cases à cocher, dans cet ordre :

```text
[ ] NordVPN
[ ] WARP
[ ] Proxys SOCKS5/HTTPS
[ ] Direct (local)  ⚠️ expose l'IP résidentielle
```

- L'opérateur peut cocher 1 à 4 providers.
- Une seule case cochée → comportement legacy exact.
- Plusieurs cases cochées → moteur `auto_egress`.
- Zéro case cochée → sauvegarde refusée avec « au moins un provider ».
- La sélection est persistée comme liste (`egress.providers`).
- Au reload/restart, la sélection est restaurée.

## Panneau Auto-Egress (si ≥2 providers)

Affiche :

- providers cochés avec poids ;
- poids (somme 100, validation) ;
- `max_concurrent` par provider ;
- hedge delay/attempts ;
- `allow_direct` (défaut off, confirmation explicite) ;
- `sticky` et TTL ;
- état `degraded` + raison.

## Matrice option → consommateur

| Option | Clé | Consommateur | Effet | Test |
|---|---|---|---|---|
| Cocher NordVPN/WARP/Proxys/Direct | `egress.providers` | `_free_proxy_mode` / `build_pool` | pool unifié ou legacy | `test_auto_egress_mode` |
| Provider coché | `selected_providers` | `build_pool` | inclus/exclu | `test_provider_selector` |
| Poids | `weights` | `provider_selector` | répartition | `test_auto_egress_weights` |
| Cap concurrence | `max_concurrent` | sémaphores | limite | `test_auto_egress_caps` |
| Hedge | `hedge_*` | retry | tentatives | `test_auto_egress_hedge` |
| Direct | `allow_direct` | sélection | direct/fail-closed | `test_auto_egress_direct` |
| Sticky | `sticky` | affinité | cache | `test_auto_egress_sticky` |

## Règles

- Aucune option sans consommateur runtime.
- Une valeur invalide (poids ≠ 100) est normalisée avec warning, pas ignorée
  silencieusement.
- Le mode ne change pas sans drain des requêtes en vol.
- Le GUI affiche la valeur effective, pas seulement la valeur enregistrée.
