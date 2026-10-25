# Cause racine du blocage du chat gratuit — 2026-10-23T23:17:27Z

> **Verdict.** Le provider n'a pas fermé le gratuit, n'a pas banni nos IP et n'a
> pas changé le quota. Il a **durci la vérification du corps de requête** : il
> exige désormais la signature complète du client officiel (les 10 outils + le
> prompt système). Le proxy envoyait un corps de 608 octets avec un seul outil,
> donc tout était refusé, quelle que soit l'IP.
>
> Correctif : `free/official_payload.json` + `_free_wire_body()`.
> Preuve bout-en-bout : 4 modèles `200` le 2026-10-25.

---

## 1. Symptômes

| Moment (UTC) | Événement |
|---|---|
| 2026-10-23T23:17:05 | dernier `200` du chat gratuit |
| 2026-10-23T23:17:27 | premier `429`, puis **0 succès en continu** |
| 2026-10-24T00:00 | frontière annoncée par `retry-after` : rien ne rouvre |
| 2026-10-24 → 2026-10-25 | **159 948 tentatives, 0 succès**, ~100 IP, 23 modèles |
| 2026-10-25T00:12 | toujours `429` sur une IP jamais utilisée ce jour-là |

Deux faits qui ne collaient pas avec l'hypothèse « quota » :

1. `jev-1.13-free` continuait à **100 %** depuis les mêmes IP, aux mêmes heures
   (207/207 après minuit) → le compte et l'IP n'étaient pas en cause.
2. Le passage à zéro était **instantané et global** (23:17:05 → 23:17:27), pas
   progressif comme un quota qui s'épuise.

## 2. Ce qui a été éliminé (matrice)

| Hypothèse | Test | Verdict |
|---|---|---|
| Quota par IP épuisé | ~100 IP distinctes, dont des IP jamais vues | éliminé |
| Quota par modèle | 23 modèles free, dont 4 jamais testés (`beam-free`, `flint-40b-free`, `nemotron-*`) | éliminé |
| Blocage global / paywall | client officiel testé en live sur la même machine | éliminé |
| Identité (`x-opencode-*`, projet, session) | quartets/trions par IP, rotations | éliminé |
| TLS / JA3 | `impersonate=chrome131`, Bun JA3 | éliminé |
| Heure / reset | minuit passé, veille continue 17 h | éliminé |
| Course au quota (un autre consommateur) | 160 k tentatives, 0 `200` | éliminé |
| **Forme du corps** | **bisection ci-dessous** | **CAUSE** |

Le doublon `jev` a été l'indice décisif : même IP, même heure, même
`Authorization: Bearer public`, et pourtant `200`. La différence ne pouvait être
que dans la requête elle-même.

## 3. La preuve par le client officiel

Le 2026-10-25, la documentation du provider dit explicitement :

> *« Free-tier chat models require OpenCode; use paid models or Go from other clients. »*

Test immédiat du binaire officiel installé (`opencode` 1.18.31), même machine,
même réseau :

```
> build · deepseek-v4.1-flash-free
OK
```

Le gratuit fonctionne donc toujours. Le blocage est une **détection de client**.

## 4. Méthode : intercepter la requête réelle

Plutôt que de deviner, on a fait envoyer au client officiel sa vraie requête vers
un serveur local, en clair :

1. configuration temporaire (`OPENCODE_CONFIG`) déclarant un provider dont
   l'identifiant commence par `opencode` (sinon le client n'attache pas les
   en-têtes `x-opencode-*`) et dont le `baseURL` pointe sur `127.0.0.1` ;
2. `opencode run --model <provider>/deepseek-v4.1-flash-free "…"` ;
3. le serveur local journalise méthode, chemin, version HTTP, **en-têtes dans
   l'ordre** et corps intégral ;
4. il répond un SSE minimal pour que le client termine proprement.

Script reproductible : `scripts/capture_official_payload.py`.

Ce qui en sort (2026-10-25) :

