# Contract — Error Taxonomy

**But**: ne jamais confondre un défaut de forme (`403`) avec un quota (`429`),
et ne pas parker un rate-limit court comme un quota journalier.

## Classes

| Classe | Détection | Effet ledger | Rotation | Fallback |
|---|---|---|---|---|
| `OK` | 2xx + contenu valide, pas d'`event: error` | succès, clear | non | non |
| `GATE_403` | 403, `FreeTierError`, forme/geo/auth/version | aucune écriture | **non** | **non** |
| `QUOTA_429` | 429 `FreeUsageLimitError` | bloc épuisé, `reset_at` | park si reset proche, sinon rotation | oui après WARP |
| `RATE_LIMIT_429` | 429 `RateLimitError` | pas d'exhaustion | non (attendre fenêtre) | non |
| `TRANSIENT_5XX` | 5xx, timeout, reset connexion | pas d'exhaustion | retry borné | non |
| `ACCOUNT_OR_GLOBAL` | 429 sur bloc frais + plusieurs blocs/axes | observation | non (pas de gain) | oui si provider différent |
| `AUTH` | 401/403 auth | aucune écriture | non | non |

## Règles

1. Un `403` ne crée ni ne modifie un `EgressBlock`.
2. Un `429` quota sans succès antérieur n'est pas automatiquement une
   « mauvaise IP » : il est classé quota si le corps/`Retry-After` le confirme.
3. Un `429` court ne parke pas ; il respecte la fenêtre annoncée.
4. Un `429` sur un bloc frais après plusieurs rotations peut être
   `ACCOUNT_OR_GLOBAL` ; ne pas empiler les proxies avant d'avoir testé
   l'axe compte/clé.
5. La classe est journalisée dans `QuotaObservation.error_class`.

## Tests

- 403 → `GATE_403`, ledger inchangé, rotation non appelée.
- 429 FreeUsageLimitError → `QUOTA_429`, bloc épuisé, reset enregistré.
- 429 RateLimitError → `RATE_LIMIT_429`, pas d'exhaustion.
- 500 → `TRANSIENT_5XX`, retry borné, pas de fallback.
- 429 sur 3 blocs frais → `ACCOUNT_OR_GLOBAL` (si la preuve est suffisante).
