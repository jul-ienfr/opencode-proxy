# Contract — IPv6 Egress Gateway API (DIFFÉRÉ, payant, hors périmètre)

> **Non implémenté dans cette itération.** Ce contrat documente une option
> payante future. Aucun achat, aucun VPS, aucun gateway ne doit être déployé
> avant une décision explicite de l'opérateur.

**But**: exposer une rotation de bloc IPv6 en quelques secondes, sans recréer
de conteneur, tout en gardant le ledger exact (source stable par `/64`).

## Sécurité

- Écoute uniquement sur `127.0.0.1` (ou tunnel SSH/WireGuard).
- Token obligatoire : `Authorization: Bearer <IPV6_GATEWAY_TOKEN>`.
- Aucun secret dans les logs.
- Firewall : seul SSH/WireGuard exposé.

## Endpoints

### `GET /health`

```json
{
  "ok": true,
  "prefix": "2001:db8:abcd::/48",
  "allocated": 6,
  "free": 65530,
  "gateway": "up"
}
```

### `POST /allocate`

```json
{ "label": "vps-1", "port": 11090 }
```

Réponse `201` :

```json
{
  "label": "vps-1",
  "block": "2001:db8:abcd:0007::/64",
  "source_ip": "2001:db8:abcd:0007::2",
  "port": 11090
}
```

### `POST /rotate`

```json
{ "label": "vps-1" }
```

Réponse `200` :

```json
{
  "label": "vps-1",
  "old_block": "2001:db8:abcd:0007::/64",
  "new_block": "2001:db8:abcd:0008::/64",
  "source_ip": "2001:db8:abcd:0008::2",
  "port": 11090,
  "duration_ms": 2400
}
```

### `POST /release`

```json
{ "label": "vps-1" }
```

Réponse `204`. La libération est explicite ; un bloc libéré n'est pas
réutilisé avant le reset fournisseur.

## Invariants

1. Un label = un bloc = un port = une source stable.
2. Deux labels ne partagent jamais un bloc.
3. Pas de réseau `::`, multicast, link-local.
4. Rotation < 10 s ; drain des connexions avant rebind.
5. Persistance SQLite ; rechargement au boot.
6. Si le gateway est down, l'endpoint est bad-marké et WARP reprend le relais.
7. Aucun fallback direct silencieux.

## Erreurs

| Code | Sens |
|---|---|
| 400 | label/port invalide |
| 401 | token absent/incorrect |
| 404 | label inconnu |
| 409 | bloc/port déjà alloué |
| 503 | gateway down / préfixe indisponible |