```
POST /inference/v1/chat/completions HTTP/1.1
Authorization: Bearer public
Content-Type: application/json
User-Agent: opencode/1.18.31 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14
x-opencode-client: desktop
x-opencode-project: global
x-opencode-request: msg_…
x-opencode-session: ses_…
Accept: */*
Host: …
Accept-Encoding: gzip, deflate, br, zstd
Content-Length: 44107
```

Corps : `model, max_tokens, messages, tools, tool_choice, stream, stream_options`
— prompt système de **21 603 caractères**, **10 outils** (`bash, edit, glob,
grep, read, skill, task, todowrite, webfetch, write`).

Deux constats immédiats :

- le client officiel **n'envoie pas** `x-opencode-directory` ni
  `x-opencode-workspace` vers le provider (notre proxy les ajoutait) ;
- `x-opencode-project` vaut `global`, pas un hash de projet.

Ces écarts ne sont pas la cause (voir bisection C), mais ils sont désormais
documentés.

## 5. Bisection : ce qui décide

Même station, même IP, même seconde, même TLS. Seuls le corps et les en-têtes
changent.

| Cas | En-têtes | Corps | Résultat |
|---|---|---|---|
| A | officiels | officiel 44 107 o | **200** |
| B | officiels | minimal 108 o | **403** |
| C | proxy (avec `directory`/`workspace`) | officiel 44 107 o | **200** |
| D | proxy | minimal 108 o | **403** |

→ **Les en-têtes ne décident pas. Le corps décide.**

Puis, sur le corps :

| Variante | Taille | Résultat |
|---|---|---|
| copie intégrale | 44 740 o | **200** |
| sans `tools` | 22 926 o | **403** `FreeTierError` |
| 1 seul outil (`read`) | 28 908 o | **403** `FreeTierError` |
| outils complets, **sans prompt système** | 22 027 o | **429** `FreeUsageLimitError` |
| ni tools ni système | 213 o | **403** `FreeTierError` |

Message du 403 :

```json
{"type":"error","error":{"type":"FreeTierError","message":"OpenCode's free tier can only be used from within OpenCode. Download it at https://opencode.ai and keep it updated."}}
```

### La règle exacte

```
passage = signature_complète(tools) ∧ prompt_système_présent
```

- les **10** outils officiels sont nécessaires (un seul → 403) ;
- le prompt système est nécessaire (absent → 429) ;
- l'ordre des clés et `tool_choice:"auto"` sont la forme du client, pas des
  critères observables séparément.

C'est exactement la régression du 23/10 : l'ancienne mesure du 21/10 disait
« `read` seul → 200 ». Le proxy n'injectait donc que `read`. Le jour où le gate a
exigé la suite complète, chaque requête est devenue un 403/429 — sans qu'aucune
IP, aucun modèle ni aucun quota ne soit en cause.

## 6. Correctif

### Fichiers

| Fichier | Rôle |
|---|---|
| `free/official_payload.json` | capture intégrale du client 1.18.31 : corps complet + en-têtes d'origine + provenance |
| `opencode.py` (`_official_free_payload`, `_has_official_system`, `_free_wire_body`) | envoie la copie dès qu'un nom officiel manque |
| `scripts/capture_official_payload.py` | régénère la capture (maintenance) |
| `tests/test_free_official_payload.py` | verrouille le contrat |

### Comportement

- **corps client incomplet** → les 10 outils officiels et le prompt système sont
  injectés, les messages du client sont conservés à la suite ;
- **corps déjà conforme** (vrai client OpenCode) → il passe **tel quel**, octet
  pour octet ;
- `tool_choice:"auto"` ajouté s'il manque ; `stream_options` ajouté quand le
  stream est forcé ;
