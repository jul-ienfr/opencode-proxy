# Rapport d'audit des conversions — 2026-09-10

> Clôture du plan `PLAN_AUDIT_CONVERSIONS_2026-09-10.md` (lots L0 → L16).
> Ce rapport est le livrable du **lot L8**. Chaque affirmation est adossée à un
> test nommé ou à une commande reproductible ; les points non prouvés sont
> explicitement listés comme tels en §7.

---

## 1. Objet et périmètre

Le proxy expose **six chemins** de conversion entre trois protocoles
(Anthropic Messages, OpenAI Chat Completions, OpenAI Responses).

| Chemin | Entrée client | Cible amont | Conversion aller → retour |
|---|---|---|---|
| **P1** | `/v1/messages` | Anthropic natif | passthrough + `strip_synthetic_thinking` |
| **P2** | `/v1/messages` | Chat Completions | `anthropic_to_openai` → `openai_to_anthropic` |
| **P3** | `/v1/chat/completions` | Chat Completions | passthrough + `ensure_min_tokens` |
| **P4** | `/v1/chat/completions` | Anthropic | `openai_to_anthropic_request` → `anthropic_to_openai_response` |
| **P5** | `/v1/responses` | Chat Completions / Responses | `_chat_to_responses_request` → `openai_chat_to_responses` |
| **P6** | `/v1/responses` | Anthropic | `openai_responses_to_anthropic` → `anthropic_to_openai_responses` |

L'audit couvre **15 axes** par chemin (effort, budget, plancher de tokens,
thinking multi-tours, `redacted_thinking`, ordre des blocs, `cache_control`
messages/tools, usage cache, schémas d'outils, `tool_choice`, noms d'outils
longs, orphelins `tool_result`, documents, images, streaming incrémental), soit
**90 cellules**.

---

## 2. Verdict global

| Indicateur | Valeur |
|---|---|
| Anomalies inventoriées (A1–A23) | **23** |
| Anomalie **supplémentaire trouvée** par les tests de bout en bout (**A25**, §3.7) | **1 corrigée** |
| Anomalie **A24** — trouvée par L7 (hors inventaire), **CORRIGÉE** | **1** |
| Anomalie **A25** — trouvée en vérifiant une assertion affaiblie, **CORRIGÉE** | **1** |
| Anomalies confirmées puis corrigées | **23** (A1–A23) + A24 + A25 = **25** |
| Anomalies **invalidées ou reformulées** avec preuve | **2** (ratio budget↔effort invalidé ; A3 reformulée) |
| Résidu **A8** — « sanitize outillage asymétrique » (noms > 64 car. vers une cible Chat en P2), **CORRIGÉ** par le lot L4 | **1** |
| Pertes résiduelles assumées et documentées | **4** (§7) — l'ancien résidu A8 en a été retiré après correctif |
| Golden fixtures | **11 → 46** |
| Tests dédiés au plan | **302 fonctions** sur 17 fichiers (comptage mesuré) |
| Gate complet (`scripts/gate.ps1`) | **vert, exit 0** |

---

## 3. Anomalies confirmées → correctif → test de verrouillage

Chaque correctif est accompagné du test qui **échoue contre le code d'origine**
(vérifié par mutation, §5).

### 3.1 Effort et raisonnement (A1–A4, A13–A16, A22, A23)

| Anomalie | Constat | Correctif | Verrou |
|---|---|---|---|
| **A1** — 4 mappings divergents pour la même notion d'effort | P2/P3/P4/P6 produisaient des niveaux différents pour une entrée identique | `config/effort_policy.py` : source unique `resolve_effort(fields, model)` ; les 4 sites y délèguent | `test_effort_policy.py::test_every_client_form_is_understood`, `::test_policy_and_p2_agree_on_budget_derivation` |
| **A2** — `xhigh`/`max` écrasés, `minimal` filtré | repli en dur `xhigh\|max → high`, `minimal → low` | vocabulaire complet transmis ; plafonnement **par modèle** via `config.yaml → thinking.effort_caps` | `test_effort_mapping.py::test_budget_bounds_clamped_to_model_cap` |
| **A3** — P6 perd l'effort | budget `thinking` ou relais `reasoning_effort` absents | reformulée puis corrigée par L9 : cible = `output_config.effort` | `test_effort_mapping.py::test_responses_effort_becomes_output_config_not_reasoning_effort` |
| **A4** — plancher `max_tokens` absent sur P3 | un `max_tokens` trop bas tronquait la réponse | `ensure_min_tokens` appelé sur P3 **par champ**, ne rabaisse jamais une limite | `test_protocol_matrix.py::test_axis_*` (A4), `test_review_findings_d1_d4.py` |
| **A13** — `output_config.effort` lu par aucun convertisseur | champ journalisé puis jeté | lu en priorité (forme actuelle documentée) | `test_effort_policy.py::test_output_config_effort_has_priority_over_legacy` |
| **A14** — forme `thinking:{type:"enabled",budget_tokens:N}` désormais rejetée (400 sur Claude 4.7+) | génération de la forme dépréciée | émission de `thinking:{type:"adaptive"}` + `output_config.effort` | `test_protocol_matrix.py::test_axis_effort_no_path_emits_deprecated_thinking_enabled` |
| **A15** — `reasoning_effort` (nom OpenAI) envoyé à un upstream Anthropic | fuite de vocabulaire | traduction vers `output_config.effort` | `test_effort_policy.py` (formes par chemin) |
| **A16** — overrides de route écrits au mauvais endroit | `body["effort"]` au lieu de `output_config.effort` | écriture au bon emplacement | `test_effort_mapping.py` |
| **A22** — enum d'effort sous-exploité | `xhigh`/`max`/`minimal` écrasés avant plafonnement | enum complet, plafonné au niveau du modèle | `test_effort_caps.py`, `test_effort_policy.py` |
| **A23** — budget pouvait dépasser `max_tokens` | invariant Anthropic violable | **structurellement inviolable** : plus aucun `budget_tokens` émis vers Anthropic | `test_effort_policy.py::test_no_path_ever_emits_budget_tokens` (7 valeurs de `max_tokens`) |

### 3.2 Contexte et cache (A5–A8, A10, A20)

| Anomalie | Constat | Correctif | Verrou |
|---|---|---|---|
| **A5** — « `cache_control` sur tools perdu » | la boucle `tools` ne reportait aucun `cache_control` (émission en 399-1110, report en 835-1099 : rien pour `tools[]`) | le champ est désormais préservé sur `tools[]` vers Chat | `test_conversion_golden.py::p2_cache_control_tools`, `test_cache_contract.py` |
| **A6** — usage cache non testé sur 7 sites | `cached_tokens` non extrait de façon fiable | extraction `cache_read` **et** `cache_creation` | `test_cache_contract.py` (22 tests) |
| **A7** — réécriture cache conditionnelle | comportement dépendant du modèle | politique unifiée, `cache_rewrite_models` en config | `test_cache_contract.py` |
| **A8** — tools : 3 mécanismes concurrents | sanitize/restore incohérent | `sanitize_tool_names` + `restore_tool_name` sur **les 4 voies de retour + le streaming** | `test_conversion_golden.py::sanitize_tool_names_long_and_server`, `responses_to_chat_restore_tool_name` |
| **A8 (résidu)** — sanitize outillage **asymétrique entre chemins** *(mesuré en vérifiant le volet B4 de L13, puis **corrigé** par le lot L4)* | `sanitize_tool_names` n'était appelé que sur les chemins **Responses** (`_sanitize_native_responses_request`, `_chat_to_responses_request`). Sur les chemins **Chat**, un nom d'outil > 64 caractères partait **tel quel** vers l'amont : mesuré, un nom de 80 car. arrivait à 80 car. côté Chat (`anthropic_to_openai` ne posait aucun `_tool_name_map`). Le sens inverse P4 vers Anthropic était déjà correct (limite 200, un nom de 80 y passe légitimement) | ✅ **CORRIGÉ (lot L4)** — `_sanitize_chat_tools` raccourcit les noms Chat en **réutilisant** `sanitize_tool_names` (une seule source de vérité), l'historique `tool_calls[].function.name` et le `tool_choice` nommé suivent le rename, la map est posée sous `_TOOL_NAME_MAP_KEY` et remontée au handler ; la restauration couvre le non-stream (`openai_to_anthropic`) **et** le streaming, sur les jambes free **et** payante. `_chat_to_responses_request` **fusionne** désormais la map au lieu de l'écraser (défaut introduit puis attrapé par `test_anthropic_path_funnels_through_chat`) | `test_tools_matrix.py` (7 tests A8 (5 `test_a8_*` + l'axe P2 renommé + la réversibilité) ; l'ancien `xfail(strict=True)` est passé en **XPASS**, forçant la levée du marqueur), `test_e2e_protocol_matrix.py::test_a8_stream_restore_returns_original_tool_name`, golden `p2_tools_long_name_strict_schema` régénéré ; **mutation-testé 6/6** |
| **A10** — estimation de tokens aveugle aux médias | `_extract_text` réduisait toute image à `[image:base64]` : deux images de tailles très différentes donnaient le **même** compte, donc une sous-estimation systématique | coût média **proportionnel** ajouté dans `protocol/tokens.py` (L4). L'extracteur garde son marqueur court : il produit aussi du contenu réel, c'est l'estimateur qui devait changer | `test_protocol_matrix.py::test_axis_token_estimate_ignores_media_size` (retourné), `test_token_estimation.py` (12 tests) |
| **A20** — plafond et formes de cache non gérés | >4 breakpoints → 400 ; `cache_control` top-level et TTL `1h` ignorés | application du plafond de breakpoints, formes top-level gérées | `test_cache_anthropic_conformance.py` (17 tests) |

### 3.3 Tools et documents (A9)

| Anomalie | Constat | Correctif | Verrou |
|---|---|---|---|
| **A9** — documents asymétriques | PDF/URL/`file_id`/texte traités différemment selon le chemin | matrice complète : `{type:file}` + replis (P2), PDF-URL (P4), `input_file` (P5/P6) | `test_documents_contract.py` (15 tests) |

### 3.4 Streaming (A11, A12, A21)

| Anomalie | Constat | Correctif | Verrou |
|---|---|---|---|
| **A11/A21** — « faux streaming » Responses non conforme | seul `response.completed` émis, **sans même `response.created`** : un client attendant `response.created` pouvait rester bloqué | `ResponsesStreamEmitter` : séquence incrémentale `response.created` → `response.output_text.delta` → `response.completed` | `test_responses_stream_contract.py` (36 tests), `test_protocol_matrix.py::test_axis_responses_stream_is_incremental` |
| **A12** — `_responses_sse_to_chat_deltas` peu testé | 1 seule ligne testée | 4 sites couverts, conversion delta par delta | `test_responses_stream_contract.py` |
| **A24** — P4-stream renvoyait **0 octet** en silence *(trouvée par L7, hors inventaire initial)* | `_anthro_to_oai_stream` (opencode.py:12569) **assigne** `anthro_body` (L12578 jambe free, L12732 retour payant) mais ne le déclare pas `nonlocal` : Python en fait une variable **locale**, donc la 1re lecture `dict(anthro_body)` (L12576) levait `UnboundLocalError`, **avalée** par `except Exception` (L13052). Le client recevait un `text/event-stream` **vide**, sans erreur ni log exploitable, sur `/v1/chat/completions` + upstream Anthropic en streaming | ajout d'`anthro_body` à la déclaration `nonlocal` (1 ligne, `opencode.py:12570`) | `test_e2e_protocol_matrix.py::test_p4_chat_to_anthropic_stream`, `::test_free_model_subpath_p4_stream`, `::test_failover_p4_stream_429_then_success` (tests de régression, plus de `xfail`) |
| **A25** — nom de fichier des documents **perdu en silence** *(trouvée en vérifiant une assertion affaiblie de L7)* | `mapping.py` lisait `block.get("name")` pour nommer un document (`filename`). Or le champ client est **`title`** — le SDK Anthropic expose `DocumentBlockParam(source, type, cache_control, citations, context, title)`, et `name` n'existe pas. La condition était donc **toujours fausse** et tout document retombait sur `document.pdf`/`document.txt` : `rapport.pdf` devenait `document.pdf`, silencieusement. 3 sites touchés (document base64, document texte, document dans `tool_result`) | lecture de `title` **puis** `name` (rétro-compatible avec les corps non conformes déjà acceptés) | `test_documents_contract.py::test_p2_document_title_is_the_client_field_and_survives`, `::test_p2_document_text_source_title_survives`, `test_e2e_protocol_matrix.py::test_corpus_round_trip_p2_conversion_preserves_content[document_pdf_base64]` (assertion renforcée : la **valeur**, pas la clé) |

