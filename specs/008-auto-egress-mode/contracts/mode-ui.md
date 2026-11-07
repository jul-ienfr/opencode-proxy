# Contract — Mode UI

## Sélecteur

Quatre modes, dans cet ordre :

```text
NordVPN | WARP | Direct | Auto-Egress
```

- `Auto-Egress` est un bouton radio, pas un menu caché.
- Le libellé reste « Auto-Egress » en FR et EN (nom produit).
- Sous-titre : « NordVPN + WARP + proxys, sélection automatique ».
- L'état sélectionné est persisté dans `config.yaml`.
- Au reload/restart, le mode est restauré.

## Panneau Auto-Egress

Affiche :

- providers inclus (WARP, NordVPN, proxys) avec toggle ;
- poids (somme 100, validation) ;
- `max_concurrent` par provider ;
- hedge delay/attempts ;
- `allow_direct` (défaut off, confirmation explicite) ;
- `sticky` et TTL ;
- état `degraded` + raison.

## Matrice option → consommateur

| Option | Clé | Consommateur | Effet | Test |
|---|---|---|---|---|
| Activer Auto-Egress | `auto_egress.enabled` | `_free_proxy_mode` | pool unifié | `test_auto_egress_mode` |
| Provider WARP | `providers` | `build_pool` | inclus/exclu | `test_provider_selector` |
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
