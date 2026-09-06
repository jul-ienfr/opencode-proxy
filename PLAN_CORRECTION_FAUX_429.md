# Plan de correction — Faux 429 « Free quota exhausted on all VPN stations »

Date : 2026-09-05 | Etat : implémenté (A✅ B✅ C✅ D✅ E✅ — 2026-09-05)

---

## Verdict : l'utilisateur a raison

Ce n'est **pas** un problème de quota. La preuve est dans le code et les logs.

### Preuve n°1 — les logs montrent un 503, jamais un 429

`logs/debug.log`, fenêtre 12:06 → 12:18 (12 requêtes refusées d'affilée) :

```
[free] req_id=msg_17a272ef78fc0-d0 leg=free→refuse 'muse-spark-1.3-contributor-free' free_status=503 (strict_free, no paid leg)
```

L'upstream free a répondu **503**, et le client a reçu **`429 Free quota exhausted on all VPN stations. Retry after 120s.`** — un statut et une cause **fabriqués par le proxy**.

À 15:15-15:21, le même modèle free passe en boucle avec succès (`[free] 'muse-spark-1.3-contributor-free' succeeded (72 000+ tokens) via /responses`). Conclusion : le quota n'a jamais été épuisé ; c'est cohérent avec l'argument « si c'était du quota, streaming et non-streaming échoueraient pareil ».

### Preuve n°2 — le code ré-étiquette TOUT échec free en « 429 quota »

Dans `_try_free_model_first` (opencode.py), mode `strict_free` (config actuelle : `strict_free: true`) :

| # | Ligne | Comportement actuel | Pourquoi c'est faux |
|---|-------|--------------------|---------------------|
| C1 | `opencode.py:6440-6447` | Tout statut non-200 non-429 (503, 400, 502…) → `raise FreeQuotaExhausted(...)` | Le client reçoit 429 « quota » même si l'erreur réelle est 503 |
| C2 | `opencode.py:6197-6208` | Tentatives épuisées (`resp is None` : échecs tunnel OU retries consommés) → `raise FreeQuotaExhausted("120")` | Fabrique 429 + `Retry-After: 120` (défaut `_FREE_429_DEFAULT = 120`, ligne 5531) sans jamais avoir vu un 429 |
| C3 | `opencode.py:5841-5857` (`_free_quota_exhausted_response`) | Réponse HTTP 429 fixe, message unique, `Retry-After` = valeur passée ou « 60 » par défaut | Ni le statut ni le délai ne reflètent l'upstream |

### Preuve n°3 — le streaming client passe par CE chemin menteur

`/v1/messages` avec `stream: true` (Claude Code) n'utilise **pas** la boucle SSE véridique : à `opencode.py:13086`, le handler fait `anthro_body["stream"] = False` (« collect, then emit SSE ») et appelle `_try_free_model_first` (ligne 13090) — le chemin **non-streaming** qui répond le faux 429 HTTP aux lignes C1/C2.

Le chemin streaming SSE véridique existe déjà (`_free_stream_refuse_bytes`, `opencode.py:7114-7155` : 429 → message quota, sinon `api_error` avec le vrai statut et le vrai body) mais il ne sert que la boucle streaming OpenAI (9920+) ; le streaming Anthropic (le client concerné) n'y passe jamais.

### Pourquoi 503 à ce moment-là

Fenêtre 12:06-12:18 : `public IP probe failed on all 4 endpoints via SOCKS5`, `AUTH_FAILED`, `rotation attempt failed` (cf. PLAN_STABILISATION_VPN). Tunnel dégradé → l'endpoint free (géo-restreint, opencode.ai) répond 503. La cause amont est couverte par PLAN_STABILISATION_VPN.md ; **ce plan-ci corrige le mensonge 429 côté proxy**, indépendamment.

### Cause aggravante — pas de retry inter-stations sur 503

Dans la boucle de `_try_free_model_first` (6096-6146), seuls **429, 400 et 403-DataPolicy** déclenchent un retry sur station fraîche. Un 503 tombe directement dans le post-boucle → refus immédiat, même si 5 autres stations auraient pu réussir. À l'inverse, la boucle streaming OAI retente déjà les non-429 sur station fraîche (`opencode.py:9920-9944`) — asymétrie à corriger.

---

## Plan de correction (code uniquement, config inchangée, `strict_free` préservé)

### Lot A — Refus véridique (statut réel, cause réelle) — ✅ FAIT (2026-09-05)

**A1. Nouvelle exception + réponse véridique.**
- Enrichir `FreeQuotaExhausted` : lui ajouter `status: int=429` et `body: str=""` — ou créer `FreeRefusal(status, body, retry_after)`. Le nom « Quota » ne doit plus porter les erreurs non-quota.
- Nouveau `_free_refusal_response(status, body, retry_after, protocol)` qui remplace `_free_quota_exhausted_response` (5841) :
  - `status == 429` → réponse 429, message « free quota exhausted », `Retry-After` = header upstream **réel** (sinon omettre le header — ne jamais inventer 60/120) ;
  - sinon → réponse avec le **statut upstream** (503 → 503), type `api_error`, message `Free model request failed with status {status}: {body tronqué}` (miroir exact de `_free_stream_refuse_bytes`, 7131-7138), `Retry-After` propagé si présent.
- Fichier : `opencode.py` (5830-5857). Les 6 appels `except FreeQuotaExhausted` existants (8566, 8646, 9505, 11046, 12044, 12910, 12968, 13115, 13266, 13325, 13560) deviennent `except FreeRefusal` — la forme de la réponse change, pas la structure du code.

**A2. C1 (ligne 6440-6447)** : lever la nouvelle exception avec `status=resp.status_code`, `body=resp.text[:300]`, `retry_after=resp.headers.get("retry-after","")`. Fini le « or 60 » fabriqué.

**A3. C2 (ligne 6197-6208)** : mémoriser la cause terminale dans la boucle (`_last_err_status` / `_last_err_exc`) :
- dernière cause = erreur de tunnel/connexion (`_FreeTunnelFailure`, connect error) → refus **503** « no usable VPN station/tunnel » ;
- dernière cause = vrais 429 → 429 quota avec le Retry-After du dernier 429 ;
- ne jamais émettre « quota exhausted » sans avoir vu un 429 réel.

### Lot B — Parité de retry inter-stations — ✅ FAIT (2026-09-05)

**B1.** Dans la boucle de `_try_free_model_first` (~6117), ajouter **5xx (502/503/504) et timeouts** aux statuts qui déclenchent `continue` sur station fraîche (avec `_log_free_model_usage` + log), miroir du comportement streaming `opencode.py:9920-9944`. Un 503 transitoire (tunnel qui flap) ne doit pas tuer la requête au premier essai alors que `free_max` stations restent disponibles.

**B2.** (reprend PLAN_STABILISATION_VPN §5.2, étendu au non-stream) : délai court (≈2 s, plafonné) entre tentatives free consécutives dans les deux chemins.

### Lot C — Cohérence du chemin collect-stream — ✅ FAIT (option HTTP JSON véridique documentée en code, 2026-09-05)

**C1.** `/v1/messages` streaming (13085-13097) : avec A appliqué, le refus devient véridique. Décision à documenter : garder la réponse HTTP non-SSE (simple, le client la gère) ou répondre en SSE `api_error` véridique. Recommandation : SSE `api_error` (cohérent avec ce que le client attend quand `stream: true`), en réutilisant `_free_stream_refuse_bytes` — factoriser alors la logique véridique en UN helper partagé par les 3 chemins (non-stream, collect-stream, SSE natif).

### Lot D — Instrumentation — ✅ FAIT (D1 body loggé + D2 ctx push, 2026-09-05)

**D1.** La ligne debug du refus strict (`6443-6444`) doit inclure le body upstream tronqué (`_redact(resp.text, 300)`) — aujourd'hui elle ne loggue que le statut, d'où l'impossibilité de diagnostiquer le 503 depuis les logs seuls.

**D2.** Vérifier que `free_status` réel est persisté en DB sur ce chemin (ctx C1) — fait côté stream via `_free_fallback_bookkeep`, à aligner côté non-stream si absent.

### Lot E — Tests (le projet a déjà `test_free_quota.py` + `tests/`) — ✅ FAIT (2026-09-05 : `tests/test_faux_429_veridique.py` E1-E6 + legacy, 8 tests verts ; `test_no_valid_keys_guard.py` aligné sur refus véridique)

| Test | Entrée | Attendu après correction |
|------|--------|--------------------------|
| E1 | free 503, strict_free | réponse **503** `api_error` + vrai message (jamais 429) |
| E2 | vrais 429 sur toutes les stations | 429 quota + Retry-After réel du dernier 429 |
| E3 | échecs tunnel sur toutes les stations | **503** « no usable VPN station/tunnel » |
| E4 | `/v1/messages` stream=true + free 503 | refus 503/AE api_error (SSE) — pas de « quota » |
| E5 | free 503 sur station 1, 200 sur station 2 | succès 200 (B1), usage loggé pour les 2 essais |
| E6 | 503 upstream SANS header Retry-After | réponse SANS header Retry-After fabriqué |

Vérification live : rejouer sur le port 4000 une requête streaming Claude Code pendant une fenêtre de 503 upstream → le client doit afficher l'erreur réelle (503/raison), plus jamais « Free quota exhausted ».

---

## Ordre d'implémentation

| Priorité | Lot | Impact | Effort |
|----------|-----|--------|--------|
| P0 | A (refus véridique) | Le client voit la vraie erreur — fini les fausses pistes « quota » | ~2h |
| P1 | B1 (retry 5xx inter-stations) | Résorption directe des refus : un 503 transitoire ne tue plus la requête | ~1h |
| P1 | D1 (log du body refusé) | Diagnostic futur immédiat | ~15min |
| P2 | C1 (collect-stream SSE véridique) | Cohérence d'interface client | ~1h |
| P2 | E (tests) | Non-régression | ~2h |
| P3 | B2 (délai inter-tentatives) | Confort | ~30min |

## Critère de succès — ✅ VÉRIFIÉ EN TESTS (2026-09-05)

- Zéro message « Free quota exhausted » sans 429 upstream observé (vérifiable par grep sur logs : tout refus 429 émis doit être précédé d'un `RATE LIMITED (429)` ou d'un log quota réel). — ✅ couvert E1/E3/E6 (503 → jamais « quota »).
- Un 503 upstream transitoire est absorbé par retry inter-stations (taux de succès free ↑) ; s'il persiste, le client reçoit 503 + cause réelle. — ✅ couvert E5 (503→200) + E1 (503 persistant → 503).
- `PLAN_STABILISATION_VPN.md` reste le chantier amont (killers d'AUTH_FAILED) — ce plan ne le remplace pas, il supprime le bruit qui masquait la vraie cause.

## Notes d'implémentation (2026-09-05)

- `FreeRefusal(status, body, retry_after)` créée comme base ; `FreeQuotaExhausted` = sous-classe legacy (compat) ; les 14 `except` passés à `except FreeRefusal` ; `_free_refusal_response` = réponse véridique (429 → 429 + RA réel ou omis ; sinon statut relayé + `api_error`), `_free_quota_exhausted_response` = wrapper legacy.
- `_free_stream_refuse_bytes` : fini le `or "60"` fabriqué (message sans délai si RA absent).
- Boucle `_try_free_model_first` : cause terminale mémorisée (`_last_free_status/_last_retry_after/_last_body/_last_tunnel_exc`) ; C2 → 503 tunnel / 429 réel / statut réel ; C1 → `FreeRefusal` véridique + log body + ctx push ; B1 retry 5xx (500-599) sur station fraîche ; B2 délai `0.5×attempt` plafonné 2 s (`_free_retry_delay_seconds`, 0 sous pytest) en non-stream + centralisé en streaming via `_open_free_stream(fresh_station)`.
- C1 : option HTTP JSON véridique documentée en code (le client stream gère l'erreur HTTP).
- Fichiers touchés : `opencode.py`, `tests/test_faux_429_veridique.py` (nouveau, 8 tests), `tests/test_no_valid_keys_guard.py` (2 tests alignés).
