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

### Le client 2.1.4 (desktop) diffère complètement de 1.18.31

Capture du `opencode-cli.exe` v2.1.4 (même méthode d'interception) :

| | 1.18.31 | 2.1.4 |
|---|---|---|
| Outils | **10** : `bash, edit, glob, grep, read, skill, task, todowrite, webfetch, write` | **12** : `edit, glob, grep, question, read, shell, skill, subagent, webfetch, websearch, write, execute` |
| Prompt système | 21 603 car., « You are opencode, an interactive CLI tool… » | **9 691 car., « You are an AI agent running in OpenCode, a coding agent harness… »** |
| Corps | `max_tokens`, `tool_choice` | **`max_completion_tokens`**, pas de `tool_choice` |
| En-têtes | quartet `x-opencode-request/session/…` | `x-opencode-session-id` + `x-session-affinity` + `X-Session-Id` + `x-opencode-project` + `x-opencode-client`, **sans `x-opencode-request`** |

Conséquence : injecter le jeu v1 à un client v2 lui donnait des outils
(`bash`, `task`, `todowrite`) qu'il **ne sait pas exécuter**. Depuis le
correctif, un client officiel reconnu (v1 **ou** v2) traverse tel quel.

Le CLI v2 ne lit `OPENCODE_CONFIG` que lancé avec **`--standalone`** : sans ce
flag il se connecte au service en arrière-plan, qui a sa propre configuration
et ignore le fichier. C'est ce qui faisait échouer le premier test
(`Model unavailable`).

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

> **Révision du 2026-10-25 (15 h).** La règle mesurée le matin (« les 10 outils
> exacts sont nécessaires ») **ne tient plus** : re-mesurée avec des témoins
> intercalés sur une station fraîche, elle est plus simple. Les deux mesures
> sont consignées ici, la plus récente faisant foi.

```
passage = prompt_système_officiel_reconnu ∧ outil `read` présent
```

Mesures du 15 h (chaque cas encadré par un témoin qui repasse en 200, donc
pas de confusion avec le quota) :

| Corps | Résultat |
|---|---|
| 12 outils v2 + prompt v2 | **200** |
| `read` seul + prompt v1 | **200** |
| `read` seul + prompt v2 | **200** |
| `read` + `edit` + prompt v2 | **200** |
| 6 outils **sans** `read` + prompt v2 | **429** |
| aucun outil + prompt v2 | **429** |
| `read` seul + prompt système **court** (« Tu es un assistant concis. ») | **429** |
| `read` seul, **sans** système | **429** |

Deux conséquences :

1. **Le contenu du prompt système compte** : un prompt court ou absent est
   refusé même avec `read`. Il faut un prompt officiel reconnu.
2. **Les autres outils sont libres** : seul `read` est nécessaire. Le jeu v2
   (12 outils) passe tel quel.

Conséquence pour le proxy : on **n'injecte plus la copie v1** quand le client
est déjà un client officiel (v1 ou v2) — il traverse tel quel. L'injection
reste pour les clients génériques, qui n'ont ni prompt officiel ni `read`.

> **Pourquoi le changement.** Le matin, un corps v1 avec `read` seul
> répondait 403 ; l'après-midi, le même corps répond 200. Le provider ajuste
> son gate en continu : c'est précisément pour cela que le proxy vérifie une
> **signature minimale** et laisse passer les clients officiels, au lieu de
> figer un jeu d'outils qui peut changer sous nos pieds.

## 6. Correctif

### Fichiers

| Fichier | Rôle |
|---|---|
| `free/official_payload.json` | capture intégrale du client 1.18.31 : corps complet + en-têtes d'origine + provenance |
| `opencode.py` (`_official_free_payload`, `_has_official_system`, `_free_wire_body`) | laisse passer un client officiel, complète un client générique |
| `scripts/capture_official_payload.py` | régénère la capture (maintenance), y compris pour le client v2 (`--binary … --standalone`) |
| `tests/test_free_official_payload.py` | verrouille le contrat |

### Comportement

- **client officiel reconnu** (1.18.31 ou 2.1.4 : prompt officiel + `read`)
  → traverse **tel quel**, octet pour octet. C'est ce qui préserve les 12
  outils du client v2 (`shell`, `execute`, `subagent`…) au lieu de lui
  imposer ceux de 1.18.31 ;
- **client générique** (pas de prompt officiel) → le prompt officiel est
  ajouté, et **seul l'outil `read` officiel** est injecté. Les outils du
  client sont conservés tels quels : le modèle ne se voit plus offrir
  `bash`/`task`/`todowrite` qu'un client tiers ne saurait pas exécuter ;
- **client sans aucun outil** → `read` est ajouté pour le gate mais
  `tool_choice: "none"` interdit de l'appeler (un client de chat ne recevra
  jamais un appel d'outil qu'il ne connaît pas) ;
