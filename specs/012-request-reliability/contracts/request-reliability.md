# Contrat — Fiabilité des requêtes free

## C1 — Modes du pool

```text
_FREE_POOL_MODES = ("vpn", "socks5", "auto_egress")
```

- `auto_egress` est un mode pool valide.
- Le `503` « pool indisponible » n'est émis que si :
  - le pool est absent, ou
  - `pool.enabled` est faux, ou
  - aucun endpoint n'est sélectionnable.
- Le message doit distinguer « pool vide » de « mode non supporté ».

## C2 — Sélection des endpoints

- Un endpoint `managed=true`, `provider=warp`, `ipv4=false` ne déclenche
  jamais de rotation de conteneur.
- Un endpoint IPv4 peut être sélectionné comme canari, mais ne compte pas
  comme capacité garantie.
- Les buckets IPv6 `/64` restent les porteurs de quota.

## C3 — Réponse SystemOne free

`POST /v1/systemone` avec `model=jev-1.13-free` :

| Condition | Réponse |
|---|---|
| pool actif, endpoint 200 | `200` JSON SystemOne |
| pool actif, endpoint 429 | `429` + `Retry-After` réel si présent |
| pool actif, endpoint 5xx | `5xx` réel, pas un 200 |
| pool vide | `503` explicite fail-closed |

Aucun `200` avec une trame d'erreur.

## C4 — Quota journalier

Un `FreeUsageLimitError` :

- bloque `(modèle, IP)` jusqu'à `Retry-After` ou reset explicite ;
- ne déclenche pas une rotation sur un 429 transitoire ;
- autorise au plus une rotation contrôlée par fenêtre quand toute la flotte est
  épuisée.

## C5 — Rotation

- Budget : 3 rotations/heure/endpoint.
- Intervalle : 600–900 s.
- Déclencheurs : quota journalier, échec d'egress persistant.
- Jamais : 429 transitoire, timeout isolé, IPv4 canari.
- `rotation_jobs` doit porter `finished_at`, `result`, `reason`.

## C6 — Observabilité

`requests` doit permettre de distinguer :

- une requête client (`client_request_id`) ;
- une tentative (`attempt_no`) ;
- un statut final (`final_status`).

Sans ces champs, le taux d'échec client n'est pas mesurable.
