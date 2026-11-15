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

## Preuves live (2026-11-15)

| Heure | Mesure |
|---|---|
| 12:25 → 12:31 | **12/12 requêtes client en 200**, `client_429_total = 0`, réserve 4 → 7 |
| 12:31 → 12:35 | réserve 7 → 8, `client_usable` 4/6 → **6/6 à 12:35:04** |
| 12:35:34 | retour à 4/6 : la flotte fluctue encore avec le quota upstream |

Latence client : 1,1 à 2,6 s sur 11 requêtes, un outlier à 40,6 s (bascule
interne). Le zéro 429 est tenu quand une IP réservée est liée à une station.

## 429 raisonnés (T009)

Le client reçoit toujours un 429 JSON avec `Retry-After`, un champ `reason` et
l'en-tête `X-Free-Reason` :

| `reason` | Sens |
|---|---|
| `reserve_empty` | Aucune IP de réserve liée à une station ; un fallback WARP/proxy existe |
| `all_fallbacks_dead` | Réserve vide **et** aucun WARP/proxy utilisable |
| `upstream_429` | Une IP réservée est servie (ou en cooldown) mais l'amont a refusé |

Compteur : `client_429_total{reason="..."}`. Un 429 vide n'est plus possible.

## Anti-churn (correctif 2026-11-15)

Deux gardes empêchent la flotte de se détruire elle-même :

- `allocator.refresh_min_available = 3` : aucune rotation proactive tant que
  moins de 3 stations ont du quota. La réparation des IPs 429 reste active.
- Une station `health=suspect` / `settling` n'est jamais rotationnée : on
  attend qu'elle soit confirmée `usable`.

Réconciliation ledger ↔ observations : toutes les 60 s, un 200 réel plus récent
qu'un 429 débloque le bucket (`_reconcile_free_ledger_once`). Sans elle, un
POST `/quota/mark` perdu pendant un redémarrage laissait l'IP bloquée ~19 min.

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
