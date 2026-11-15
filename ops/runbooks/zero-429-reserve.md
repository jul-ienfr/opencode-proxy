# Runbook — réserve client et zéro 429

**Feature** : 022-zero-429-client-reserve
**Date** : 2026-11-15

## Principe

La réserve client (`client_reserve` dans `registry.db`) contient les IPs
prouvées 200. Le pool client ne sert que ces IPs ; les probes ne les
consomment jamais. Le superviseur remplit la réserve en arrière-plan.

## Vérifier la réserve

```powershell
python -c "from free.client_reserve import count, reserved_ips; print(count(), sorted(reserved_ips()))"
```

Ou via `/metrics` :

```
client_reserve_ips 3
client_429_total 0
```

## Ce qui se passe quand la réserve est basse

- `reserve < 2` : alerte `ALERT client_reserve=X (<2) — probes suspendues`.
- Les probes sont suspendues, le client est prioritaire.
- Le superviseur qualifie de nouvelles IPs en arrière-plan (mode shadow).
- Une IP épuisée (429 dans `quota_buckets`) sort automatiquement de la réserve.

## Ce qui se passe quand la réserve est vide

1. Le pool client tente WARP, puis les proxys.
2. Le direct n'est utilisé que si `direct_allowed` l'autorise.
3. Si tout est épuisé : `429` JSON avec `Retry-After` et `rate_limit_error`.

## Amorcer la réserve

```powershell
python logs\seed_client_reserve.py
```

Le script lit `/api/vpn-status`, prend les stations `model_usable`, et ajoute
leurs IPs à la réserve avec un TTL d'une heure.

## Diagnostic

| Symptôme | Vérifier |
|---|---|
| clients en 429 alors que des stations sont up | `reserved_ips()` contient-il ces IPs ? |
| réserve qui ne remonte pas | `logs/fresh_chain_supervisor.log` : `client_reserve=` et `ALERT` |
| IP réservée mais 429 | `quota_buckets` : `last_status=429`, `reset_at` futur → elle doit sortir |
| probes qui consomment la réserve | `fresh_chain.py` doit exclure `_reserved_client_ips()` |

## Ne pas faire

- Ne pas ajouter une IP non prouvée à la réserve.
- Ne pas laisser une IP 429 dans `client_reserve` (le filtre le fait).
- Ne pas modifier `direct_allowed` sans décision opérateur.
- Ne pas dépasser six stations.