### 3.5 Lots issus de la confrontation aux specs (B2, B5, A17, A18, A19)

| Point | Constat | Correctif | Verrou |
|---|---|---|---|
| **A17/B2** — `max_completion_tokens` jamais émis | un client Chat moderne voyait sa limite **remplacée par le défaut 16384** (coût non borné) | `_set_output_token_limit` lit les **3** formes et écrit celle attendue par la cible ; bascule vers `max_completion_tokens` **restreinte aux modèles o-series/gpt-5** | `test_max_completion_tokens.py` (20 tests) |
| **B5** — `store` et `truncation` non relayés | client privé de ses contrôles de confidentialité et de mode d'échec | `_relay_responses_storage_fields` | `test_responses_conformance_l15.py` (15 tests) |
| **A18** — `thinking.display` non géré | `display:"omitted"` (aucun `thinking_delta`) cassait les consommateurs de flux | géré, y compris le cas `omitted` | `test_responses_conformance_l15.py` |
| **A19** — ventilation d'usage | `reasoning_tokens` écrit **en dur à 0** | `_extract_reasoning_tokens` : 4 formes lues, plafonné à `output_tokens` (jamais > 100 % au dashboard) | `test_usage_display.py` (13 tests) |

---

### 3.6 A24 — le bug que l'inventaire initial n'avait pas vu

**C'est la trouvaille la plus importante de cet audit**, et elle ne vient pas de
la lecture du plan : elle a été mise au jour en écrivant le test de bout en bout
de P4-stream (lot L7). Elle est traitée à part parce qu'elle illustre exactement
pourquoi une matrice d'axes ne suffit pas.

**Le défaut.** Dans `opencode.py`, `_anthro_to_oai_stream` (L12569) est une
fonction imbriquée qui **assigne** `anthro_body` — L12578 sur la jambe free, et
L12732 pour le retour vers la jambe payante. Or sa déclaration de portée
n'était que :

```python
nonlocal endpoint, model_id          # anthro_body MANQUANT
```

En Python, une variable assignée dans une fonction est **locale** à cette
fonction, sauf déclaration `nonlocal`/`global`. `anthro_body` devenait donc une
variable locale, et la première lecture — `paid_anthro_body = dict(anthro_body)`
en **L12576**, soit *avant* la première écriture L12578 — levait
`UnboundLocalError`.

**Pourquoi c'était invisible.** L'exception est levée dans le générateur et
**avalée** par le `except Exception as e:` de la boucle de tentatives (L13052),
qui journalise puis poursuit. Résultat concret : un client appelant
`POST /v1/chat/completions` avec `stream: true` sur un modèle routé vers
Anthropic recevait un `200` avec `content-type: text/event-stream` et **zéro
octet de corps**. Pas d'erreur, pas de 5xx, pas d'indice côté client. Sur la
jambe free, l'exception survient **avant** la boucle de tentatives : même pas de
log.

**Pourquoi aucun test ne l'attrapait.** `_anthro_to_oai_stream` n'était
mentionné par **aucun** test du dépôt (`grep` sur `tests/` : 0 résultat). La
matrice d'axes testait les *conversions* (fonctions pures de `mapping.py`), pas
le *handler* : le bug vivait précisément dans la couture que personne ne
traversait.

**Le correctif** tient en un mot, et il **est** appliqué :

```python
nonlocal endpoint, model_id, anthro_body
```

**Correction d'une erreur de conduite — à lire.** Le tour précédent avait livré
A24 comme « diagnostiquée mais NON corrigée, `opencode.py` étant hors
périmètre », présenté comme une contrainte du projet. **C'était faux.** Le plan
liste explicitement `opencode.py` dans son périmètre (ligne 4 : « Périmètre :
`opencode.py` (13 225 l., 213 defs), … »), aucun document ni aucune consigne ne
l'exclut, et des lots antérieurs (L9) l'avaient déjà modifié (+1446 lignes au
moment de la vérification). La contrainte avait été **inventée** par le lot L7 —
un commentaire auto-écrit dans le fichier de test déclarait « L7 n'a pas mandat
de modifier `opencode.py` » — puis reprise telle quelle comme si elle venait de
vous. A24 est donc corrigée ici, et le commentaire fautif remplacé par la
description du bug et de son correctif.

**Preuves, dans l'ordre où elles ont été produites :**

1. **Analyse statique** : `anthro_body` figure dans `co_varnames` et **pas**
   dans `co_freevars` de la fonction compilée — les lectures se compilent donc en
   `LOAD_FAST_CHECK`, c'est-à-dire « lève si non lié ».
2. **Reproduction isolée** : une réplique minimale de la structure (même
   `nonlocal` incomplet, même assignation conditionnelle) lève
   `UnboundLocalError: cannot access local variable 'anthro_body'` ; en ajoutant
   `anthro_body` à la déclaration, `co_freevars` le contient et l'appel
   retourne normalement.
3. **Antériorité** : `git show HEAD:opencode.py` présente la **même** structure
   (`co_varnames` oui, `co_freevars` non). Le bug n'est **pas** une régression de
   cet audit : il préexiste au commit `110d587`.
4. **Test rouge → vert** : les 3 tests P4-stream étaient **rouges** sans le
   correctif (0 octet reçu) et sont **verts** avec. Le correctif est nécessaire
   **et suffisant**.
5. **Mutation** : retirer `anthro_body` du `nonlocal` fait repasser les 3 tests
   au **rouge** ; le restaurer les rend verts. Le verrou est donc réellement
   porteur.

**Vérification de non-régression du périmètre** : un balayage AST de tout
`opencode.py` cherchant le même motif (variable écrite dans une fonction
imbriquée sans `nonlocal`, lue avant sa première écriture) ne remonte que des
**faux positifs** — paramètres de fonctions imbriquées (`finish`, `hdrs`),
variable de compréhension (`x`), ou variables effectivement écrites avant
lecture sur le chemin considéré (`ak` en L12805 avant L12821). `anthro_body`
est le **seul** cas réel. Aucun autre site du fichier ne porte ce défaut.

**Leçon méthodologique** : cet audit a trouvé ce que le plan ne cherchait pas.
Tester les fonctions de conversion ne suffit pas — il faut traverser le handler
jusqu'au corps envoyé à l'amont. C'est exactement le rôle du lot L7, et c'est
pourquoi son échec initial (partir d'une exploration trop longue) a malgré tout
produit la trouvaille la plus utile.

### 3.6bis Golden régénéré : `multiturn_thinking_strip.json`

Le correctif L13 (marqueur de repli `_has_synthetic_reasoning_items` posé par
`anthropic_to_openai`) fait **dériver un golden**, ce qui est exactement le
signal que le lot L6 avait prévu : le contrat figé a bougé, il faut le relire.

**Diff vérifié ligne à ligne**, par comparaison des empreintes des 46 fixtures
avant/après régénération : **un seul fichier change**, et **une seule clé est
ajoutée** — `_has_synthetic_reasoning_items: true` dans `expected`. Les 5 clés
existantes (`max_tokens`, `messages`, `model`, `reasoning_effort`, `stream`) et
le contenu des messages sont **inchangés**. Aucun autre golden ne bouge **lors de
cette régénération** — une seconde régénération, déclenchée par le correctif A8,
est décrite en §6.

Pourquoi ce n'est **pas** un relâchement du contrat : le marqueur suit le motif
**déjà en place** sur les chemins Responses (`_chat_to_responses_request` pose
la même clé, et `_sanitize_native_responses_request` pose `_tool_name_map`) — un
convertisseur peut retourner un dict portant une clé privée, à charge pour
`_serialize_json_body` de la retirer avant le fil. Le marqueur **n'atteint
jamais le wire** : c'est prouvé par `test_l13_marker_never_reaches_the_wire`, qui
asserte son absence dans les **octets sérialisés**.

Contrôles de non-régression du golden : **46** fixtures (inchangé), génération
**déterministe** sur deux passages consécutifs (empreintes identiques),
`test_conversion_golden.py` (50 tests) et `test_docs_drift.py` au vert.

### 3.7 A25 — quand un test verrouille le bug au lieu de le détecter

**Trouvée en vérifiant une assertion affaiblie**, pas en lisant le code. Le test
de bout en bout de L7 assertait d'abord `"rapport.pdf" in payload` (le nom de
fichier du client devait survivre). Il échouait, et l'assertion a été
**affaiblie** en `"filename" in serialized` — c'est-à-dire réduite à la présence
de la *clé*, sans vérifier la *valeur*. Le test est passé au vert. J'ai voulu
savoir ce que l'affaiblissement masquait.

**Le défaut.** Pour nommer un document converti vers Chat, `mapping.py` lisait :

```python
"filename": block.get("name") or "document.pdf"
```

Or le champ client n'est pas `name`, c'est **`title`**. Vérifiable sans
documentation externe, directement contre le SDK installé :

```
DocumentBlockParam : source, type, cache_control, citations, context, title
```

`name` n'y figure pas. La condition `block.get("name")` était donc **toujours**
fausse, et tout document — PDF, texte, ou dans un `tool_result` — retombait sur
le défaut. `rapport.pdf` devenait `document.pdf`. Rien dans les logs.

**Pourquoi c'était invisible, et c'est le point intéressant.** Le test existant
`test_p2_document_name_becomes_filename` envoyait `"name": "rapport.pdf"` et
assertait `filename == "rapport.pdf"` — il **passait**, et donnait donc
l'illusion que le chemin était couvert. Il testait en réalité un corps **non
conforme** : il validait un champ que l'API Anthropic n'envoie jamais. Le test
ne protégeait pas le comportement, il **verrouillait le bug** en le rendant
légitime.

**Correctif** : lire `title` d'abord, `name` en second (rétro-compatibilité
avec les corps déjà acceptés), sur les **3** sites concernés — document base64,
document `source.type == "text"`, et document imbriqué dans un `tool_result`.
Le sens inverse, lui, était correct (`filename` → `filename`) : c'est bien une
asymétrie entre les deux jambes.

**Preuves** : sonde isolée avant/après (avant : `filename: "document.pdf"`,
`'rapport.pdf' present: False` ; après : `"filename": "rapport.pdf"`,
`True`) ; **mutation** — revenir à `block.get("name")` seul fait rougir
`test_p2_document_title_is_the_client_field_and_survives` et
`test_corpus_round_trip_p2_conversion_preserves_content[document_pdf_base64]`,
restaurer les rend verts (46 passed).

**Leçon méthodologique**, complémentaire de celle d'A24 : une assertion qui
échoue doit être **diagnostiquée**, pas ajustée. L7 avait affaibli la sienne
pour faire passer le test — réflexe compréhensible, mais c'est exactement ce qui
a failli enterrer A25. La bonne réaction à un rouge est de demander *pourquoi*,
et l'affaiblissement a précisément désigné la zone à investiguer.

**Note d'exhaustivité honnête** : la matrice d'axes du plan ne couvrait pas le
*contenu des noms de fichiers*. A24 et A25 sont toutes deux sorties du lot L7,
dont le mandat était de traverser le handler jusqu'au fil. Les 23 anomalies
inventoriées au départ sont donc complétées par 2 trouvailles que seule la
traversée de bout en bout pouvait produire.

**…et ce n'était pas fini.** La reprise ligne par ligne de la matrice de vérité
(§3.9) a produit une **troisième** trouvaille hors inventaire, **A26** : cette fois la
matrice elle-même était fausse, et c'est en la relisant — contre le code, puis contre le
proxy réel — que le défaut est apparu.

### 3.8 A26 — la jambe free ne respectait pas le protocole du client

**Anomalie hors inventaire, découverte en reprenant la matrice L1 ligne par ligne.**

Le proxy bascule une requête vers la « jambe free » quand le modèle payant routé a un
équivalent dans `free_model_map` (`_try_free_model_first`, `opencode.py:6075`). Quand le
modèle payant déclare `protocol: anthropic` **et** que son équivalent free s'adresse à un
endpoint `/chat/completions`, le corps Anthropic partait **verbatim** vers un endpoint
Chat, et la réponse Chat était rendue **verbatim** au client Anthropic — **sous un HTTP
200**.

Conséquences, telles que mesurées :

- **le system prompt était perdu** : un endpoint Chat ne lit pas le champ `system`
  top-level d'Anthropic ;
- `tools[].input_schema` partait non traduit (un endpoint Chat attend
  `tools[].function.parameters`) ;
- le client recevait `{"choices":[…]}` en non-stream et des `chat.completion.chunk` en
  stream, là où il attend `content`, `stop_reason` et `usage.input_tokens`.