- `stream_options` ajouté quand le stream est forcé ;
- l'ordre des clés n'est réordonné que si l'on a modifié le corps ; un corps
  officiel garde son ordre (v2 utilise `max_completion_tokens`) ;
- l'entrée n'est **jamais** mutée (les retries/hedges réutilisent le même dict).

### Ce qui a été retiré

L'injection des **10 outils v1** est supprimée : elle datait de la mesure
« suite complète exigée », invalidée par la re-mesure du 15 h. Le shim `read`
suffit, et il reste aussi utilisé pour la forme `responses` (`input`).

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

Vérification finale dans `logs/requests.db` — 8 succès consécutifs, 3 modèles,
4 IP de sortie différentes :

```
2026-10-25T03:32:45Z  glm-5.3-flash-free       success=1  ip=187.13.240.147
2026-10-25T03:33:14Z  glm-5.3-flash-free       success=1  ip=84.233.199.109
2026-10-25T03:33:45Z  mimo-v2.6-flash-free     success=1  ip=187.15.103.33
2026-10-25T03:34:00Z  mimo-v2.6-flash-free     success=1  ip=186.247.41.77
2026-10-25T03:34:22Z  glm-5.3-flash-free       success=1  ip=187.13.240.147
2026-10-25T03:35:44Z  deepseek-v4.1-flash-free success=1  ip=84.233.199.109
2026-10-25T03:35:54Z  mimo-v2.6-flash-free     success=1  ip=187.15.103.33
2026-10-25T03:36:00Z  glm-5.3-flash-free       success=1  ip=186.247.41.77
```

Et le *function calling* d'un client tiers, de bout en bout avec l'outil
`get_weather` fourni par le client :

```
stream     : tool_calls -> {"city": "Paris"} (fragments SSE)
non-stream : tool_calls -> {"name":"get_weather","arguments":"{\"city\": \"Paris\"}"}
             finish_reason = tool_calls
```

Et le raisonnement, qui était également jeté en non-stream (mesuré sur
`mimo-v2.6-flash-free`, `delta.reasoning_content`) :

```
clés message : ['role', 'content', 'reasoning_content']
content      : "391"
reasoning    : "The user asks in French: 'How much is 17*23?' ..."
```

### Matrice du raisonnement (2026-10-25)

Vérifiée sur `mimo-v2.6-flash-free`, un modèle qui raisonne réellement :

| Protocole | Non-stream | Stream |
|---|---|---|
| `/v1/chat/completions` | `message.reasoning_content` | `delta.reasoning_content` |
| `/v1/messages` | bloc `thinking` (signature locale) | `thinking_delta` |
| `/v1/responses` | item `reasoning` | événements `reasoning` |

Un premier test avait conclu à tort que les façades Anthropic/Responses
perdaient le raisonnement : le modèle avait répondu sans raisonner sur cet
appel. La matrice ci-dessus a été refaite avec un prompt qui l'oblige à
détailler son calcul, trois fois de suite.

### Dialecte du raisonnement, mesuré modèle par modèle (2026-10-25)

