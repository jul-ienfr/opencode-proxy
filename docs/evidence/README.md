# Preuves — présélection IP

Ces fichiers sont **générés**, jamais édités à la main. Ils servent de base
aux chiffres cités dans `docs/AUDIT_PRESELECTION_QUOTA_2026-11-02.md` et
`docs/PLAN_CORRECTION_2026-11-02.md`.

Les fichiers `*-post-restart*.json` capturent l’état après application de
`requests.server_host` et rebuild qualité (attribution non nulle).

## Régénérer

```bash
python scripts/ip_preselection.py --hours 24  --top 15 --output docs/evidence/ip-preselection-2026-11-02-24h.json
python scripts/ip_preselection.py --hours 72  --top 15 --output docs/evidence/ip-preselection-2026-11-02-72h.json
python scripts/ip_preselection.py --hours 168 --top 15 --output docs/evidence/ip-preselection-2026-11-02-168h.json
```

Le script lit `logs/requests.db` (colonne `server_host` si elle existe) et
l'API NordVPN (pays, recommendations). En cas d'échec API, un pays est signalé
sur stderr ; le rapport JSON reste produit avec les compteurs DB et les notes.

## Champs

- `generated_at`, `hours`, `thresholds` : horodatage et seuils A/B/C.
- `api` : nombre de serveurs online et distribution `load`.
- `api_errors` : erreurs de source (`source_unavailable`) au lieu d’un vide
  silencieux.
- `db` : IP distinctes, hostnames distincts, requêtes exploitables.
- `matching` : appariement hostname (fiable) vs IP d'entrée (legacy, non fiable).
- `tiers` : répartition A/B/C des candidats.
- `top_ips`, `top_candidates` : classements.
- `notes` : annotations d'audit (attribution, `ip_registry.enabled=false`,
  `no-adapter:nordcache_packed`).

## Règle

Un chiffre sans fichier JSON ou commande reproductible ne doit pas entrer dans
un rapport. Les fenêtres sont glissantes : deux exécutions à des heures
différentes peuvent différer légèrement, c'est attendu.