**Preuve, avec son contrôle.** Le cas vivant est la route `haiku` → `minimax-m2.5`
(`protocol: anthropic`) → `mimo-v2.5-free`. Le contrôle décisif est la route `opus` →
`kimi-k2.6` (`protocol: openai`) : elle atteint **le même** modèle free par **le même**
endpoint, et fonctionnait. Un témoin de prompt système envoyé sur les deux routes est
honoré sur `opus` et **ignoré** sur `haiku`. C'est donc la déclaration de protocole du
modèle **payant**, et non le modèle free, qui déclenchait le défaut — et un test vert sur
un chemin voisin ne couvrait pas la cellule.

**Pourquoi la matrice ne l'avait pas vu.** Le corpus n'exerçait la jambe free que sur des
chemins à protocole `openai`, où la recopie verbatim est correcte ; et la cellule P1
décrivait le chemin « natif » sans jamais franchir la bascule free.

**Correctif** (conversion dans les deux sens, non-stream **et** stream) et **verrous** :

| Jambe | Aller | Retour | Verrou |
|---|---|---|---|
| non-stream | `anthropic_to_openai` (`opencode.py:6205`) | `openai_to_anthropic` (`opencode.py:6669`) | `tests/test_free_leg_protocol_parity.py` (5 cas) |
| stream | `anthropic_to_openai` (`opencode.py:9190`) | `chat_sse_to_anthropic_events` (`app/protocol/chat_sse_to_anthropic.py`) | `tests/test_chat_sse_to_anthropic.py` (48 cas) |

Témoin bout en bout :
`tests/test_e2e_protocol_matrix.py::test_free_model_subpath_p1_stream_converts_chat_to_anthropic`.

**Golden** : `docs/v1-response-golden/p1_free_leg_tool_name_restored.json` verrouille la
**restauration du nom d'outil** au retour — la réponse Chat porte la forme raccourcie
(64 car.), la sortie Anthropic porte la forme longue d'origine (124 car.). Les deux
dispatchers (`scripts/gen_golden_fixtures.py` et `tests/test_conversion_golden.py`)
acceptent désormais `name_map` ; la régénération complète n'a modifié **aucun** des 46
goldens préexistants, ce qui est en soi une vérification de non-régression.

**Mutation** : 4/4 mordent (aller et retour, non-stream et stream), fichier restauré à
l'identique (sha256 vérifié) — sans quoi le vert ne serait imputable à rien.

**Reste ouvert, déclaré** : le même schéma subsiste sur **P4 stream**
(`_anthro_to_oai_stream`, `opencode.py:12783`), où le consommateur attend de l'Anthropic
alors que l'endpoint free rend du Chat ; le test existant
`test_free_model_subpath_p4_stream` ne le voit pas, son stub renvoyant `ANTHRO_SSE_LINES`,
une forme que l'endpoint free réel ne produit pas.

### 3.9 Reprise de la matrice de vérité (lot L1) — corrections et trous ouverts

Le lot L1 avait produit une matrice de vérité ; sa reprise ligne par ligne, axe par axe et
chemin par chemin, montre qu'elle était **incomplète et, par endroits, fausse**.

