# Contract — Provider Selection

## Portes dures (avant le score)

Un endpoint est exclu si :

- `health=down` ou `unknown` depuis trop longtemps ;
- `quota_state=exhausted` ou `parked` avant `reset_at` ;
- provider désactivé ;
- bloc déjà utilisé par un autre endpoint actif (dédup) ;
- `inflight >= max_concurrent[provider]` (sauf sticky déjà établi) ;
- famille incompatible avec la requête.

Un `403` n'est **pas** une porte dure ; il ne retire pas le provider.

## Score

```text
score = 0.40 * success_rate
      + 0.20 * quota_headroom
      + 0.15 * latency_score
      + 0.15 * recent_success
      + 0.10 * normalized_weight
```

- `success_rate` : 1 h glissante.
- `quota_headroom` : 1 - used/profile, borné 0–1.
- `latency_score` : 1 - p50/max_p50, borné 0–1.
- `recent_success` : 1 si dernier probe 200, 0 sinon.
- `normalized_weight` : poids / 100.

## Poids et caps

- Poids par défaut : WARP 60, NordVPN 30, proxys 10.
- Poids 0 = provider désactivé (pas d'exclusion silencieuse : visible GUI).
- `max_concurrent` : sémaphore par provider ; une requête au cap attend ou
  choisit un autre provider.
- `diversity=true` : préfère un provider différent du précédent, sans jamais
  exclure un provider sain.

## Sticky

- Clé : conversation/session.
- TTL : `sticky_ttl_s`.
- Un egress sticky est conservé tant que `health=up`, `quota_state=ok`, et
  qu'aucun `403`/`429` quota n'est survenu.
- Un échec libère le sticky et journalise la raison.

## Hedge

- Après `hedge_delay_ms`, une seconde tentative peut partir sur un autre
  provider.
- `hedge_max_attempts` borne les tentatives.
- Le premier `200` valide gagne ; les autres sont annulés.
- Un hedge ne doit pas dupliquer une requête non idempotente sans accord.

## Dédup

- Clé : `block` (`/64` ou `/32`) + provider.
- Deux endpoints avec le même `block` ne servent pas simultanément.
- Le doublon est exposé dans le dashboard.

## Tests

- score déterministe avec des entrées fixes ;
- provider au cap → autre provider ;
- sticky conservé puis libéré sur 429 ;
- hedge : 1er lent, 2e 200 → succès ;
- 403 → pas de rotation ;
- dédup → un seul endpoint actif.