Les amonts free n'écrivent pas tous le raisonnement dans le même champ :

| Modèle | Champ émis | `reasoning_options` (models.dev) |
|---|---|---|
| `deepseek-v4.1-flash-free` | `reasoning_content` | `toggle` + `effort` (low/high/max) |
| `glm-5.3-flash-free` | `reasoning_content` | `effort` (low/high/max) |
| `deepseek-v4-flash-free` | `reasoning_content` | `effort` |
| `mimo-v2.6-flash-free` | **`reasoning` + `reasoning_details[]`** | *(aucune : toujours actif)* |
| `nemotron-3.5-lightning-free` | **`reasoning` + `reasoning_details[]`** | *(aucune : toujours actif)* |
| `big-pickle` | `reasoning_content` | *(aucune)* |

Ce que fait le client officiel 1.18.31 (fonction extraite du binaire, verbatim) :

```
reasoning_options absent  -> aucun paramètre
reasoning_options = []    -> aucun paramètre (raisonnement toujours actif)
effort présent            -> reasoning_effort (low/high/max)
toggle seul               -> rien pour @ai-sdk/openai-compatible
```

Et le parseur 1.19.3 ne lit **que** `delta.reasoning_content` : un modèle qui
répond `reasoning` seul verrait son raisonnement invisible.

Le proxy accepte donc les deux dialectes : `_normalize_chat_reasoning_chunk`
ajoute `reasoning_content` depuis `reasoning`/`reasoning_details` **sans
retirer** les champs d'origine (les clients OpenRouter, eux, lisent
`reasoning`/`reasoning_details`). Le collecteur non-stream applique la même
règle.

### Appels d'outils au nom vide (deepseek-harness, 2026-10-25)

Symptôme côté client : `This turn failed format v4 tool/result at seq N
requires toolCallId matching its tool source`, puis session impossible à
reprendre.

Cause mesurée dans le journal de session du harness (`session.v4.jsonl.zstd`) :

```json
{"type":"tool/call","callId":"","name":"","arguments":"\"npm run db:migrate…\""}
```

Le modèle avait appelé un outil **inexistant**. Pourquoi : le prompt du
harness (`You are an AI agent powered by DeepSeek Harness…`) n'est pas un
prompt officiel, donc le proxy injectait celui d'OpenCode — qui décrit
`bash`, `task`, `todowrite`. Or le harness fournit `pwsh`, `read`, `edit`…
Le modèle a suivi le prompt injecté, appelé `bash` (absent de `tools`), et la
passerelle a rendu un `tool_call` **sans nom ni id**. Le harness ne peut alors
apparier aucun résultat, et refuse de rejouer le tour.

Correctif en deux points :

1. **garder le `read` du client** quand il en fournit un (le gate ne vérifie
   que le nom ; remplacer son schéma `file_path` par `filePath` casserait son
   exécuteur) ;
2. **consigne ajoutée à la fin du dernier message système du client** :
   *« Only the tools declared in this request's `tools` array are available.
   Ignore any tool name mentioned above that is not in that array, and never
   invent a tool name. »*

Vérifié : le gate accepte le prompt officiel + cette consigne (200), et le
même tour harness produit désormais
`id='call_00_e4fh…' name='pwsh' args='{"command": "npm run db:migrate"}'`.

> **Reprise d'une session déjà cassée** : le journal se termine par un
> `tool/call` vide sans résultat. Le plus sûr est de démarrer une nouvelle
> session ; sinon il faut supprimer les deux derniers événements
> (`assistant/message` + `tool/call`) du `session.v4.jsonl.zstd` avant de
> reprendre.

### Récidive : arguments valides, nom/id vides (dsh, 2026-10-26)

Même symptôme (`format v4 tool/result at seq 9473 requires toolCallId
matching its tool source`), mais **cause différente** : le modèle n'a pas
appelé un outil inexistant. L'amont free a émis un `tool_call` avec
`id=""` et `name=""` **alors que les arguments étaient ceux de `pwsh`** :

