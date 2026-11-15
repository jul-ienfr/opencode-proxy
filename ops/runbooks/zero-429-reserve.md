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
| **14:06 → 14:11** | **6/6 `model_usable` et 6/6 `health=usable` tenus 4 min**, réserve 9-11, **3/3 requêtes client en 200** (1,6–2,2 s) |
| **14:52 → 14:55** | **6/6 tenu**, six conteneurs Docker `healthy` (uptime 5-29 min), **3/3 requêtes client en 200** (2,2–2,4 s), réserve 12 |
| **21:30 → 21:40** | correctif de périmètre réserve : **WARP usable=9–12/13**, `socks5_current` = WARP 3/5/12 IPv6, **réserve 0** et **6/6 stations NordVPN en 429**, pourtant **4/4 requêtes client en 200** (3,1–3,3 s), `client_429_total = 0` |
| **21:48** | après reset : **réserve 6/6** et **6/6 `model_usable`**, **WARP toujours usable=9/13** (avant le correctif : 0), **2/2 requêtes client en 200** (3,0 s) |

Le 6/6 de 14:06 est le premier tenu après les correctifs anti-churn :
`refresh_min_available=3`, rotation réservée aux stations `health=usable`,
grâce de 75 s après un lancement de chaîne, `winner_cooldown_s=120`,
transports forcés en WireGuard, et réconciliation ledger ↔ observations.

Le 6/6 de 14:52 ajoute le correctif DNS : les tunnels utilisaient DoT vers
Cloudflare/Google/Quad9, bloqué sur certains serveurs NordVPN
(`DNS ladder exhausted`, `lookup checkip.amazonaws.com: server misbehaving`).
Passage en **plain UDP vers le DNS NordVPN** (`103.86.96.100`,
`103.86.99.100`) : plus de healthcheck DNS en échec, donc plus de station
marquée `tunnel_down` alors que le tunnel fonctionne.

Le correctif de 21:30 restreint le filtre réserve aux stations Docker. Avant,
dès qu'une IP NordVPN entrait dans `client_reserve`, tous les endpoints WARP
devenaient « unusable » et disparaissaient de la GUI ; WARP ne revenait qu'à
réserve vide, c'est-à-dire quand NordVPN était mort. Le test
`test_reserve_scope_is_docker_stations_only` verrouille ce périmètre.

Deux états ont été mesurés après le correctif :

- réserve 0, six stations NordVPN en 429 → 4/4 requêtes client en 200 via WARP ;
  avant, cet état produisait des 429 `all_fallbacks_dead` pendant tout
  l'intervalle de reset.
- réserve 6/6, six stations `model_usable` → WARP reste `usable=9/13` ; avant,
  la même réserve non vide mettait WARP à 0.

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
| WARP « unusable » alors que les conteneurs sont sains | la réserve ne filtre que les stations Docker ; vérifier `bad_until` / ledger WARP, pas `client_reserve` |
| réserve qui ne remonte pas | `logs/fresh_chain_supervisor.log` : `client_reserve=` et `ALERT` |
| IP réservée mais 429 | `quota_buckets` : `last_status=429`, `reset_at` futur → elle doit sortir |
| probes qui consomment la réserve | `fresh_chain.py` doit exclure `_reserved_client_ips()` |

## Ne pas faire

- Ne pas ajouter une IP non prouvée à la réserve.
- Ne pas laisser une IP 429 dans `client_reserve` (le filtre le fait).
- Ne pas modifier `direct_allowed` sans décision opérateur.
- Ne pas dépasser six stations.
