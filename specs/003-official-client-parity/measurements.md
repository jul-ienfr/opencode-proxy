# Mesures A/B — parité wire free

**Statut** : incident ouvert (2026-11-05) ; Phase 0 en attente.  
**Règle** : une mesure par IP et par fenêtre ; ne jamais conclure d'une IP au pool.  
**Preuve** : conserver la requête brute et la réponse (statut + début de corps) dans `.artifacts/audit-<date>/ab/`.

## Incident 2026-11-05 — faits observés

| Indicateur (24 h) | Valeur |
|---|---:|
| Requêtes totales | 2 377 |
| `free_status=502` | 2 141 |
| `free_status=429` | 227 |
| Modèle | `jev-1.13-free` uniquement |
| Client | smoke test `python-requests/2.33.1` |
| Dernier client réel | 2026-11-04 20:28 UTC, `deepseek-v4.1-flash-free`, 200 |
| `Retry-After` des 429 récents | 42–58 s |

**Lecture** : les 429 récents ne sont pas le bucket journalier (RA long → minuit UTC). Ce sont des rate-limits courts par modèle/IP. Le 502 est un problème de tunnel. Le client réel n'apparaît plus dans la DB depuis la veille.

### Campagne live 2026-11-05 22:47–22:52 UTC

| Profil | Egress | Statut | `Retry-After` | Corps | Interprétation |
|---|---|---|---:|---|---|
| 2.2.5 CLI exact | direct résidentiel | 429 | 6 956 s | `FreeUsageLimitError` | bucket IP |
| 2.2.5 CLI exact | direct résidentiel | 429 | 6 948 s | `FreeUsageLimitError` | bucket IP |
| 2.2.5 CLI exact | direct résidentiel | 429 | 6 948 s | `FreeUsageLimitError` | bucket IP |
| 2.2.5 desktop exact | direct résidentiel | 429 | 6 784 s | `FreeUsageLimitError` | bucket IP |

Aucun 403/`FreeTierError` : les deux profils atteignent la couche quota. L'infrastructure est `0/6` stations (`AUTH_FAILED`, SOCKS rejected), donc le test CLI vs desktop sur IP fraîche est **bloqué jusqu'au Plan N**.

### Campagne 2026-11-06 — credentials valides, limite de sessions

| Test | Egress | Statut | `Retry-After` | Interprétation |
|---|---|---|---:|---|
| CLI exact via conteneur VPN `al41` | 87.120.102.86 | 429 | 71 431 s | IP épuisée, wire accepté |
| desktop exact via conteneur VPN `al41` | 87.120.102.86 | 429 | 71 431 s | idem |
| 6 stations + conteneur test | — | `AUTH_FAILED` | — | limite de sessions atteinte |
| 6 stations arrêtées + 1 conteneur | `al41` | auth OK | — | credentials valides |

Conclusion : le token/les credentials sont valides ; le blocage restant est la **limite de sessions simultanées** et le **quota par IP**. Le wire exact CLI **et** desktop passe la couche signature (aucun 403/`FreeTierError`).

### Classification à remplir pour le 429 du client

| Date | Modèle | IP egress | Corps | `Retry-After` | Classe | Action |
|---|---|---|---|---:|---|---|
| | | | | | long / court / `FreeTierError` / 502 | |

## Variantes

| ID | Profil | En-têtes | Corps | TLS | But |
|---|---|---|---|---|---|
| A | proxy actuel | 2.2.5 hybride | 2.2.5 injecté | version du vault | baseline |
| B | 2.2.5 `cli` exact | ordre 2.2.5 | 13 outils, `max_completion_tokens`, sans `tool_choice` | Bun 1.4.4 | cible |
| C | 2.2.5 `desktop` exact | ordre 2.2.5 | idem B | Bun 1.4.4 | distribution app |
| D | 1.19.5 exact | ordre 1.19.5 | 11 outils, `max_tokens`, `tool_choice: auto` | Bun 1.4.4 | rollback |
| E1 | 2.2.5 + placeholders | ordre 2.2.5 | 13 placeholders `properties: {}` | Bun 1.4.4 | tester D3 |
| E2 | 2.2.5 + définitions complètes | ordre 2.2.5 | 13 définitions réelles | Bun 1.4.4 | tester D3 |
| F1 | 2.2.5 + date figée + chemin réel | ordre 2.2.5 | payload figé | Bun 1.4.4 | tester D6 |
| F2 | 2.2.5 + date courante + chemin synthétique | ordre 2.2.5 | template | Bun 1.4.4 | tester D6 |

## Matrice

| Date | IP | ASN | Variante | Statut | `Retry-After` | TTFT (s) | Contenu | Notes |
|---|---|---|---|---|---:|---:|---|---|
| | | | A | | | | | |
| | | | B | | | | | |
| | | | C | | | | | |
| | | | D | | | | | |
| | | | E1 | | | | | |
| | | | E2 | | | | | |
| | | | F1 | | | | | |
| | | | F2 | | | | | |

## Règles de lecture

- Un 200 sans contenu ne vaut pas succès.
- Un 429 avec `Retry-After` long est un quota, pas une preuve de forme.
- Un 403 est un rejet de forme : noter le corps exact.
- Toujours intercaler un témoin A entre deux variantes pour distinguer une dérive temporelle d'un effet de forme.
- Minimum 20 échantillons par variante ; publier p50/p95 et le taux de 200.

## Décision attendue

| Question | Réponse | Preuve |
|---|---|---|
| L'ordre des en-têtes est-il discriminant ? | | |
| Le TLS 1.3.14 + UA 2.2.5 est-il refusé ? | | |
| Les placeholders passent-ils ? | | |
| Le prompt templaté passe-t-il ? | | |
| Le project `global` est-il lu ? | | |
| Le trace ID stable est-il exigé ? | | |
