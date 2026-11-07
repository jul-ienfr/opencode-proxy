# Quickstart — WARP primaire + replis d'egress free

**But**: valider la feature sans réseau/docker pour l'unité, puis avec des
scénarios live contrôlés.

## Prérequis

- Python 3.12+, `pytest`, `ruff`.
- Proxy lancé sur `http://127.0.0.1:4000`.
- WARP containers disponibles pour les tests live.
- `config.yaml` avec `ip_rotation.fallback` et `egress_providers`.
- Aucun test live ne doit être lancé sans accepter de consommer du quota.

## 1. Tests offline

```powershell
python -m pytest -k "not docker" -q
python -m ruff check .
```

Attendu : `exit 0`, aucun warning bloquant.

## 2. Taxonomie d'erreurs

```powershell
python -m pytest tests/test_error_taxonomy.py -q
```

Vérifie :

- `403` → `GATE_403`, ledger inchangé, rotation non appelée ;
- `429 FreeUsageLimitError` → `QUOTA_429`, bloc épuisé ;
- `429 RateLimitError` → `RATE_LIMIT_429`, pas d'exhaustion ;
- `5xx` → `TRANSIENT_5XX`, pas de fallback.

## 3. Fallback simulé

```powershell
python -m pytest tests/test_fallback_policy.py -q
```

Scénarios :

1. WARP épuisé → une station fallback sert 10/10 `200`.
2. `403` → aucune bascule.
3. 2 probes WARP `200` → retour WARP après cooldown.
4. fallback payant désactivé → `degraded`, pas de direct.

## 4. Dédup et rotation

```powershell
python -m pytest tests/test_egress_provider.py tests/test_warp_rotation.py -q
```

Vérifie :

- deux adresses du même `/64` = un seul bloc ;
- `rotate` avec un bloc dupliqué → rejet ;
- probe `False` → `quota_exhausted`, IP trackée ;
- famille IPv4 rejetée sur endpoint `ipv6` ;
- `current_ip` jamais stale.

## 5. Live WARP (contrôlé)

```powershell
# Statut
python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:4000/api/vpn-status')))"

# Rotation des blocs épuisés
curl.exe -sS -X POST http://127.0.0.1:4000/api/vpn/socks5/rotate-egress -H "Content-Type: application/json"

# Ping free (mimo)
curl.exe -sS -m 90 -X POST http://127.0.0.1:4000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "x-opencode-session: quickstart-mimo" `
  --data-binary "@C:\Users\julie\AppData\Local\Temp\opencode\ping.json"
```

Attendu :

- `cap 6/6` (ou dégradé explicite) ;
- `with_quota` honnête ;
- `mimo` → `200` ;
- aucun bucket `socks5:*` ;
- aucun doublon de bloc.

## 6. Phase 0 — granularité du quota

Suivre `PLAN_ALTERNATIVE_WARP_2026-11-06.md` §2.2 et produire
`docs/QUOTA_GRANULARITY_2026-11-07.md` avec :

- baseline `403` vs `429` ;
- adresse `A` vs `B` du même `/64` ;
- `/64` distinct ;
- contrôle IPv4 `/32` ;
- deux modèles free ;
- `Retry-After`, heure UTC, recovery réelle.

**Ne pas** acheter/activer un VPS avant le go/no-go.

## 7. Gateway VPS (optionnel)

```powershell
python scripts/ipv6_egress_gateway.py --prefix 2001:db8:abcd::/48 --port-base 11090
```

Puis :

```powershell
curl.exe -sS http://127.0.0.1:8099/health
curl.exe -sS -X POST http://127.0.0.1:8099/allocate -H "Authorization: Bearer $env:IPV6_GATEWAY_TOKEN" -H "Content-Type: application/json" -d '{\"label\":\"vps-1\",\"port\":11090}'
curl.exe -sS -X POST http://127.0.0.1:8099/rotate -H "Authorization: Bearer $env:IPV6_GATEWAY_TOKEN" -H "Content-Type: application/json" -d '{\"label\":\"vps-1\"}'
```

Attendu : 3 rotations, 3 blocs distincts, 3 × `200`, rotation < 10 s.

## 8. Critères de sortie

- Tests offline verts.
- `403` ne tourne pas.
- WARP reste majoritaire.
- Fallback gratuit sert 10/10.
- Aucun doublon.
- Dashboards provider/bloc/état fonctionnels.
- Phase 0 documentée.
