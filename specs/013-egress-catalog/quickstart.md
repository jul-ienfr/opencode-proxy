# Quickstart — Catalogue d'egress

## 1. Activer le catalogue

```yaml
egress_catalog:
  enabled: true
  providers:
    public:
      enabled: true
      max_active: 64
      sources_file: config/egress_sources.yaml
    warp:
      enabled: true
    nordvpn:
      enabled: true
      max_active_stations: 6
```

Les listes de proxys publics vivent dans `config/egress_sources.yaml`, pas
dans `config.yaml`. Voir [docs/EGRESS_SOURCES.md](../../docs/EGRESS_SOURCES.md)
pour la revue des sources et la notation des IP.

> Le scheduler ne démarre pas tant que `egress_catalog.enabled` n'est pas vrai.
> `scripts/autoscale_max_ips.py` n'est **pas** planifié ; c'est un one-shot de
> transition, pas le scheduler cible.

## 2. Cycle manuel

```powershell
python scripts/egress_catalog.py refresh --provider public
python scripts/egress_catalog.py refresh --provider warp
python scripts/egress_catalog.py refresh --provider nordvpn
python scripts/egress_catalog.py threat-feeds
python scripts/egress_catalog.py export
```

`threat-feeds` recharge la denylist (Feodo, Spamhaus DROP, FireHOL level1,
blocklist.de, ipsum) avant les probes. Un flux indisponible conserve ses
anciens indicateurs.

## 3. Vérifier

```powershell
curl.exe -s http://127.0.0.1:4000/api/egress/catalog
curl.exe -s "http://127.0.0.1:4000/api/egress/catalog/options?provider=public&status=usable&limit=50"
curl.exe -s "http://127.0.0.1:4000/api/egress/catalog/runs?limit=10"
```

## 4. Tests

```powershell
python -m pytest tests/test_egress_catalog.py tests/test_egress_store.py -q
python -m pytest tests/test_egress_public_adapter.py -q
python -m pytest tests/test_egress_warp_adapter.py -q
python -m pytest tests/test_egress_nordvpn_adapter.py -q
python -m pytest tests/test_egress_scheduler.py -q
python -m pytest -k "not docker" -q
```

## 5. Smoke live (opt-in)

```powershell
# Une seule source publique, 50 candidats max
python scripts/egress_catalog.py refresh --provider public --max-candidates 50

# WARP : uniquement les conteneurs existants, pas de création
python scripts/egress_catalog.py refresh --provider warp --observe-only

# NordVPN : observe les stations/IP, ne change pas station_count
python scripts/egress_catalog.py refresh --provider nordvpn --observe-only
```

## 6. Critères de sortie

- `GET /api/egress/catalog` montre au moins 1 option `usable` par provider
  activé.
- Aucune option `dead`/`auth` n'est sélectionnée par une rotation.
- Un `429` porte `reset_at` et n'est pas re-testé avant.
- Un cycle échoué ne vide pas le catalogue.
- Aucun `transport_url`/token dans l'API/UI.