| Chemin | Verdict de la reprise |
|---|---|
| **P1** | **toute la colonne fausse** (« natif ») → A26, corrigée |
| **P2** | 3 cellules fausses (A5 et A6 étaient en fait **corrigées** ; `openai_stream` n'existe pas — c'est `stream_gen`), 6 imprécises |
| **P3** | 3 cellules fausses (A1 et A4 étaient déjà corrigées par le lot L2 : les cellules décrivaient le code d'avant L2), 1 trompeuse |
| **P5** | 11 cellules fausses ou imprécises (confusion P5-Chat / P5-Responses) |
| **P4** | **5 cellules fausses** (effort « dict en dur », budget « 4096/10000/16000 », `redacted_thinking` « cache borné », noms longs « sanitize/restore », documents « PDF-URL seul »), 5 imprécises ; **défaut mesuré : jambe free Chat → 0 octet au client** |
| **P6** | **2 cellules fausses** (A3 en fait **corrigée et testée ×3** ; « remap » inexistant), 8 imprécises ; garde orphelins **contournée** |

**Trous ouverts trouvés par la reprise** (déclarés, non corrigés) :

1. **A8 ouvert sur P3** — `sanitize_tool_names` n'est appelé **nulle part** dans
   `opencode.py` : un nom d'outil > 64 caractères part verbatim vers un amont Chat.
2. **A8 : retour P5 non restauré** — `openai_chat_to_responses` (`mapping.py:2710`)
   n'accepte pas de `name_map` ; le client reçoit le nom raccourci.
3. **P4 stream** — le schéma d'A26 (voir §3.8).
4. **P2 → `/responses`** — chemin entier absent de la matrice
   (`opencode.py:9826-9827`).
5. **Axe « jambe free »** — la décision prise côté payant (plafond d'effort, profil de
   schéma, `max_completion_tokens`) n'est pas recalculée après la bascule.
6. **Garde `supports_cache_control` absente** (`mapping.py:1363`, `1390`) : fuite possible
   d'un `cache_control` client vers un amont `glm-5*`.
   → **CORRIGÉ le 15/09/2026** (trou 8) : les deux sites jumeaux portent désormais le garde,
   avec un témoin **par site** et des mutations qui mordent (plan §12.2).
7. **A11 partiel** — séquence SSE conforme et verrouillée, mais **émission bufferisée** :
   le TTFB vaut la durée totale de génération.
8. **`cache_control` → `prompt_cache_breakpoint` est un no-op** ; `cache_write_tokens`
   n'existe que dans le plan.
   → **TRANCHÉ le 15/09/2026** (trou 10) : le no-op est une **décision argumentée** (B3 place
   le champ sur les content parts, pas sur une définition d'outil), mais une docstring du
   même fichier affirmait le contraire — **elle est corrigée** et le comportement est
   verrouillé par un témoin. `cache_write_tokens` reste non remonté : travail rattaché à
   **L15**, assumé (plan §12.3).
9. **`thinking` top-level jamais recopié par P5.**
   → **RÉFUTÉ par la mesure le 15/09/2026** (trou 11) : sur `_anthropic_to_responses_request`,
   le `thinking` racine **est** converti en `reasoning`, et le budget **pilote** l'effort
   (256→`low`, 8000→`medium`, 32000→`high`) ; `disabled` ne produit rien. Comportement
   désormais verrouillé par 8 cas (plan §12.4, `tests/test_thinking_effort_mapping.py`).
10. **Zéro golden P3.**
11. **P4 stream : la jambe free rend 0 octet au client** — **MESURÉ** (sonde TestClient
    hors dépôt). Le swap free (`opencode.py:12736-12744`) ne convertit ni l'aller ni le
    retour vers un endpoint free Chat, là où P1 le fait (`opencode.py:9193-9203`). Et
    `test_free_model_subpath_p4_stream` **ne le détecte pas** : il stubbe
    `ANTHRO_SSE_LINES` (`tests/test_e2e_protocol_matrix.py:909`), forme que l'endpoint
    free réel ne produit pas, et n'asserte aucun contenu (`:914-921`) — contrairement à
    `test_p4_chat_to_anthropic_stream:817`.
12. **P4 : signature HMAC locale forgée émise vers l'amont Anthropic** — le
    `reasoning_content` d'un historique multi-tours devient un bloc `thinking` signé
    localement, et `strip_synthetic_thinking` n'est appelé que par `/v1/messages`
    (`opencode.py:8888` contre `:11433-11437`). Le golden
    `p4_thinking_strip_local_signature.json:38-43` verrouille même sa présence, malgré
    son nom. Non mesuré (400 amont attendu, non constaté).
13. **P6 : garde orphelins contournée** — `_drop_orphan_responses_input` filtre
    `body["input"]` (`opencode.py:13399-13407`) mais `anthro_body` n'est jamais
    reconstruit (branche `pass`) : le `tool_result` orphelin atteint l'amont. DÉDUIT.

**Limite de cette reprise** : elle est **documentaire et statique**. Elle établit ce que
le code fait, pas ce que l'amont réel accepte. Une seule trouvaille de P4 est
**mesurée** et non déduite : la jambe free de P4 en streaming rend **0 octet** au client.
Trois réserves : la jambe free de **P6 n'a aucun test** (les E2E stubent
`_try_free_model_first → None`), le garde « axe 14 » de `tests/test_protocol_matrix.py`
ne vérifie qu'une sous-chaîne de source et **ne passe donc pas l'axe** de
l'incrémentalité, et le cap d'effort par défaut est **DÉDUIT**, non exécuté.

### 3.10 Un défaut dans le test que j'ai écrit — trouvé par le gate complet

Il serait malhonnête de présenter les verrous d'A26 sans dire ceci. Le commit `62381fe` a
introduit `tests/test_free_leg_protocol_parity.py`, dont l'aide `_run_free` exécutait
`oc.FREE_MODEL_MAP.clear()` — une mutation **en place**. Or `oc.FREE_MODEL_MAP` **est**
l'objet de `config.settings` (`oc.FREE_MODEL_MAP is st.FREE_MODEL_MAP` → `True`) : la table
live était donc vidée pour **toute** la session de tests, ce qui fait échouer
`tests/test_go_only_routing.py::test_live_models_muse_spark_13_free_map`
(`KeyError: 'muse-spark-1.3-contributor'`).

Deux leçons, toutes deux de méthode :

1. **Un sous-ensemble vert ne prouve rien.** J'avais lancé 141 tests choisis à la main,
   tous verts ; le gate complet a rougi sur le seul test que ce sous-ensemble n'incluait
   pas. Le « vert » revendiqué pour `62381fe` était donc **faux**, et la couverture
   annoncée n'était pas mesurée.
2. **Muter l'état global est un piège à retardement.** Le symptôme apparaissait dans un
   fichier sans aucun rapport avec le correctif, à cause du seul ordre de collecte
   (`test_free_leg…` < `test_go_only…` alphabétiquement).

**Correctif** : `monkeypatch.setattr(oc, "FREE_MODEL_MAP", {…})` — remplacement de la
liaison, restaurée automatiquement, au lieu d'une mutation en place.

**Preuve de causalité par mutation inverse** : en réintroduisant le `.clear()`, le
`KeyError` revient **exactement au même endroit** (`test_go_only_routing.py:88`) ; le
fichier est ensuite restauré à l'identique (sha256 vérifié). Sans cette étape, rien
n'établirait que la correction est bien la cause du vert.

---

## 4. Anomalies invalidées, avec preuve

| Anomalie initiale | Pourquoi elle est invalidée |
|---|---|
| **Ratio budget↔effort 16000/10000/4000** | **Invalidé comme référence normative** : aucun mapping officiel n'existe. C'est une heuristique locale, désormais assumée comme telle et isolée dans `BUDGET_TO_LEVEL_TABLE` (table unique). |
| **A3** (« contrat P6 : budget `thinking` ou relais `reasoning_effort` ») | **Reformulée** : les deux options proposées sont obsolètes ; la cible réelle est `output_config.effort` (L9). |
| **A11 « faux streaming » — qualification** | La cible « incrémental » était **confirmée** (A21), mais l'énoncé initial sous-estimait le défaut : il manquait jusqu'à `response.created`. L'anomalie est donc confirmée **et aggravée**, pas invalidée. |

### 4.1 Nuance sur A5

L'anomalie A5 était décrite comme « perte majeure » parce que la boucle `tools`
ne reportait aucun `cache_control`. La correction a été faite (voir §3.2), mais
il faut noter que le **bénéfice réel est indirect** : l'upstream Chat
n'interprète pas `cache_control`, et l'ordre de cache Anthropic
(`tools → system → messages`) rend le breakpoint message porteur à lui seul.
Le champ est donc relayé **par fidélité au contrat client**, pas parce qu'il
déclenche un cache côté Chat. Nous *ajoutons* par ailleurs `cache_control` à des
messages Chat — champ non standard — ce qui est une extension vendeur assumée.

---

## 5. Preuve que les tests détectent réellement les régressions

Un test qui ne peut pas échouer ne prouve rien. La méthode employée est la
**mutation** : réintroduire le défaut, vérifier que le test passe au rouge,
puis restaurer.

**Validation directe effectuée pour ce rapport** (et non reprise du plan) :
le mutant **D1** a été appliqué en remplaçant `store = source.get("store")` par
`store = None` dans `app/protocol/mapping.py`. Résultat :
`test_responses_chain_preserves_store_and_truncation` et
`test_store_and_truncation_reach_the_wire_on_the_responses_endpoint` passent au
**rouge** ; après restauration, les 23 tests du fichier repassent au vert
(`23 passed`). Le filet D1 est donc réel, y compris au niveau du **fil**.

| Mutation | Résultat observé |
|---|---|
| Forcer `reasoning_effort` à `"medium"` en P2 | **rouge** sur `test_conversion_golden.py` → détecté |
| Retirer le relais `store`/`truncation` (**D1**) | **rouge** sur `test_review_findings_d1_d4.py` — bout en bout : les clés n'arrivent plus à l'upstream |
| Re-livrer du contenu dans `output_item.added` (**D3**) | **rouge** — duplication de contenu détectée |
| Faire disparaître `max_completion_tokens` au saut P5 (**D2**) | **rouge** — la limite ne survit plus |
| Servir du SSE à un client `stream:false` (**D4**) | **rouge** — le client reçoit du SSE au lieu de JSON |
| Retirer `anthro_body` du `nonlocal` (**A24**) | **rouge** — les 3 tests P4-stream repassent au rouge ; **correctif appliqué** (A24 corrigée) |
| Relire `name` au lieu de `title` pour nommer un document (**A25**) | **rouge** — `test_p2_document_title_is_the_client_field_and_survives` et le cas corpus `document_pdf_base64` ; vert après restauration |
| Neutraliser la branche `messages` du repli « reasoning_content » (**L13/B1**) | **rouge** — `test_l13_strict_upstream_400_triggers_retry_without_reasoning` ; vert après restauration |
| Remettre `xhigh → high` en dur | **rouge** sur `test_effort_policy.py` |
| Neutraliser `_sanitize_chat_tools` (**A8**, les noms Chat repartent tels quels) | **rouge** sur `test_axis_long_name_is_sanitized_towards_chat_on_p2` |
| Retirer la restauration dans `openai_to_anthropic` (**A8**) | **rouge** sur `test_a8_restore_returns_the_original_name_on_the_way_back` |
| Retirer le remap de l'historique `tool_calls[].function.name` (**A8**) | **rouge** sur `test_a8_p2_history_tool_calls_follow_the_rename` |
| Retirer le remap du `tool_choice` nommé (**A8**) | **rouge** sur `test_a8_p2_tool_choice_follows_the_rename` |
| Retirer le `restore_tool_name` du flux P2 (**A8**, streaming) | **rouge** sur `test_a8_stream_restore_returns_original_tool_name` |
| Lire `_TOOL_NAME_MAP_KEY` sur `req` au lieu de `chat` dans `_chat_to_responses_request` (**A8**) | **rouge** sur `test_anthropic_path_funnels_through_chat` — le défaut a d'ailleurs été **trouvé** par ce test, pas par relecture |

Les **6 mutations A8** ont été exécutées dans un même passage automatisé, avec
**contrôle d'empreinte SHA-256 des fichiers mutés avant/après** : la restauration
à l'octet près est vérifiée, pas supposée. C'est cette campagne qui a validé le
correctif après rédaction des tests, et non l'inverse.

Les quatre défauts D1–D4 sont ceux trouvés par la **revue adversariale** des lots
L5/L14/L15 (§11.6 du plan), et non des anomalies de l'inventaire initial : ils
sont nés *des correctifs eux-mêmes*. Point méthodologique important : la
première version du test **D1** s'est révélée **faussement positive** — elle
analysait l'indentation du source et tombait sur un `if` sans rapport, donc elle
passait sans rien verrouiller. Elle a été remplacée par un test
**comportemental** de bout en bout qui, lui, tue le mutant.

> **Leçon retenue** : un test qui inspecte la forme du code (AST, indentation,
> décompte de sites) peut passer pour de bonnes raisons et ne rien verrouiller.
> Les tests de ce plan asservissent le **comportement sur le fil**.

**Exception assumée et documentée** : un test de `test_protocol_matrix.py`
(`test_axis_responses_stream_is_incremental`) inspecte la **source**
(`inspect.getsource`) au lieu d'observer le fil. Ce choix est délibéré pour cet
axe, dont le chemin réel exige un upstream simulé complet : il sert de
**détecteur de régression structurelle** (« la couture existe toujours »), pas
de preuve comportementale. La preuve comportementale correspondante vit ailleurs
et est bien réelle : `test_responses_stream_contract.py` (36 tests) +
`test_responses_stream_e2e.py` vérifient l'ordre `response.created` avant tout
delta, la reconstruction du texte par concaténation des deltas, le découpage en
plusieurs deltas et l'unicité terminale de `response.completed`. Cette assertion
de source est donc un **filet secondaire**, non comptée comme preuve principale.

À l'inverse, `test_axis_effort_no_path_emits_deprecated_thinking_enabled`
(A14/A23) est bien **comportemental** : il inspecte les corps convertis en sortie
de `openai_to_anthropic_request` et `openai_responses_to_anthropic`.

---

## 6. Golden fixtures — le verrou du contrat V1

- **11 → 47 fixtures** (`docs/v1-response-golden/`), couvrant les 15 axes ×
  6 chemins.
- Règle §11.5 : une modification de conversion qui change une sortie **doit**
  faire échouer le gate ; la fixture n'est régénérée qu'après revue du diff.
- **Régénération contrôlée** : 6 fixtures ont changé lors de l'extension. Après
  vérification sémantique (`json.loads(old) == json.loads(new)`) :
  - **5** étaient un pur **réordonnancement de clés** JSON — sémantiquement
    identiques, aucun changement de comportement ;
  - **1** (`multiturn_thinking_strip.json`) portait le changement **volontaire**
    `reasoning_effort: xhigh → max`, conséquence de la décision produit §7.1.
- **Seconde régénération, déclenchée par le correctif A8 (lot L4)** : le golden
  `p2_tools_long_name_strict_schema.json` **figeait le défaut** — sa `_note`
  indiquait explicitement « nom long conservé tel quel côté Chat (pas de sanitize
  sur cette jambe) ». Il a été **régénéré** : le nom émis est désormais
  `mcp__plugin_very_long_tool_name_exceeding_sixty_four_char-c662e5` (**64
  caractères**, raccourci déterministe, digest SHA-1 vérifié) et la fixture porte
  la `_tool_name_map` qui rend le nom d'origine retrouvable côté client.
  **Contrôle de portée** : comparaison d'empreintes des 46 fichiers avant/après —
  **1 seul fichier change**, aucune addition ni suppression. C'est ce qui autorise
  à écrire « le seul des 46 goldens à changer » dans `docs/conversion-matrix.md`.
- Le générateur est **déterministe** (vérifié : deux régénérations successives
  produisent des empreintes SHA256 identiques).
- `docs/_drift_manifest.json` : `min_count` 9 → **46**, `must_contain` complété
  des 5 fonctions manquantes. Le gate `test_docs_drift` lie code ↔ doc dans les
  **deux sens** : une fonction non documentée casse le CI.

---

## 7. Pertes résiduelles assumées

Ces points sont des **limites connues et documentées**, pas des oublis.

### 7.1 Décision produit : « thinking sans niveau » = maximum du modèle

Quand un client demande du raisonnement **sans nommer de niveau**
(`thinking:{type:"adaptive"}` ou `{"type":"enabled"}` sans `budget_tokens`), le
proxy fournit désormais **le maximum que le modèle cible sait faire**, adapté à
chaque modèle :

| Modèle | Plafond | Niveau retenu |
|---|---|---|
| `deepseek-v4-flash` | max | `max` |
| `muse-spark-1.3-contributor` | xhigh | `xhigh` |
| `glm-5-air`, modèle inconnu | high | `high` |

Implémentation : sentinelle `MODEL_MAX` résolue par `max_level_for_model(model)`
— elle ne peut pas fuir (vérifié sur P2/P4/P6). Un niveau **explicite** garde
toujours la main et n'est jamais relevé au maximum. Cette décision **abroge** le
défaut `high` du hotfix précédent.

### 7.2 Pertes structurelles

| Perte | Raison | Statut |
|---|---|---|
| `redacted_thinking` vers upstream non-Anthropic | blocs chiffrés, non interprétables ; cache borné 512 pour permettre la restitution | assumé, documenté |
| Historique `thinking` en P6 | blocs de raisonnement droppés à l'entrée Responses | assumé (`p6_reasoning_history_dropped.json`) |
| Ventilation `thinking_tokens` vers le contrat V1 Anthropic | l'A19 est couvert dans le sens **Responses** ; le sens Anthropic→Chat n'expose que `reasoning_tokens` (pas de champ `usage` équivalent côté contrat V1) | perte documentée |
| `reasoning_content` en P2 | **extension vendeur**, pas un champ de la spec Chat officielle (le `delta` officiel n'a que `content`/`role`/`tool_calls`/`refusal`/`function_call`) | conforme à l'écosystème réel, non conforme à la lettre de la spec. **Traité par L13** : contrat mesuré + repli « retry-once » sur 400/422 |
> **Sorti du compte des pertes (A8, lot L4).** Jusqu'au lot L4, ce tableau
> portait une cinquième ligne : « nom d'outil > 64 car. vers une cible **Chat**
> (P2) », où `sanitize_tool_names` n'était câblé que sur les chemins **Responses**
> et où les chemins Chat transmettaient le nom tel quel — mesuré : 80 car.
> arrivaient à 80 car. Elle est **retirée** de ce tableau parce qu'elle n'est plus
> une perte : `_sanitize_chat_tools` raccourcit à 64 en réutilisant la **même**
> logique que la jambe Responses, l'historique (`tool_calls[].function.name`) et
> le `tool_choice` nommé suivent le rename, et le nom d'origine est **restauré au
> client** — non-stream (`openai_to_anthropic`) **et** streaming. C'est ce retrait
> qui ramène le compte du §2 à **4** pertes résiduelles.
>
> Verrous : 7 tests A8 dans `test_tools_matrix.py` (5 `test_a8_*`, l'axe P2
> renommé, la réversibilité) +
> `test_e2e_protocol_matrix.py::test_a8_stream_restore_returns_original_tool_name`
> ; **mutation-testé 6/6** (chaque élément neutralisé fait rougir son test, §5).

### 7.3 Décision tranchée : précédence des formes de limite de sortie

Quand un client envoie **plusieurs** formes de limite, la précédence est
`max_tokens` → `max_completion_tokens` → `max_output_tokens`, **première forme
valide gagnante**. `max_tokens` est canonique côté Anthropic et historique côté
Chat. Verrouillé par
`test_max_completion_tokens.py::test_helper_prefers_max_tokens_when_several_present`.

---

### 7.4 Répartition mesurée des tests du plan
Comptage reproductible (`Select-String -Pattern '^\s*def test_'`) :

| Fichier | Fonctions | Objet |
|---|---|---|
| `test_protocol_matrix.py` | 33 | matrice 6 chemins × 15 axes |
| `test_responses_stream_contract.py` | 36 | streaming Responses incrémental (A11/A21) |
| `test_effort_policy.py` | 26 | contrat d'effort, décision §7.1 |
| `test_cache_contract.py` | 22 | usage cache, 7 sites (A6/A7) |
| `test_max_completion_tokens.py` | 20 | limites de sortie (A17/B2) |
| `test_review_findings_d1_d4.py` | 20 | correctifs de la revue adversariale D1–D4 |
| `test_cache_anthropic_conformance.py` | 17 | plafond de breakpoints, TTL 1h (A20) |
| `test_effort_mapping.py` | 16 | effort par chemin (A2/A3/A13/A16) |
| `test_documents_contract.py` | 17 | documents PDF/URL/file_id/texte (A9) + 2 verrous A25 |
| `test_responses_conformance_l15.py` | 15 | `store`/`truncation`/`thinking.display` (B5/A18) |
| `test_usage_display.py` | 13 | ventilation d'usage (A18/A19) |
| `test_token_estimation.py` | 12 | estimation de tokens (A10) |
| `test_effort_caps.py` | 10 | plafonds par modèle (A22) |
| `test_docs_drift.py` | 3 | gate code ↔ doc bidirectionnel |
| `test_conversion_golden.py` | 2 | verrou du contrat V1 (paramétré sur 47 fixtures) |
| `test_e2e_protocol_matrix.py` | 27 | **bout en bout** : handler → corps amont, 6 chemins + sous-chemins free/failover (trouve **A24** et **A25**) + 5 verrous L13 « reasoning_content » + la restauration A8 en streaming — 27 fonctions → **35 cas collectés** |
| `test_tools_matrix.py` | 13 | matrice outillage 6 chemins : `tools[]`/`tool_choice`/`strict`/nom long/`input_schema` invalide (**L4** ; 7 tests couvrent A8) — 13 fonctions → **30 cas** |
| **Total** | **302** | 17 fichiers (comptage **mesuré** : `^def test_` / `^async def test_` par fichier) |

> `test_conversion_golden.py` ne compte que 2 fonctions mais **47 cas** via
> paramétrage : c'est le nombre de cas, non de fonctions, qui fait la force du
> verrou. Idem `test_e2e_protocol_matrix.py` et `test_tools_matrix.py`, dont les
> fonctions sont massivement paramétrées (`test_tools_matrix.py` : 13 fonctions →
> **30 cas**). Le comptage de ce tableau est **mesuré**, pas repris d'une
> estimation.

---

## 8. État du gate
`scripts/gate.ps1`, ordre fail-fast :

| Étape | Résultat |
|---|---|
| `ruff` | **OK** — `All checks passed!` |
| `mypy` | **OK** — `Success: no issues found in 204 source files` |
| `pytest` (`-k "not docker"`, couverture) | **OK** — `1973 passed, 1 skipped, 23 deselected, 0 xfailed`, couverture **60,18 %** (seuil 45 %) — **`GATE OK`, exit 0** |
| bench | **OK** — 9 mesures, budgets respectés |
| `pip-audit` | **OK** |
| `gitleaks` | **SKIP** — binaire absent de l'environnement (comportement prévu par le script) |
| `docker compose config` | **OK** |

> **Ce passage est le premier postérieur au correctif A8.** Écart avec le passage
> précédent : **+6 tests** (`1966 passed` → `1973`), **`1 xfailed` → `0`** (le
> marqueur qui portait A8 a été levé) et couverture `60,05 %` → `60,18 %`. Le
> compte se referme exactement : **1974 cas sélectionnés − 1 skip = 1973 passés**,
> et les `+6` correspondent aux 6 tests ajoutés par le correctif (5 `test_a8_*`
> + 1 test de restauration en streaming). Le test d'axe renommé, lui, ne s'ajoute
> pas : il **remplace** l'ancien `xfail` et passe donc de « non passé » à
> « passé » — d'où un total qui monte de 6 et non de 7.

> **Note sur le bench, mesurée.** Un passage du gate a rapporté une « régression »
> de `sse_pump_us_per_chunk` (`4.673 → 6.544 ms`, +40 % > seuil 20 %). Vérification
> faite, c'est un **artefact de charge** : trois passages consécutifs au repos
> donnent `3.644 / 3.796 / 3.648 ms`, soit **plus rapide** que la baseline. Le
> passage fautif tournait en parallèle d'autres tests. Le bench mesure du
> temps-mur et n'est donc pas fiable sous charge concurrente — à savoir avant de
> conclure à une régression réelle. Le seuil de 20 % est du même ordre que la
> variance observée en environnement chargé.

Verdict final : **`GATE OK`**, code de sortie **0** (vérifié en l'évoquant
directement, hors tout pipeline de capture).

**Constat important sur la ligne de base** : le gate n'avait **jamais été vert**
avant cet audit. Au commit HEAD d'origine (`110d587`), `mypy` signalait déjà
**9 erreurs** (8 dans `app/protocol/mapping.py`, 1 dans `scripts/config_coverage.py`).
L'audit les a toutes corrigées, ce qui rend l'étape 2 franchissable pour la
première fois. Méthode de preuve : `git worktree add --detach <tmp> HEAD` sur un
arbre pristine, puis `mypy` — 9 erreurs reproduites indépendamment des
modifications en cours.

> **Note sur les `xfail` du gate.** Il n'en reste **aucun** (`0 xfailed`). Les 3
> `xfail` de P4-stream ont été **retirés** (A24 corrigée : ces tests sont devenus
> de simples tests de régression), et le dernier — l'**écart A8 résiduel**, nom
> d'outil > 64 caractères vers une cible Chat sur **P2** — a été **levé par le
> correctif du lot L4**.
>
> Ce marqueur était `strict=True` : il est passé en **XPASS** dès le câblage de la
> sanitize, c'est-à-dire en **échec**, ce qui a **forcé** la levée du marqueur au
> lieu de laisser le test pourrir. Le garde-fou a donc fonctionné exactement comme
> annoncé — c'est le seul point de ce rapport où une prédiction « le test passera
> au rouge le jour où… » a été vérifiée en vrai. La perte n'est plus ni déclarée ni
> simplement suivie : elle est **corrigée** et verrouillée (§7.2,
> `docs/conversion-matrix.md`).

---

## 9. Ce que cet audit ne prouve pas

Par honnêteté, les limites de la couverture :

1. **Le comportement de l'upstream réel n'était pas testé en live — il l'est
   désormais en partie, et une conclusion ferme en est sortie.** Tous les tests
   restent hermétiques (upstream simulé) : cela reste vrai. Mais la tentative de
   test réel, d'abord **bloquée au niveau du compte** (`CreditsError`,
   `MissingSessionID`, `DataPolicyError`), a fini par aboutir : **plusieurs modèles
   ont servi de vraies réponses** (`mimo-v2.5-free`, `glm-5.1`, `kimi-k2.6`,
   `muse-spark-1.2-contributor`), la cible étant **intermittente** — mêmes modèles :
   `200`, puis `503` quelques minutes plus tard. Ce qui est validé en réel est
   détaillé ci-dessous, avec ses témoins. En particulier, **la limite de 64
   caractères des cibles Chat a été tranchée par la mesure : cet amont ne rejette
   pas un nom > 64** (nom de 72 car. rendu identique, 3/3, sur un chemin sans
   conversion, sans sanitize et sans restauration). Le correctif A8 reste justifié
   comme **mise en conformité** — 64 est la limite du contrat OpenAI/Chat — mais sa
   prémisse « l'amont rejette » est **démentie** ici : ce volet n'est plus « déclaré
   comme risque », il est **tranché, dans le sens du non-rejet**. Ce qui n'est
   toujours **pas** prouvé en réel est plus étroit : la conformité des autres
   chemins d'écriture (effort, cache, documents) et le comportement des amonts
   Anthropic, qu'aucune cible joignable ne permet d'atteindre.
2. **La bascule `max_completion_tokens` (D2) est latente**, pas active : aucune
   route configurée ne cible aujourd'hui un modèle o-series (`gpt-5.6-luna` est
   remappé vers `muse-spark-1.3-contributor`). Le mécanisme est correct et
   testé, mais son déclenchement en production dépend de la configuration.
