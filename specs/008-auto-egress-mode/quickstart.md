# Quickstart — Auto-Egress

**But**: valider le mode multi-provider sans casser les modes existants.

## Prérequis

- Feature 007 (provider abstraction, erreurs, ledger) implémentée.
- NordVPN, WARP et/ou proxys configurés.
- Proxy sur `http://127.0.0.1:4000`.
- `config.yaml` avec `ip_rotation.auto_egress`.

## 1. Tests offline

```powershell
python -m pytest tests/test_auto_egress_mode.py tests/test_provider_selector.py -q
python -m ruff check .
```

## 2. Cocher les providers

GUI : `Modèles Gratuits` → section **Egress providers** → cocher **WARP** et
**NordVPN** (laisser Proxys/Direct décochés).

API :

```powershell
curl.exe -sS -X POST http://127.0.0.1:4000/api/vpn/proxy-mode `
  -H "Content-Type: application/json" `
  -d '{\"providers\":[\"warp\",\"nordvpn\"]}'
```

Vérifier :

```powershell
python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:4000/api/vpn-status')); print(d['proxy_mode'], d.get('auto_egress'))"
```

Attendu : `selected_providers=["warp","nordvpn"]`, `derived_mode=auto_egress`,
providers listés, `degraded=false`.

## 3. Répartition multi-provider

Envoyer 20 requêtes free (ping mimo) avec des sessions distinctes :

```powershell
1..20 | ForEach-Object {
  curl.exe -sS -m 90 -o NUL -w "%{http_code}`n" -X POST http://127.0.0.1:4000/v1/chat/completions `
    -H "Content-Type: application/json" `
    -H "x-opencode-session: auto-$_" `
    --data-binary "@C:\Users\julie\AppData\Local\Temp\opencode\ping.json"
}
```

Attendu : ≥ 2 providers ont servi, > 99 % de `200`, attribution visible dans
`/api/free-health`.

## 4. Provider down

Désactiver NordVPN dans le GUI (ou couper le conteneur). Envoyer 10 requêtes.

Attendu : WARP/proxy servent, `degraded=false` si au moins deux providers
restent sains ; `degraded=true` avec raison si un seul reste.

## 5. 403 vs 429

- Injecter un `403` via un provider de test (`tests/fixtures`).
  Attendu : aucune rotation, aucun bloc marqué, provider toujours disponible.
- Injecter un `429 FreeUsageLimitError` sur WARP.
  Attendu : bloc WARP parké/roté, NordVPN/proxy continuent.

## 6. Direct fail-closed

Désactiver tous les providers, `allow_direct=false`.

Attendu :

```json
{"degraded": true, "reason": "no_provider_available"}
```

Aucune requête directe.

## 7. Poids

Configurer WARP 60 / NordVPN 30 / proxy 10, envoyer 100 requêtes.

Attendu : répartition à ±10 points, caps respectés.

## 8. Persistance

Redémarrer le proxy.

Attendu : `selected_providers` restaurés, `derived_mode=auto_egress`,
providers/poids/caps restaurés.

## 9. Tests complets

```powershell
python -m pytest -k "not docker" -q
npm --prefix dashboard-ui run check
npm --prefix dashboard-ui run build
```

## Critères de sortie

- Mode visible, persisté, sans interruption au switch.
- ≥ 2 providers servent ; > 99 % de succès.
- 403 → 0 rotation ; 429 → park/rotate local.
- Direct interdit sans `allow_direct`.
- Attribution et répartition visibles.
