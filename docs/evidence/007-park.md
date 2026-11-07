# Evidence — 007 park/reset et provider WARP

**Date**: 2026-11-07
**Feature**: `specs/007-warp-egress-fallback/`
**Scope**: US1 park/reset + foundation provider.

## Tests

```text
pytest tests/test_warp_rotation.py tests/test_quota_ledger.py \
       tests/test_fallback_config.py -q  -> 49 passed
pytest -k "not docker" -q              -> exit 0
ruff                                   -> All checks passed
```

Scénarios couverts :

- reset proche (< `park_window_s`) + autre endpoint servable → **park**,
  aucune rotation ;
- reset lointain → **rotation** ;
- reset proche mais aucun autre endpoint → **rotation** (pas d'attente) ;
- `parked_ips()` ne retourne que les blocs dont le reset est dans la fenêtre ;
- `WarpDockerProvider` convertit `WarpRotationResult` en
  `EgressRotationResult` sans changer le comportement Docker ;
- doublon de bloc et `rotation_rejected` restent couverts par les tests
  existants.

## Live

Après redémarrage :

```text
status connected · 6/6 stations · with_quota 6/6
/api/free-egress/diag -> parked/reset_at/next_probe_at présents
```

Aucun bloc n'était parké au moment du relevé (tous avaient du quota) ; les
champs sont présents et à `null`.

## Notes

- `park_window_s` vit dans `ip_rotation.fallback` (défaut 1800 s).
- `ledger.max_block_s` reste l'exception de probe anticipé ; le park est la
  politique par défaut quand le reset est proche.
- Le provider WARP est un adapter du rotateur existant ; aucun second moteur.