```json
{"type":"tool-call","id":"","name":"","arguments":"{}"}
```

Le flux journalisé montre pourtant `"command": "for ($i=0; … Invoke-WebRequest
…"`. Le harness reçoit donc un `tool/call` vide et ne peut apparier le
`tool/result` (qui n'a pas le même `callId`) ; la session est refusée au
rechargement.

La consigne « n'invente pas de nom d'outil » ne protège pas de ce cas : le
nom n'a jamais été émis par l'amont. Correctif côté proxy, avant tout yield :

- `_ChatToolCallHealer` retient les deltas d'un `tool_call` sans nom, puis
  infère le nom **d'après les clés de ses arguments** et les outils réellement
  déclarés par le client (`command` → `pwsh`, `file_path`+`content` → `write`,
  etc.) ;
- le flux dsh mesuré avait aussi perdu l'ouverture `{"` (`command": "for …`) :
  le proxy la restaure avant d'émettre l'appel, sinon le JSON resterait
  invalide même avec le bon nom ;
- tout `id` vide est synthétisé ; jamais `id=""` côté client ;
- si plusieurs schémas restent compatibles, aucun choix n'est deviné : l'appel
  est écarté et `finish_reason` devient `stop` (pas `tool_calls`) ;
- si rien d'exploitable n'a été émis, le flux est rejoué sur une station
  fraîche via la reprise « stream vide » existante ; le client ne voit jamais
  l'appel invalide ;
- le collecteur non-stream applique la même inférence/écart, et le chemin
  Anthropic écarte un `tool_use` sans nom au lieu d'émettre un bloc invalide.

Couvert par `tests/test_tool_call_identity_healing.py` et le test E2E
`test_p3_stream_repare_un_tool_call_amont_sans_nom_ni_id`.

### « the turn stopped while a tool result was still pending » (dsh, 2026-10-26)

Symptôme : le harness affiche `⚠️ No reply: the turn stopped while a tool
result was still pending…`. Deux tours avaient terminé avec
`{"kind":"error","message":"upstream stream truncated without terminal"}`.

Cause : l'amont ferme parfois le flux après un **simple chunk `role`**, sans
contenu ni `finish_reason`. Le retry de flux vide exigeait `not has_yielded` :
ce chunk d'enveloppe annulait donc le retry, et le proxy émettait sa propre
erreur `upstream stream truncated without terminal` — que le harness remontait
comme un tour échoué.

Correctifs :

- `_should_retry_empty_stream(..., content_yielded=...)` : une enveloppe seule
  n'est pas du contenu exploitable, le retry sur station fraîche reste sûr ;
- si des `tool_calls` **complets** ont été reçus mais que le terminal manque,
  le proxy synthétise `finish_reason: "tool_calls"` au lieu d'une erreur — le
  tour d'outil est valide et le client peut exécuter l'outil.

Les vraies troncatures de texte (contenu partiel, pas de terminal) restent des
erreurs : on ne fabrique pas de faux `stop`.

### Paramètres d'activation : mesurés, tous tolérés

Envoyés à l'amont sur `deepseek-v4.1`, `glm-5.3` et `mimo-v2.6` : rien,
`reasoning_effort: "high"`, `thinking: {"type":"enabled"}`,
`enable_thinking: true`. **200 partout**, raisonnement présent quand le
prompt l'exige.

Idem pour des valeurs **non déclarées** par le modèle : `glm` accepte
`medium` et `xhigh` (il ne déclare que low/high/max), `deepseek` accepte
`medium`, `mimo` accepte `high` et `minimal` (il ne déclare rien), `beam`
accepte `max`. **200 partout** : l'amont est permissif.

Conclusion : aucun paramètre n'est requis ni rejeté ; le raisonnement est
actif par défaut. Le proxy ne force donc rien et laisse passer ce que le
client envoie. Le seul point dur reste l'historique : `deepseek` exige
`reasoning_content` sur chaque message assistant (rempli à `" "` par
`ensure_deepseek_reasoning`), tandis que `glm` et `mimo` acceptent son
absence — mesuré sur un tour assistant rejoué, avec et sans le champ.

### Tous les modèles free non dépréciés (models.dev, 2026-10-25)

| Modèle | `reasoning_options` |
|---|---|
| `deepseek-v4.1-flash-free` | `toggle` + `effort` low/high/max |
| `glm-5.3-flash-free` | `effort` low/high/max |
| `beam-free` | `effort` low/medium/high/xhigh… |
| `flint-40b-free` | `effort` low/medium/high/xhigh… |
| `nemotron-3.5-super-vl-free` | `effort` none/minimal/low/medium/high… |
| `mimo-v2.6-flash-free` | *(aucune : toujours actif)* |
| `nemotron-3.5-lightning-free` | *(aucune)* |
| `nemotron-3-ultra-free` | *(aucune)* |
| `big-pickle` | *(aucune)* |

Le champ de sortie reste `reasoning_content` pour tous (sauf le dialecte
`reasoning`/`reasoning_details` constaté sur mimo et nemotron), d'où la
normalisation décrite plus haut.

### Différence entre le client installé (1.18.31) et 1.19.3

- **1.18.31** : le binaire contient la fonction qui traduit
  `reasoning_options` en options provider (extraite verbatim ci-dessus) ;
  `@ai-sdk/openai-compatible` envoie `reasoning_effort` quand le modèle
  déclare une option `effort`, et rien du tout sinon.
- **1.19.3** : réécriture avec ses propres protocoles ; le parseur
  `openai-chat.ts` ne lit **que** `delta.reasoning_content`.

D'où la règle du proxy : **émettre `reasoning_content` dans tous les cas**,
tout en conservant `reasoning`/`reasoning_details` pour les clients qui
lisent le dialecte OpenRouter.

À noter : `deepseek-v4.1` ne raisonne pas sur une question triviale
(« 17*23 ») — d'où les faux négatifs des premiers tests. Sur une demande qui
impose de détailler, il émet bien `reasoning_content`.

### Validation du chemin réel (2026-10-25)

- **Client officiel via le proxy** : `opencode` 1.18.31 configuré sur
  `baseURL = http://127.0.0.1:4000/v1`, `opencode run` → `OK`. Requête
  enregistrée `success=1`, `is_stream=1`, `tools` = les 10 officiels : le
  payload complet traverse le proxy en passthrough.
- **Façade Anthropic** `/v1/messages` avec un outil client : `200`,
  `stop_reason="tool_use"`, bloc `tool_use` `{"city":"Paris"}`.
- **Façade Responses** `/v1/responses` avec un outil client : `200`,
  item `function_call` correctement formé (`call_id`, `name`, `arguments`).
  Bug corrigé le 2026-10-25 : les formes courtes `"input": "texte"` et
  `"content": "texte"` étaient **perdues** (la boucle itérait la chaîne
  caractère par caractère) — le modèle recevait une requête vide et
  répondait « Bonjour, comment puis-je vous aider ? ». Après normalisation,
  les trois formes d'`input` donnent la même réponse et l'outil du client est
  bien appelé.
- **Tour d'agent complet via le client officiel** : « combien de `.py` dans le
  dossier ? » → le client appelle `Glob "*.py"` (2 matches), exécute l'outil,
  renvoie le résultat au modèle, réponse finale `2`. La boucle complète
  (outil → exécution → second tour) fonctionne en gratuit.
- **Client tiers réel à 22 outils** (`browser_exec`, `terminal`, …) : `200`
  (10 officiels + 22 extras = 32 outils). La fusion tient à l'échelle.

### Tenue sur une heure de trafic (2026-10-25, 08:39 → 09:39 UTC)

```
deepseek-v4.1-flash-free   n=76   ok=75  ( 98,7 %)  avg=6,7 s   max=28,9 s
mimo-v2.6-flash-free       n=63   ok=63  (100,0 %)  avg=13,8 s  max=61,9 s
glm-5.3-flash-free         n=57   ok=57  (100,0 %)  avg=8,8 s   max=71,5 s
échecs : 1 (429)
jev (systemone)            n=78
```

Les maxima élevés sont des générations longues (raisonnement + sorties
verbeuses), pas des reprises : une seule requête a échoué sur 196.

## 8. Coût et limites

| Point | Valeur | Commentaire |
|---|---|---|
| Tokens d'entrée | ~5–7 k selon le client | prompt système officiel (client v2 : 9,7 k car. + 12 outils ; générique : 21,6 k car. + `read` + ses outils) |
| Cache | 11 008 tokens cachés (v1) | le prompt est stable → coût réel faible |
| Outils du client | **fusionnés** après les 10 officiels | le *function calling* tiers est préservé (mesuré : extras acceptés) |
| `tool_calls` non-stream | recomposés depuis le SSE | bug corrigé le 2026-10-25 : ils étaient jetés, message vide |
| `reasoning_content` non-stream | recomposé depuis le SSE | bug corrigé le 2026-10-25 : mimo et deepseek l'émettent, il était jeté |
| Raisonnement (3 protocoles) | **porté partout** | mesuré 2026-10-25 : chat `reasoning_content`, Anthropic bloc `thinking` + `thinking_delta`, Responses item `reasoning` + événements |
| Latence observée | p50 4,4 s (deepseek), 8,1 s (glm), 11,4 s (mimo) | complétion entière avec ~11k tokens d'entrée ; le SLO `p50 < 4 s` du health vise le TTFT, pas la génération |
| SLO health | cibles pilotées par `observability.slo_targets` (12 s / 30 s) + `slo_targets_by_model` | la série `leg_ms` mesure la complétion complète ; `mimo` (raisonnement verbeux) a sa propre cible p95 45 s. Vérifié : `ok` |
| Comportement | le modèle se comporte en agent de code | conséquence directe du prompt système officiel |
| Choix d'outil | peut privilégier un outil officiel | observé quand la requête est vide ; avec une entrée valide, l'outil du client est appelé (2/2 mesures) |
| Erreurs en cours de flux | HTTP 200 déjà engagé | **préflight ajouté le 2026-10-25** : quand aucune station chaude ne peut servir, le client reçoit un vrai `429` + `Retry-After` **avant** l'ouverture du flux. Une erreur qui survient après le début du flux reste en `200 + data:{error}` (inévitable) |
| Forme `responses` | non couverte par la capture | elle passe aujourd'hui via conversion → chat |

Si un jour un client tiers a besoin de ses propres outils, il faut **mesurer**
si le gate accepte les 10 officiels **plus** des outils supplémentaires. Ce n'est
pas testé, et ce n'est pas nécessaire au fonctionnement actuel.

## 9. Latence : où part le temps (mesuré 2026-10-25)

| Poste | Coût mesuré | Commentaire |
|---|---|---|
| Traitement proxy (injection 44 Ko, sérialisation) | **0,02–0,07 ms** | négligeable |
| Estimation de tokens (tiktoken, 11k) | 1,6 ms médian | premier appel 110 ms (chargement) |
| Identité par IP, empreinte TLS | ~0 ms | cache mémoire |
| Aller-retour réseau via le tunnel | **~160 ms** (session chaude) | ~600 ms si handshake TCP+TLS |
| Génération du modèle | **1,4–2,3 s** pour 8 tokens ; 6,5 s médian sur réponses réelles | hors de portée du proxy |

Conclusion : le proxy représente **2 à 3 %** du temps total. Le seul gain
réel côté proxy était le handshake à froid.

### Préchauffage TLS

Le pool curl évince une session après **10 min d'inactivité**. La requête
suivante repayait alors ~600 ms de handshake. Un GET minuscule sur
`https://opencode.ai/robots.txt` toutes les **5 min** garde la connexion
chaude — aucune requête d'inférence, donc **aucun quota consommé**
(`free_prewarm` dans `config.yaml`).

Gain : ~440 ms sur la première requête après une période d'inactivité.
Le reste de la latence est la génération du modèle : pour aller plus vite il
faut changer de modèle (`deepseek` ≈ 6,5 s médian, `mimo` ≈ 21 s) ou activer
le hedging, qui double la consommation de quota.

### Hedging de flux (streaming)

`free_parallel.mode: hedge` course **deux stations** et garde le premier flux
qui émet un token ; le perdant est annulé et fermé, et la comptabilité n'est
incrémentée que pour le gagnant (`note_hedge_winner`). La seconde ne part que
si la primaire n'a rien produit après `hedge_delay_ms` (1 500 ms) — inutile de
payer une requête quand la première répond vite.

Fonctionne aussi pour le non-stream (mécanisme historique). Coût : une requête
supplémentaire **par course**, donc le quota fond plus vite ; c'est pourquoi
le mode est un simple menu dans le GUI (VPN → stations free), avec le toggle
« Paralléliser les stations free » pour tout couper.

Premières mesures après activation : `{primary: 1, hedge: 3, total: 4}` — trois
courses sur quatre gagnées par la station secondaire, sans dégrader les flux
(vérifié : chat/messages/responses en streaming, outils et raisonnement).

## 10. Vérifier et maintenir

### Vérification rapide

```bash
python scripts/verify_free_leg.py            # 6 contrôles, 3 requêtes
python scripts/verify_free_leg.py --full     # + outils et raisonnement, 5 requêtes
```

Le script vérifie le payload officiel, le canary, puis fait de **vrais appels**
sur les trois protocoles (prompts uniques pour ne pas être servi par le cache).
Sortie : tableau PASS/FAIL, code de sortie 1 au premier échec — utilisable en
supervision.

Contrôle manuel équivalent, sur un seul endpoint :

```bash
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash-free","messages":[{"role":"user","content":"Dis OK"}],"max_tokens":8}'
```

Attendu : `200` avec `"content":"OK"`.

### Vérification continue (canary)

Le proxy sonde déjà la jambe free en continu (`_free_canary_loop`) :

```bash
curl -s "http://127.0.0.1:4000/health?detail=full" | python -c "import json,sys; print(json.load(sys.stdin)['canary']['probes'])"
# {'free.chat': {'verdict': 'ok', 'ok_rate': 1.0, ...}, 'free.messages': {...}}
```

Le 2026-10-25 après correctif : `free.chat` **10/10** et `free.messages`
**10/10**, verdict `ok`. Si le gate change encore, ce canary passera en
`failing` en quelques cycles — sans attendre qu'un utilisateur se plaigne.

### Santé de la copie officielle

```bash
curl -s "http://127.0.0.1:4000/health?detail=full" | python -c "import json,sys; print(json.load(sys.stdin)['free_payload'])"
# {'tools': 10, 'system_chars': 21603, 'ok': True}
```

`ok: false` (fichier absent, tronqué, tools manquants) signifie que toute la
jambe free va répondre 403 : régénérer avec
`scripts/capture_official_payload.py`. Un warning est aussi émis dans les logs
au premier chargement invalide.

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

## 11. Chronologie de la recherche (pour mémoire)

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
8. **25/10** — correctif, tests, preuve bout-en-bout, documentation.
9. **25/10** — tâches planifiées de capture (`OpenCodeQuotaWatch`,
   `OpenCodeQuotaResetCapture`) supprimées : elles n'avaient plus d'objet et
   auraient consommé du quota free pour rien.

## 12. Ce que ce document remplace

Les plans antérieurs (`PLAN-corrections-429.md`, `PLAN_CORRECTION_FAUX_429.md`,
`PLAN_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-09.md`) décrivaient le gate d'avant le
durcissement. Ils restent valables pour l'identité, le TLS, les stations et la
gestion des 429 de quota, mais **le critère d'acceptation du corps est celui
ci-dessus**.