- l'ordre des clés reste `model, max_tokens, messages, tools, tool_choice,
  stream, stream_options` ;
- l'entrée n'est **jamais** mutée (les retries/hedges réutilisent le même dict).

### Ce qui a été retiré

Le shim `read` seul n'est plus utilisé pour la forme `chat` : il ne passait plus
le gate. Il reste en place pour la forme `responses` (`input`), qui n'est pas
couverte par la capture.

## 7. Preuve bout-en-bout (2026-10-25)

Client générique, corps minimal `{"messages":[{"role":"user","content":"Reponds exactement: OK"}], "max_tokens":16}` :

```
deepseek-v4.1-flash-free  -> 200  "OK"   (2,7 s)
mimo-v2.6-flash-free      -> 200  "OK"   (1,8 s)
glm-5.3-flash-free        -> 200  "OK"   (1,2 s)
big-pickle                -> 200  "OK"   (0,8 s)

stabilité : 3/3 non-stream 200
stream    : 200, chunks reçus
/v1/responses : 200 (input_tokens 11 045)
```

## 8. Coût et limites

| Point | Valeur | Commentaire |
|---|---|---|
| Tokens d'entrée | ~11 000 / requête | prompt système + 10 tools |
| Cache | 11 008 tokens cachés | le prompt est stable → coût réel faible |
| Outils du client | remplacés par les 10 officiels | un client tiers perd son *function calling* propre ; c'était déjà le cas avec le shim `read` |
| Comportement | le modèle se comporte en agent de code | conséquence directe du prompt système officiel |
| Forme `responses` | non couverte par la capture | elle passe aujourd'hui via conversion → chat |

Si un jour un client tiers a besoin de ses propres outils, il faudra **mesurer**
si le gate accepte les 10 officiels **plus** des outils supplémentaires. Ce n'est
pas testé, et ce n'est pas nécessaire au fonctionnement actuel.

## 9. Vérifier et maintenir

### Vérification rapide

```bash
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash-free","messages":[{"role":"user","content":"Dis OK"}],"max_tokens":8}'
```

Attendu : `200` avec `"content":"OK"`.

### Régénérer la capture (si le client officiel évolue)

```bash
python scripts/capture_official_payload.py
```

Le script intercepte le vrai client en local, choisit la requête la plus
complète (le client émet aussi des POST de titre/compaction) et réécrit
`free/official_payload.json`. Il n'envoie **aucun** trafic vers le provider.

> Le prompt système contient la date et l'environnement : une nouvelle capture
> peut différer de quelques caractères. C'est normal, le gate ne compare pas le
> texte exact — il vérifie la présence de la signature.

### Symptômes de rechute

| Symptôme | Interprétation | Action |
|---|---|---|
| `403 FreeTierError` **avec** le corps complet | le gate a encore changé | re-capturer, comparer les 10 outils |
| `429` avec le corps complet | quota réel de l'IP | rotation normale |
| `410 ModelDeprecated` | modèle retiré | mettre à jour `free_model_candidates` |
| `200` mais réponse vide/excentrée | prompt système injecté | vérifier que les messages client suivent bien le prompt |

## 10. Chronologie de la recherche (pour mémoire)

1. **24/10** — capture automatique au reset : 43 tentatives, 0 succès.
2. **24/10** — vérification que `retry-after` = secondes jusqu'à minuit
   (59/59, écart 0) : c'est une fin de fenêtre, pas une heure de récupération.
3. **24/10** — la bascule réelle est retrouvée à 23:17:27, 43 min avant minuit.
4. **25/10** — veille prioritaire : 159 948 tentatives, 0 succès, y compris
   9 520 dans l'heure suivant minuit.
5. **25/10** — client officiel testé : il fonctionne → détection de client.
6. **25/10** — interception locale → payload réel.
7. **25/10** — bisection A/B/C/D puis variantes de corps → la signature complète
   est la condition.
8. **25/10** — correctif, tests, preuve bout-en-bout.

## 11. Ce que ce document remplace

Les plans antérieurs (`PLAN-corrections-429.md`, `PLAN_CORRECTION_FAUX_429.md`,
`PLAN_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-09.md`) décrivaient le gate d'avant le
durcissement. Ils restent valables pour l'identité, le TLS, les stations et la
gestion des 429 de quota, mais **le critère d'acceptation du corps est celui
ci-dessus**.