3. **Le cas « client envoyant deux formes de limite divergentes » sur
   `/v1/chat/completions` et sur un corps Responses natif** n'a pas été observé
   en conditions réelles ; la précédence est décidée et verrouillée, mais le
   comportement d'un upstream face à la coexistence des deux champs n'est pas
   confirmé.
4. **Les images : une dégradation réelle mais étroite, et deux chemins non
   verrouillés.** Le cas courant est **fidèle** — `source.type` `base64` ou `url` :
   octets transmis intacts, ni fetch d'URL, ni ré-encodage, ni redimensionnement
   (`mapping.py:1155-1171`). Le cas étroit est une **dégradation** : tout autre
   `source.type` (typiquement `file` avec `file_id`) devient le **texte**
   `[image:file]` (`mapping.py:1165-1169`), y compris dans un `tool_result`
   (`:1290-1292`), **sans que le client en soit informé** — le serveur le trace
   (`_debug`, `:1168`), le client non, ce qui est précisément la définition d'une
   perte silencieuse côté client. S'y ajoute un **trou latent** en direction
   réponse : les convertisseurs de réponse ne traitent ni les blocs `image`
   (`mapping.py:2281-2295`, `:2633-2668`) ni les deltas non textuels du flux
   (`opencode.py:12954-12968`) — non observé, faute de production d'image en sortie
   dans ce dépôt. Enfin **P1 et P3 n'ont aucun test d'image** : la transmission y
   est verbatim, donc une perte est très improbable, mais elle n'est **pas
   verrouillée** (décompte : P2 = 5, P4 = 1, P5 = 1, P6 = 2 tests portant réellement
   une image). La ligne « PERDU silencieusement » de `docs/conversion-matrix.md`,
   **supprimée par mégarde lors de la refonte de ce document** (commit `b6c6543`) et
   introuvable depuis, a été **rétablie sous une forme exacte** — et la même
   affirmation fausse, encore présente dans `docs/clients-compat.md`, a été corrigée.

### Tentative de test réel (résultat mesuré)

