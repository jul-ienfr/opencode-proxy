# Contrat — Cooldown 429 free

**Fonctions**: `_free_429_cooldown_seconds`, `_free_cooldown_cap_seconds`,
`_set_free_cooldown`, `QuotaLedger.note_result`.  
**Config**: `free.cooldown_cap_s`, `ip_quota.ledger_max_block_s`,
`ip_rotation.trial_dead_cooldown_s`.

## Sémantique du `Retry-After`

Le header upstream est la **distance au prochain 00:00 UTC**, pas la vraie
recovery. Mesure afree-proxy : `RA=20 h 46`, puis `200` sur la même IP
18 minutes plus tard. Il ne doit donc pas être honoré comme une vérité de
blocage.

## Formule station

```
cap = free.cooldown_cap_s        # défaut 1200 s, 0 = pas de plafond
brut = header Retry-After        # secondes ou HTTP-date
       sinon durée humaine du corps ("retry in 3hr 12min")
       sinon 120 s
base = min(brut, cap)            # si cap > 0
si scope == "trial-exhausted" et header absent :
    base = max(base, trial_dead_cooldown_s)   # défaut 21600 s
cd_station = min(base, cap)      # si cap > 0
```

Points d'attention :

- le `max` avec `trial_dead_cooldown_s` est appliqué **avant** le plafond
  final ; sinon une IP sans header resterait parkée 6 h ;
- `model_ip_bad` peut réduire à 120 s une IP à 0 succès ;
- un scope `transient` est plafonné à 30 s.

## Formule ledger

Clé : `(IP normalisée, jour UTC, bucket)`.

```
si 200 :
    exhausted = False ; retry_after = 0 ; reset_at = 0
si 429 FreeUsageLimitError :
    retry_after = RA brut
    si RA > 0 :
        reset_at = ts + min(RA, ledger_max_block_s)   # si > 0
    sinon :
        reset_at = prochain minuit UTC
si 429 transitoire :
    transient_429 += 1 ; pas d'exhaustion
```

Le défaut `ledger_max_block_s = 0` préserve le contrat historique (vérité
serveur) ; la production active 1 200 s.

## Priorité

1. Un `200` observé lève toujours l'exhaustion.
2. Un `429 FreeUsageLimitError` avec header borne le ledger et la station.
3. Un `429` sans header cale le ledger sur minuit UTC et la station sur
   `trial_dead_cooldown_s`, tous deux plafonnés.
4. Un `429` transitoire ne marque jamais le bucket.

## Config actuelle

```yaml
free:
  cooldown_cap_s: 1200
ip_quota:
  ledger_max_block_s: 1200
ip_rotation:
  trial_dead_cooldown_s: 21600   # valeur existante, plafonnée à 1200
```

## Tests

- `test_cooldown_header_wins_over_body`
- `test_cooldown_cap_applies_to_huge_retry_after`
- `test_cooldown_cap_can_be_disabled`
- `test_retry_after_cap_reprobes_before_midnight`
- `test_free_usage_429_marks_bucket_until_midnight`
