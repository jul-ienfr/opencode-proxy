# Captures free OpenCode 2.2.5

`2.2.5.json` est un **template** : le corps et l'ordre exact des en-têtes du
client officiel 2.2.5, avec un `ses_` minté par `descending()`. Le `ses_`
stocké dans le fichier n'est qu'un exemple ; `scripts/test_free_egress.py`
en mint un **neuf par requête** et réécrit les quatre en-têtes de session
(`x-opencode-session`, `x-opencode-session-id`, `x-session-affinity`,
`x-session-id`) avec le même ID.

## Régénérer

```powershell
python scripts/mint_capture.py --verify
```

- mint un `ses_` neuf (`opencode._mint_oc_id("ses", "descending")`) ;
- régénère `b3` / `traceparent` ;
- met à jour `Content-Length`, `body_bytes`, `_body_sha256`, `minted_at` ;
- archive l'ancienne capture dans `archive/` ;
- `--verify` envoie la capture et attend un 200.

Pour amorcer depuis un corps capturé :

```powershell
python scripts/mint_capture.py --from-raw chemin\vers\3_req.bin --verify
```

## Tester

```powershell
python scripts/test_free_egress.py --repeat 5 --proxy direct
python scripts/test_free_egress.py --station 2 --repeat 3
python scripts/test_free_egress.py --mint --repeat 3
```

`--repeat N` produit N requêtes avec N `ses_` différents. `--reuse-session`
réutilise explicitement le `ses_` de la capture (debug). Un `ses_` aléatoire
(`secrets.token_hex(6)`) est un **faux quota** : le gate répond
`FreeUsageLimitError` avec `Retry-After` jusqu'à 00:00 UTC. Vérifier le
préfixe avec `scripts/test_session_id.py`.