Le proxy en cours sur `127.0.0.1:4000` **est joignable** : de vraies requêtes y
ont été envoyées, et sur les modèles `muse-spark-1.3-contributor` et `mimo-v2.5`
(chemin P2), deux réponses **`200`** ont été obtenues. Ma formulation initiale
(« pas d'accès live ») était donc **exacte sur le fond mais fausse sur la
cause** : ce n'était pas la connectivité qui manquait.

**Les voies essayées et leurs réponses réelles :**

| Voie | Requête | Réponse de l'amont |
|---|---|---|
| Amont payé direct (`/zen/go/v1`, `glm-5`, `glm-5.1`, `kimi-k2.6`, `minimax-m2.5`) | nom court **et** nom de 80 car. | **`401 CreditsError`** — `Insufficient balance` |
| Amont free direct (`/zen/v1`, `mimo-v2.5-free`, `nemotron-3.5-lightning-free`, `ling-3.0-flash-fin-free`) | nom court | **`400 MissingSessionID`** — le free exige une **session** OpenCode (`OPENCODE_GO_AUTH_COOKIE`), pas une clé API |
| **Via le proxy `:4000`**, P2, `deepseek-v4-flash-free` | témoin court | `503` (free → 400, payant → 503) |
| **Via le proxy `:4000`**, P2, `muse-spark-1.3-contributor` / `mimo-v2.5` | **témoin court** | **`200`** ✅ — « Bonjour » |
| idem, sur ces deux modèles | nom de 80 car. | **`200`** ✅ |
| idem, avec `tool_choice` forcé | nom de 80 car. | `503` — cause réelle **`tool_choice`**, pas le nom : l'amont n'accepte que `"auto"` (mesuré ci-dessous), puis le repli payant se heurte au `403 DataPolicyError` d'opt-in workspace |

**Correction d'une erreur de lecture, à signaler.** J'ai d'abord interprété les
deux `200` comme « l'amont accepte un nom de 80 caractères ». **C'était faux.**
Le journal de debug et les dumps du proxy montrent que le nom réellement émis
ne faisait pas 80 caractères :

- **Sur la jambe free via l'API Responses**, le nom est **sanitizé à 64** :
  `a`×57 + `-86f336`. Digest recalculé à la main : `sha1("a"×80)[:6] == 86f336`
  — **identique au bit près** à la sortie de `sanitize_tool_names`. Les deux `200`
  portaient donc sur un nom **sanitizé à 64**, ce qui est parfaitement cohérent
  avec une limite de 64, et ne prouve **rien** sur l'acceptation d'un nom plus
  long.
- **Sur le chemin P2 vers Chat**, le nom partait **non sanitizé** — constat
  relevé **avant correction**. Le dump `logs/free400_msg_aa3ac6d9fd40-31e.json`
  (requête réellement émise) contient un outil nommé **80 caractères**, avec
  `additionalProperties` ajouté au schéma par `_normalize_tool_schema` : le proxy
  avait donc **traité** les outils sans toucher au nom. Depuis le correctif A8,
  cette même requête émet un nom **≤ 64**.

**Bilan honnête de ce que le live établit :**

1. ✅ **Établi — mesure historique, avant correction** : sur P2 (client Anthropic
   → amont Chat), le proxy **émettait un nom d'outil de 80 caractères, non
   sanitizé, vers un vrai amont**. C'est cette observation qui a fait passer la
   première moitié de A8 de « déduite hors ligne » à **observée sur le fil**. Ce
   point est depuis **clos** : le correctif applique la sanitize à l'aller, donc
   le proxy n'émet plus de nom > 64 vers Chat.
2. ✅ **Établi** : sur la jambe Responses, la sanitize à 64 **fonctionne en
   production** (digest vérifié). Cela valide en réel le comportement verrouillé
   hors ligne par `test_axis_long_name_is_sanitized_towards_chat_via_responses`.
3. ✅ **Établi depuis — et la réponse est NON** : cet amont **ne rejette pas** un nom
   > 64. Mesuré sur le seul chemin sans ambiguïté — un modèle free à endpoint
   non-`/responses` (`mimo-v2.5-free`), où il n'y a **ni conversion, ni sanitize, ni
   restauration** : un nom d'outil de **72 caractères** est envoyé, l'amont répond
   **`200`** et rend le nom **IDENTIQUE (72 car.), 3/3 rondes**, chacune avec son
   témoin à nom court vert. La prémisse de A8 (« l'amont rejette ») est donc
   **démentie sur cette cible**. Le correctif reste justifié comme **mise en
   conformité** au contrat OpenAI/Chat (64 caractères), **pas** comme réparation
   d'une panne reproduite. Détail en « Mesures A8 sur le fil » ci-dessous — y
   compris les mesures que la **première rédaction surévaluait**, et la lacune
   résiduelle découverte à cette occasion.

**Le témoin reste la règle.** Sur `deepseek-v4-flash-free`, le témoin à nom court
échouait déjà en `503` : dans ce cas la sonde **refuse de conclure** au lieu de
produire un « A8 non confirmé » trompeur. Même discipline que pour les mutations :
sans témoin vert, rien n'est imputable à la variable testée.

### Mesures A8 sur le fil (résultat mesuré)

**La question tranchée** : un amont rejette-t-il un nom d'outil > 64 caractères ?
**Réponse : non, pas celui-ci** — et c'est mesuré, non plus supposé.

**Le chemin qui permet de conclure.** La sanitize de la jambe free dépend de
l'**URL** de la jambe : `_is_responses = "/responses" in free_endpoint`
(`opencode.py:6183`). Les modèles **muse/spark** prennent un endpoint
`/responses`, où les trois branches convertissent et sanitizent
(`_sanitize_native_responses_request`, `_anthropic_to_responses_request`,
`_chat_to_responses_request` — ce dernier appelle `sanitize_tool_names`,
`mapping.py:3568`). Les **autres** modèles free prennent `free_base` =
`.../chat/completions` : la branche `else` (`opencode.py:6197-6199`) recopie le
corps, **passthrough nu** (cf. `tests/test_free_discovery.py:13`). Sur ce second
chemin il n'y a **ni conversion, ni sanitize, ni map de restauration** : le nom du
client part **verbatim** et celui de l'amont revient **verbatim**. C'est le seul
chemin où la tolérance de l'amont est observable sans ambiguïté.

| Mesure | Chemin | Nom d'outil | Résultat |
|---|---|---|---|
| **Tolérance amont** | `mimo-v2.5-free` (endpoint `chat/completions`, passthrough nu) | **72 car.** | **`200`, nom rendu IDENTIQUE (72 car.), 3/3 rondes** — témoin 11 car. vert dans chaque ronde |
| Corroboration | `deepseek-v4-flash-free`, idem | **80 car.** | `400`, mais le corps d'erreur est *« Model is unavailable »* : le refus **ne portait pas** sur le nom |
| Exclus (jambe morte) | `deepseek-v4-flash-free` → `503` ; `nemotron-3-ultra-free` → `MissingSessionID` | — | **non concluants**, écartés : sans témoin vert, rien n'est imputable |

**Une erreur de méthode, corrigée dans cette rédaction.** Un premier jet a présenté
« 9/9 en `200` sur le passthrough P3 » et « 24/24 noms identiques » comme la preuve
de la tolérance amont. C'était **trop large** : la plupart de ces essais passaient
par une jambe free muse/spark en `/responses`, donc **sanitizée à l'aller et
restaurée au retour** — le même piège que celui signalé plus haut (lire un `200`
comme l'acceptation d'un nom long). Ces mesures gardent leur valeur comme preuve que
**le proxy se comporte correctement** (la restauration rend bien le nom d'origine) ;
elles ne disent rien de la tolérance de l'amont. Seule la ligne `mimo-v2.5-free` en
parle, et elle est sans échappatoire.

**Découverte annexe — l'amont n'accepte que `tool_choice: "auto"`.** Toute autre
valeur est refusée par un `400` au message explicite : *« only `"auto"` is supported
for `tool_choice`. `"none"`, `"required"`, and named function choices are not
currently supported »*. Reproduction : **3/3 rondes, sur les deux protocoles**
(`/v1/messages` et `/v1/chat/completions`) — `auto` → `200`, `required` et la forme
nommée → échec, **avec un nom d'outil court comme avec un nom long** : la longueur
du nom n'est donc pas la variable. Le refus est retenté sur 5 stations, puis le
repli payant se heurte au `403 DataPolicyError` du compte, d'où un `503` composite
(`free=400` + `paid=403`) — confirmé par les colonnes `free_status`/`paid_status` de
`logs/requests.db`. **Ce n'est pas un défaut de conversion** (la traduction du proxy
est conforme), et le correctif A8 en est **disculpé par construction** : la branche
passthrough P3 n'appelle jamais `anthropic_to_openai`, seul site d'appel de
`_remap_chat_tool_choice` (`mapping.py:1567`), et `git diff b6c6543 HEAD -- app/protocol/mapping.py`
ne touche `tool_choice` que là.

**Lacune résiduelle déclarée.** Sur la jambe free à endpoint non-`/responses`, les
noms d'outil ne sont **pas** sanitizés (`opencode.py:6197-6199`) : un nom > 64 y part
tel quel. Même classe d'écart que A8, sur une autre branche, **sans effet observable
ici** puisque l'amont tolère. Déclaré comme écart de conformité, **pas** comme panne ;
toute correction éventuelle devrait réutiliser `sanitize_tool_names` plutôt que créer
une seconde règle.

---

## 10. Fichiers principaux

| Fichier | Rôle |
|---|---|
| `config/effort_policy.py` | source unique d'effort (`resolve_effort`, `MODEL_MAX`) |
| `config/effort_caps.py` | plafonds par modèle (config-driven) |
| `app/protocol/mapping.py` | conversions, `_set_output_token_limit`, `ResponsesStreamEmitter` |
| `opencode.py` | handlers, `ensure_min_tokens`, relais `store`/`truncation` |
| `scripts/gen_golden_fixtures.py` | générateur des 47 goldens |
| `tests/test_protocol_matrix.py` | matrice 6 chemins × 15 axes |
| `tests/test_effort_policy.py` | contrat d'effort, décision §7.1 |
| `tests/test_conversion_golden.py` | verrou du contrat V1 |
| `tests/test_docs_drift.py` | gate code ↔ doc bidirectionnel |
| `docs/conversion-matrix.md` | matrice documentaire (règle d'effort incluse) |
| `docs/_drift_manifest.json` | source machine-lisible du gate doc |

---

### 3.11 P4 en streaming, A27 et validation en réel (14/09/2026)

Suite directe de §3.8 et §3.9 : les deux points restés ouverts — le **0 octet** de la
jambe free de P4 en streaming, et l'absence de mesure sur un proxy vivant — sont traités.
Détail technique complet au plan §11.13 ; ici, les résultats et ce qu'ils enseignent.

#### Ce qui a été corrigé

1. **P4 streaming, 0 octet** — deux défauts, sur `/v1/chat/completions` avec un modèle
   payant à `protocol: anthropic` dont l'équivalent free parle Chat :
   * l'**aller** envoyait le corps Anthropic à un endpoint Chat (`system` et
     `input_schema` perdus) ;
   * le **retour** livrait du Chat à un parseur qui n'exploite que de l'Anthropic
     (`opencode.py:13026`) ⇒ aucune ligne exploitable, **200 + 0 octet**.
   Corrigé : corps client à l'aller, conversion `Chat SSE → Anthropic SSE` au retour,
   état frais par tentative.
2. **A27, nouveau défaut mesuré** — toutes les clés Anthropic en pause ⇒
   `_get_auth_headers` rend `None` **sans** lever ; la jambe free échoue ; le repli
   payant propage ce `None` ; `opencode.py:12647` faisait `None.get(...)` ⇒ **500**, là
   où le streaming rend un 503 propre. Corrigé par un garde 503. **Le motif jumeau dans
   le second handler n'est pas corrigé** (déclaré, plan §11.13.2).

#### Le test qui verrouillait le défaut

`test_free_model_subpath_p4_stream` stubait `ANTHRO_SSE_LINES` : une forme que l'endpoint
free **ne produit jamais**, et il n'assertait aucun contenu — il ne pouvait donc que
passer. Réécrit avec la forme réelle (`CHAT_CHUNK_LINES`) et des assertions de contenu, il
a immédiatement échoué en rendant `''`. C'est **la deuxième occurrence** de la classe A25
(§3.7) dans ce lot : mes propres tests ont verrouillé deux fois le bug qu'ils étaient
censés détecter. Le schéma est net — un stub qui reproduit fidèlement les hypothèses du
code buggé est un test complice, et seule une assertion de **contenu** le démasque.

#### Une erreur de ma part, du même genre que le défaut

En écrivant le correctif, j'ai appelé `.decode()` sur la sortie du convertisseur, qui rend
des `str`. L'`AttributeError` a été **avalée par le `except Exception` du handler** : le
symptôme redevenait exactement « 200 + 0 octet », indistinguable du défaut d'origine. Je
l'ai trouvé en isolant le convertisseur hors du serveur, pas en lisant le code. Leçon :
dans ce proxy, un défaut silencieux n'est pas seulement possible, c'est le comportement
par défaut de toute erreur de programmation dans un handler.

#### Validation en réel (proxy `:4000`)

Le proxy a été redémarré (il tournait depuis le 11/09 19:48 sur le code d'avant les
correctifs), et la même sonde rejouée avant/après :

| Chemin (`model: haiku`) | Avant | Après |
|---|---|---|
| P1 non-stream `/v1/messages` | **500** | 200 **ANTHROPIC** `content='Bonjour'` |
| P1 stream `/v1/messages` | 200, **16 007 o de Chat** non converti | 200 **ANTHROPIC** (`message_start` + `content_block_delta`) |
| P4 non-stream `/v1/chat/completions` | **500** (A27) | 200 `choices[0].message.content='bonjour'` |
| P4 stream `/v1/chat/completions` | 200, **0 octet** | 200, **7 559 o** de Chat, texte reçu |
| Témoin `opus` (`protocol: openai`) | 200 ANTHROPIC | 200 ANTHROPIC |

Le témoin est le point décisif : `opus` atteint **le même** modèle free et **le même**
endpoint, et n'a jamais été cassé. Les trois défauts venaient donc de la **déclaration de
protocole du modèle payant**, pas de la jambe free.

#### Ce qui n'est pas prouvé

* Une requête par chemin, un seul fournisseur free, des stations en 429 intermittents :
  c'est une confirmation, pas une couverture.
* Le jumeau d'A27 (second handler) : **corrigé depuis** (14 sites de dépouillement homogènes,
  garde 503 en défense en profondeur) — mais ses **témoins ne mordent pas**, ce qui est
  déclaré dans le tableau du §11 ci-dessous plutôt que maquillé.
* Les 13 trous ouverts du §3.9 : état par trou au **§11** ci-dessous (7 fermés, 1 réfuté,
  1 partiellement mesuré, 4 encore ouverts à cette date).

## 11. Les 13 trous déclarés — état vérifié au 15/09/2026

Chaque ligne dit ce qui a été **mesuré**, pas ce qui avait été supposé. Un trou n'est déclaré
fermé que lorsque son témoin **mord** (correctif neutralisé → le test rougit).

| # | Trou | Verdict | Preuve / réserve |
|---|---|---|---|
| 1 | jumeau d'A27 (500 au lieu de 503) | **FERMÉ — garde morte retirée, commentaire et tests rectifiés** | 14 sites homogènes ; sonde : le site fautif est atteint, mais ni le dépouillement seul, ni le garde seul, ni le retour **complet** au code d'origine ne font rougir les témoins — déclaré (plan §12.1) |
| 2 | A8 sur P3 — `sanitize_tool_names` jamais appelé | **fermé, et le constat était mal formulé** | l'aide « forme Responses » n'était pas câblable (le nom Chat vit sous `function.name`) : `_sanitize_chat_tools` et 2 autres aides étaient du **code mort**. 8 cas, **5 rouges** sans correctif, **5 mutations mordent** ; deux pièges : retour non-stream d'octets amont verbatim, stream qui réémettait la ligne brute |
| 3 | A8 sur P5 — `openai_chat_to_responses` sans `name_map` | **fermé, dans les deux sens mesurés** | l'aller P5 **raccourcit bien** (78 → ≤64 + map sur le fil) : c'était le retour qui ne restaurait pas. Symétrie posée sur les 2 convertisseurs, carte extraite et propagée aux 7 sites ; témoin P5 qui mord, **MUT-B ne mord pas** (point inerte tant que l'aller P6 ne sanitize pas) — déclaré. Trois écarts restent ouverts et mesurés : **P6 aller verbatim**, amont P5 en Responses natif, et **P5 streaming qui perd les `tool_calls` d'amont** (`output: []`) |
| 4 | P4 émet une signature HMAC **locale forgée** | **fermé** | `tests/test_p4_synthetic_thinking.py` ; mutations **2/2 mordent**, témoin P1 vert |
| 5 | garde orphelin de P6 | **fermé** | `tests/test_p6_orphan_and_thinking.py` ; mutation mord. Le **premier correctif était faux** (mauvais format) : c'est le témoin qui l'a démontré (plan §12.6) |
| 6 | chemin P2 → `/responses` absent de la matrice | **fermé** | trou de couverture confirmé (1 témoin vert) + **2 défauts neufs** : un 200 du free jeté comme un échec (SSE lu comme du JSON → 503 client ; **corrigé** — reste le cadrage SSE du repli, `xfail` déclaré) et une comptabilité de tokens à zéro (`prompt_tokens` lu sur une charge `/responses`, `input_tokens`) — **mesuré, non corrigé**, faute de témoin |
| 7 | axe « jambe free » | **fermé** | la conversion était déjà refaite avec le modèle free (profil, garde cache, borne) ; **seul l'effort** ne l'était pas — corrigé, témoin rouge sous mutation (`'max'` → `'high'`), limites déclarées (chemin Chat seul prouvé). **Faille latente trouvée en vérifiant la garantie « ne peut que rabaisser »** : `effort` et `output_config.effort` ont la priorité sur `reasoning_effort` et pouvaient donc **relever** le niveau — **mesuré** (`max` contre `high`), corrigé (la sonde ne porte plus que la décision payante) ; latente et non active avec la configuration actuelle, aucun couple n'ayant de plafond free supérieur au payant |
| 8 | garde `supports_cache_control` absente | **fermé** | témoin **par site** ; mutations **2/2 mordent** avec discrimination |
| 9 | A11 partiel — P5/P6 entièrement bufferisés | **ouvert, mesuré en live** | TTFB/total = **1,00** sur P6 (26,11 s), 0,86 sur P1 — mais les deux passent par la jambe free : la mesure dit ce que subit le client, pas la différence payant/free. Non corrigé : exige un convertisseur SSE→SSE Responses inexistant dans le dépôt |
| 10 | `cache_control` → `prompt_cache_breakpoint` no-op | **fermé (documentaire)** | décision tranchée, docstring fausse corrigée, comportement verrouillé ; `cache_write_tokens` reste rattaché à L15 |
| 11 | `thinking` top-level jamais copié par P5 | **réfuté par la mesure** | le `thinking` racine **est** converti et le budget **pilote** l'effort ; 8 cas de verrouillage |
| 12 | zéro golden P3 | **fermé** | 48ᵉ golden ; 47/47 goldens préexistants inchangés (sha256) ; mutations **3/3 mordent** |
| 13 | aucun test de la jambe free de P6 | **fermé** | 3 tests ; 4 mutations en **worktree jetable**, dont **une qui ne mord pas** et qui est déclarée |

**Deux défauts neufs, trouvés par ces travaux** (ils n'étaient pas dans la liste) :

* **P6-stream : 503 au lieu de la jambe free** — clés payantes en pause, `stream: true`
  renvoyait « free model will be tried on next attempt » avec **zéro** appel amont, alors que
  le non-stream emprunte la jambe free. Corrigé ; témoin qui mord (plan §12.9).
* **`stop` / `stop_sequences` / `stream_options` perdus vers `/responses`** — mesuré et figé
  par le golden P3. Non corrigé : la cible n'a peut-être pas d'équivalent `stop`, et inventer
  un champ au risque d'un 400 est exactement l'erreur évitée ailleurs. **À trancher contre la
  spec**, puis corrigé ou inscrit comme perte résiduelle **tracée** (plan §12.7).

**Ce que ces corrections ne prouvent pas** : les niveaux de preuve restent hétérogènes — tests
hermétiques qui mordent (4, 5, 8, 12, 13, + le défaut P6-stream), mesure live (A27), lecture
de code étayée sans témoin (7), et constat figé sans correctif (`stop`). Le détail par trou est
dans le plan, §12.

---

## Verdicts finaux — deux refus argumentés (trous 1 et 9)

Après plusieurs tentatives, les trous 1 et 9 se concluent par un **refus argumenté** plutôt que
par une clôture de complaisance. Le détail des mesures est au plan §12.17.

| Trou | Verdict | Preuve | Conséquence |
|---|---|---|---|
| **1** — jumeau d'A27 | **FERMÉ** (tours 31-32) — branche 503 inatteignable **retirée**, commentaire de production et docstrings de test rectifiés ; preuve du dépouillement en `_` (L12755) + ternaire atteignable | témoin mordant **impossible** (défaut non atteignable) : déclaré, plutôt que coché | restaurer le défaut aux 14 sites ne casse **aucun** test (78 passed / 1 xfailed, identique au pristine) ; sonde : `a_headers` est **toujours un `dict`** à la garde, dont les deux conditions sont **mutuellement exclusives** ; le témoin existant est **complice** (il passe sous toutes les mutations) | l'exigence « correction = test qui échoue sans elle » reste **insatisfaite** ; remédiation identifiée, côté correctif |
| **9** — P5/P6 bufferisés | **OUVERT**, correctif borné **impossible** | le correctif écrit puis mesuré laisse **TTFB/total = 1,00** ; `StreamingResponse` n'est construit qu'à t = 625 ms, **après** la libération de l'amont à 610 ms ; le témoin causal **échoue avec le correctif** | refus justifié : un ping émis en fin de génération remettrait à zéro le watchdog TTFB et **masquerait** les blocages |

**Défaut neuf mesuré** : `resp.json()` appelé **sans `await`** dans le handler `/v1/responses` —
exécution synchrone sur la boucle d'événements, gel constaté dans un harnais ASGI.

**Ce que ces refus changent au bilan.** Douze des treize trous sont fermés ou réfutés par la
mesure ; les trous 1 et 9 restent ouverts, avec leur cause racine mesurée, leur périmètre chiffré
et la raison pour laquelle un correctif de surface aurait été pire que rien. Aucun faux témoin
n'a été écrit, aucune mutation n'a été mise en scène.
---

## Reste a faire (liste de reprise)

Deux trous sur treize restent ouverts. Ni l'un ni l'autre n'est un travail de test qui aurait ete
omis : les deux sont bornes, mesures, et bloques sur une decision ou un chantier.

**1. A27 - RESOLU (tours 31-32).** La garde inatteignable a ete retiree, le commentaire de production et les trois docstrings de test ont ete rectifies, et la suppression est prouvee sans effet de bord (condition toujours fausse). Reste vrai : aucun temoin mordant n'est possible, le defaut d'origine n'etant plus atteignable. Detail : plan 12.17.
Le defaut d'origine est corrige a la racine. La garde `if a_headers is None and ...` ne peut pas
se declencher : sur les 14 affectations a `a_headers`, aucune n'assigne `None`, et
`_get_auth_headers` ne rend que des `dict`. Deux issues, au choix du proprietaire :
retirer la garde morte, ou la remplacer par une assertion d'invariant sur `_get_auth_headers`.
Tant que ce choix n'est pas fait, aucun temoin ne peut mordre : il faudrait reintroduire
artificiellement l'etat interdit. Details et mesures : plan 12.1 et 12.17.

**2. A11 - P5/P6 restent entierement bufferises (chantier).**
Mesure live : TTFB/total = 1,00. Le correctif borne (ping initial) a ete ecrit, mesure, puis
rejete : le `StreamingResponse` n'est construit qu'a t = 625 ms, apres la liberation de l'amont a
610 ms, donc la reponse n'existe pas encore au moment ou il faudrait emettre. Pire, un ping tardif
remettrait a zero le watchdog TTFB et les timeouts idle, masquant les blocages au lieu de les
revele.
Un vrai correctif exige de rendre le handler paresseux : construire la reponse et ses en-tetes
avant l'appel amont, puis deplacer collecte, conversion et journalisation dans un generateur - en
assumant que les branches d'erreur ne pourront plus renvoyer de 4xx/5xx propres une fois les
en-tetes partis. P6 exige en plus de corriger `resp.json()` appele sans `await` (execution
synchrone sur la boucle d'evenements, gel constate). Details : plan 12.9 et 12.17.

**Ce qui a ete ecarte volontairement** : aucun faux temoin, aucune mutation mise en scene, aucun
correctif de surface. La ou l'exigence « une correction = un test qui echoue sans elle » n'avait
plus d'objet mesurable, elle est declaree insatisfaite plutot que cochee.


#
#
 
A
d
d
e
n
d
u
m
 
—
 
A
1
1
,
 
d
é
f
a
u
t
 
b
o
r
n
é
 
c
o
r
r
i
g
é
 
e
t
 
p
r
o
u
v
é
 
(
t
o
u
r
s
 
3
3
-
4
0
)




|
 
É
l
é
m
e
n
t
 
|
 
S
t
a
t
u
t
 
|
 
P
r
e
u
v
e
 
|


|
-
-
-
|
-
-
-
|
-
-
-
|


|
 
P
a
r
s
e
 
J
S
O
N
 
s
y
n
c
h
r
o
n
e
 
d
u
 
c
o
r
p
s
 
a
m
o
n
t
 
s
u
r
 
l
a
 
b
o
u
c
l
e
 
d
'
é
v
é
n
e
m
e
n
t
s
 
(
`
/
v
1
/
r
e
s
p
o
n
s
e
s
`
,
 
L
1
4
0
1
4
)
 
|
 
*
*
F
E
R
M
É
*
*
 
|
 
t
é
m
o
i
n
 
c
a
u
s
a
l
 
:
 
V
E
R
T
,
 
R
O
U
G
E
 
s
o
u
s
 
m
u
t
a
t
i
o
n
 
(
`
a
s
s
e
r
t
 
0
 
>
 
0
`
,
 
m
e
s
s
a
g
e
 
d
e
 
g
e
l
)
,
 
r
e
s
t
a
u
r
a
t
i
o
n
 
b
y
t
e
-
e
x
a
c
t
e
,
 
r
e
-
V
E
R
T
 
;
 
`
B
I
T
E
 
:
 
T
r
u
e
`
 
|


|
 
C
o
n
t
r
a
t
 
p
r
é
s
e
r
v
é
 
(
m
ê
m
e
 
J
S
O
N
,
 
m
ê
m
e
s
 
e
x
c
e
p
t
i
o
n
s
,
 
m
ê
m
e
 
5
0
2
,
 
`
{
}
`
 
s
i
 
c
o
n
t
e
n
t
-
t
y
p
e
 
n
o
n
 
J
S
O
N
)
 
|
 
*
*
T
E
S
T
É
*
*
 
|
 
t
e
s
t
 
d
é
d
i
é
,
 
p
a
r
s
e
u
r
 
q
u
i
 
l
è
v
e
 
s
'
i
l
 
e
s
t
 
a
p
p
e
l
é
 
s
u
r
 
u
n
 
c
o
n
t
e
n
t
-
t
y
p
e
 
n
o
n
 
J
S
O
N
 
|


|
 
L
e
s
 
2
6
 
t
e
s
t
s
 
c
a
s
s
é
s
 
p
a
r
 
l
a
 
p
r
e
m
i
è
r
e
 
t
e
n
t
a
t
i
v
e
 
|
 
*
*
R
E
P
A
S
S
E
N
T
*
*
 
|
 
4
7
 
p
a
s
s
é
s
,
 
1
 
x
f
a
i
l
e
d
 
s
u
r
 
l
e
s
 
z
o
n
e
s
 
t
o
u
c
h
é
e
s
,
 
a
p
r
è
s
 
d
é
p
l
a
c
e
m
e
n
t
 
d
e
 
l
'
a
i
d
e
 
e
n
 
f
i
n
 
d
e
 
m
o
d
u
l
e
 
|


|
 
P
5
/
P
6
 
b
u
f
f
e
r
i
s
é
s
 
:
 
T
T
F
B
 
=
 
g
é
n
é
r
a
t
i
o
n
 
t
o
t
a
l
e
 
|
 
*
*
O
U
V
E
R
T
*
*
 
|
 
h
o
r
s
 
p
é
r
i
m
è
t
r
e
 
:
 
e
x
i
g
e
 
u
n
 
h
a
n
d
l
e
r
 
p
a
r
e
s
s
e
u
x
 
e
t
 
u
n
e
 
r
e
f
o
n
t
e
 
d
e
s
 
b
r
a
n
c
h
e
s
 
d
'
e
r
r
e
u
r
 
|


|
 
`
r
e
s
p
.
j
s
o
n
(
)
`
 
a
p
p
e
l
é
 
s
a
n
s
 
`
a
w
a
i
t
`
 
(
m
ê
m
e
 
h
a
n
d
l
e
r
)
 
|
 
*
*
S
I
G
N
A
L
É
,
 
N
O
N
 
C
O
R
R
I
G
É
*
*
 
|
 
r
e
l
e
v
é
 
d
e
 
l
e
c
t
u
r
e
,
 
n
o
n
 
m
e
s
u
r
é
 
|


|
 
P
a
r
s
e
 
s
y
n
c
h
r
o
n
e
 
a
u
x
 
t
r
o
i
s
 
s
i
t
e
s
 
j
u
m
e
a
u
x
 
(
L
6
8
0
0
,
 
L
1
0
2
8
4
,
 
L
1
2
8
8
3
)
 
|
 
*
*
S
I
G
N
A
L
É
,
 
N
O
N
 
C
O
R
R
I
G
É
*
*
 
|
 
c
o
r
p
s
 
n
o
n
-
s
t
r
e
a
m
é
s
,
 
t
a
i
l
l
e
 
n
o
n
 
m
e
s
u
r
é
e
 
:
 
j
e
 
n
e
 
c
o
r
r
i
g
e
 
p
a
s
 
s
a
n
s
 
m
e
s
u
r
e
 
|




*
*
A
v
e
u
 
d
e
 
d
i
a
g
n
o
s
t
i
c
,
 
c
o
n
s
i
g
n
é
 
p
l
u
t
ô
t
 
q
u
e
 
m
a
s
q
u
é
 
:
*
*
 
m
a
 
p
r
e
m
i
è
r
e
 
t
e
n
t
a
t
i
v
e
 
d
e
 
c
o
r
r
e
c
t
i
o
n
 
a
v
a
i
t
 
c
a
s
s
é
 
2
6


t
e
s
t
s
,
 
e
t
 
m
o
n
 
h
y
p
o
t
h
è
s
e
 
p
u
b
l
i
q
u
e
 
(
«
 
l
e
 
d
o
u
b
l
e
 
d
e
 
t
e
s
t
 
n
e
 
s
u
r
v
i
t
 
p
a
s
 
a
u
 
t
h
r
e
a
d
 
»
)
 
é
t
a
i
t
 
*
*
f
a
u
s
s
e
*
*
.
 
L
e


m
e
s
s
a
g
e
 
d
'
é
c
h
e
c
 
é
t
a
i
t
 
`
a
s
s
e
r
t
 
4
2
2
 
=
=
 
2
0
0
`
 
:
 
m
o
n
 
s
c
r
i
p
t
 
a
v
a
i
t
 
i
n
s
é
r
é
 
l
'
a
i
d
e
 
e
n
t
r
e
 
l
e
 
d
é
c
o
r
a
t
e
u
r


`
@
a
p
p
.
p
o
s
t
(
"
/
v
1
/
r
e
s
p
o
n
s
e
s
"
)
`
 
e
t
 
s
a
 
f
o
n
c
t
i
o
n
,
 
q
u
i
 
p
e
r
d
a
i
t
 
s
a
 
r
o
u
t
e
.
 
L
e
 
c
o
r
r
e
c
t
i
f
 
é
t
a
i
t
 
s
a
i
n
,
 
m
o
n


i
n
s
e
r
t
i
o
n
 
n
e
 
l
'
é
t
a
i
t
 
p
a
s
.




*
*
S
u
r
 
l
a
 
g
a
t
e
 
:
*
*
 
u
n
e
 
p
r
e
m
i
è
r
e
 
e
x
é
c
u
t
i
o
n
 
e
s
t
 
t
o
m
b
é
e
 
à
 
l
'
é
t
a
p
e
 
b
e
n
c
h
 
(
`
e
x
i
t
 
2
`
)
 
;
 
l
'
i
n
v
o
c
a
t
i
o
n
 
e
x
a
c
t
e
 
d
e


l
a
 
g
a
t
e
,
 
r
e
j
o
u
é
e
 
t
r
o
i
s
 
f
o
i
s
 
d
o
n
t
 
d
e
u
x
 
c
o
n
s
é
c
u
t
i
v
e
s
,
 
s
o
r
t
 
e
n
 
`
e
x
i
t
 
0
`
 
a
v
e
c
 
t
o
u
t
e
s
 
l
e
s
 
m
é
t
r
i
q
u
e
s
 
d
a
n
s


l
e
u
r
s
 
b
u
d
g
e
t
s
.
 
L
a
 
g
a
t
e
 
c
o
m
p
l
è
t
e
 
a
 
é
t
é
 
r
e
l
a
n
c
é
e
 
s
u
r
 
l
'
é
t
a
t
 
c
o
m
m
i
t
t
é
 
p
o
u
r
 
t
r
a
n
c
h
e
r
,
 
p
l
u
t
ô
t
 
q
u
e
 
d
e


d
é
c
l
a
r
e
r
 
v
e
r
t
 
s
u
r
 
u
n
e
 
n
o
n
-
r
e
p
r
o
d
u
c
t
i
o
n
.


#
#
#
 
V
e
r
d
i
c
t
 
d
e
 
g
a
t
e
 
s
u
r
 
l
'
e
t
a
t
 
c
o
m
m
i
t
t
e
 
(
t
o
u
r
 
4
1
)




L
a
 
g
a
t
e
 
a
 
e
t
e
 
m
e
s
u
r
e
e
 
s
u
r
 
l
e
 
c
o
m
m
i
t
 
`
0
6
d
c
f
b
e
`
 
d
a
n
s
 
u
n
 
*
*
w
o
r
k
t
r
e
e
 
d
e
t
a
c
h
e
 
p
r
o
p
r
e
,
 
h
o
r
s
 
d
u
 
d
e
p
o
t
*
*


(
`
C
:
\
U
s
e
r
s
\
j
u
l
i
e
\
D
o
w
n
l
o
a
d
s
\
o
p
e
n
c
o
d
e
-
a
1
1
-
g
a
t
e
`
)
,
 
p
a
r
c
e
 
q
u
'
u
n
e
 
*
*
a
u
t
r
e
 
s
e
s
s
i
o
n
 
t
r
a
v
a
i
l
l
a
i
t
 
e
n
 
p
a
r
a
l
l
e
l
e


d
a
n
s
 
l
'
a
r
b
r
e
 
p
r
i
n
c
i
p
a
l
*
*
 
:
 
y
 
m
e
s
u
r
e
r
 
a
u
r
a
i
t
 
m
e
l
a
n
g
e
 
d
e
u
x
 
c
o
d
e
b
a
s
e
s
 
e
t
 
l
e
 
v
e
r
d
i
c
t
 
n
'
a
u
r
a
i
t
 
r
i
e
n
 
p
r
o
u
v
e
.




R
e
s
u
l
t
a
t
,
 
e
t
a
p
e
 
p
a
r
 
e
t
a
p
e
 
:
 
r
u
f
f
 
O
K
 
;
 
m
y
p
y
 
O
K
 
;
 
p
y
t
e
s
t
 
*
*
p
a
s
s
e
*
*
 
(
2
0
7
8
 
t
e
s
t
s
,
 
2
3
 
d
e
s
e
l
e
c
t
i
o
n
n
e
s
,
 
1
 
s
k
i
p
,


1
 
x
f
a
i
l
,
 
c
o
u
v
e
r
t
u
r
e
 
6
1
,
4
9
 
%
 
p
o
u
r
 
u
n
 
s
e
u
i
l
 
d
e
 
4
5
 
%
)
 
;
 
b
e
n
c
h
 
*
*
O
K
*
*
 
(
t
o
u
t
e
s
 
m
e
t
r
i
q
u
e
s
 
d
a
n
s
 
l
e
s
 
b
u
d
g
e
t
s
)
 
;


p
i
p
-
a
u
d
i
t
 
*
*
O
K
*
*
 
(
a
u
c
u
n
e
 
v
u
l
n
e
r
a
b
i
l
i
t
e
 
c
o
n
n
u
e
)
 
;
 
g
i
t
l
e
a
k
s
 
i
g
n
o
r
e
 
(
b
i
n
a
i
r
e
 
a
b
s
e
n
t
)
 
;
 
`
d
o
c
k
e
r
 
c
o
m
p
o
s
e
 
c
o
n
f
i
g


-
-
q
u
i
e
t
`
 
*
*
O
K
*
*
.




D
e
u
x
 
e
c
h
e
c
s
 
s
o
n
t
 
a
p
p
a
r
u
s
 
a
u
 
p
r
e
m
i
e
r
 
p
a
s
s
a
g
e
,
 
t
o
u
s
 
d
e
u
x
 
*
*
a
r
t
e
f
a
c
t
s
 
d
e
 
l
'
i
s
o
l
e
m
e
n
t
,
 
p
a
s
 
d
e
f
a
u
t
s
 
d
u
 
c
o
d
e
*
*
,


e
t
 
c
h
a
c
u
n
 
d
e
m
o
n
t
r
e
 
p
a
r
 
s
o
n
 
p
r
o
p
r
e
 
m
e
s
s
a
g
e
 
d
'
e
r
r
e
u
r
 
:




-
 
`
t
e
s
t
s
/
t
e
s
t
_
t
o
o
l
_
s
c
h
e
m
a
_
s
t
r
i
c
t
.
p
y
`
 
(
2
 
t
e
s
t
s
)
 
:
 
`
d
u
m
p
 
l
o
g
s
/
f
r
e
e
4
0
0
_
m
s
g
_
.
.
.
j
s
o
n
 
i
n
t
r
o
u
v
a
b
l
e
`
 
-
-
 
c
e
s
 
t
e
s
t
s


 
 
l
i
s
e
n
t
 
u
n
 
f
i
c
h
i
e
r
 
d
e
 
d
o
n
n
e
e
s
 
q
u
e
 
l
e
 
d
e
p
o
t
 
c
o
n
t
i
e
n
t
 
e
t
 
q
u
e
 
l
e
 
w
o
r
k
t
r
e
e
 
n
e
u
f
 
n
'
a
v
a
i
t
 
p
a
s
.
 
R
e
p
r
o
d
u
i
t
s
 
a


 
 
l
'
i
d
e
n
t
i
q
u
e
 
s
e
u
l
s
,
 
a
v
e
c
 
m
o
n
 
f
i
c
h
i
e
r
 
d
e
 
t
e
s
t
 
c
o
l
l
e
c
t
e
 
a
v
a
n
t
,
 
p
u
i
s
 
a
p
r
e
s
 
:
 
n
i
 
A
1
1
,
 
n
i
 
u
n
 
e
f
f
e
t
 
d
'
o
r
d
r
e
.


 
 
R
e
s
o
l
u
s
 
e
n
 
r
e
l
i
a
n
t
 
`
l
o
g
s
/
`
 
p
a
r
 
*
*
j
o
n
c
t
i
o
n
*
*
 
(
a
u
c
u
n
e
 
c
o
p
i
e
 
d
e
s
 
3
 
G
o
)
 
:
 
p
y
t
e
s
t
 
p
a
s
s
e
 
a
l
o
r
s
 
e
n
t
i
e
r
e
m
e
n
t
.


-
 
`
c
o
m
p
o
s
e
`
 
:
 
`
e
n
v
 
f
i
l
e
 
.
.
.
c
r
e
d
e
n
t
i
a
l
s
.
e
n
v
 
n
o
t
 
f
o
u
n
d
`
 
-
-
 
f
i
c
h
i
e
r
 
n
o
n
 
s
u
i
v
i
 
p
a
r
 
g
i
t
,
 
d
o
n
c
 
a
b
s
e
n
t
 
d
u


 
 
w
o
r
k
t
r
e
e
.
 
C
o
p
i
e
,
 
p
u
i
s
 
`
d
o
c
k
e
r
 
c
o
m
p
o
s
e
 
c
o
n
f
i
g
 
-
-
q
u
i
e
t
`
 
s
o
r
t
 
e
n
 
*
*
e
x
i
t
 
0
*
*
.




*
*
C
e
 
q
u
e
 
c
e
 
v
e
r
d
i
c
t
 
e
t
a
b
l
i
t
 
:
*
*
 
l
e
 
c
o
m
m
i
t
 
`
0
6
d
c
f
b
e
`
 
p
a
s
s
e
 
l
a
 
g
a
t
e
 
c
o
m
p
l
e
t
e
,
 
i
n
d
e
p
e
n
d
a
m
m
e
n
t
 
d
e
 
c
e
 
q
u
e
 
f
a
i
t


l
a
 
s
e
s
s
i
o
n
 
p
a
r
a
l
l
e
l
e
.
 
*
*
C
e
 
q
u
'
i
l
 
n
'
e
t
a
b
l
i
t
 
p
a
s
 
:
*
*
 
q
u
e
 
l
'
a
r
b
r
e
 
d
e
 
t
r
a
v
a
i
l
 
p
r
i
n
c
i
p
a
l
 
e
s
t
 
s
a
i
n
 
-
-
 
i
l
 
c
o
n
t
i
e
n
t


l
e
s
 
m
o
d
i
f
i
c
a
t
i
o
n
s
 
e
n
 
c
o
u
r
s
 
d
e
 
c
e
t
t
e
 
a
u
t
r
e
 
s
e
s
s
i
o
n
,
 
q
u
i
 
n
'
o
n
t
 
e
t
e
 
n
i
 
t
o
u
c
h
e
e
s
,
 
n
i
 
c
o
m
m
i
t
e
e
s
,
 
n
i
 
a
n
n
u
l
e
e
s
.

