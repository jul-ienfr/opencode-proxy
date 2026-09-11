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

- **11 → 46 fixtures** (`docs/v1-response-golden/`), couvrant les 15 axes ×
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
| `test_conversion_golden.py` | 2 | verrou du contrat V1 (paramétré sur 46 fixtures) |
| `test_e2e_protocol_matrix.py` | 27 | **bout en bout** : handler → corps amont, 6 chemins + sous-chemins free/failover (trouve **A24** et **A25**) + 5 verrous L13 « reasoning_content » + la restauration A8 en streaming — 27 fonctions → **35 cas collectés** |
| `test_tools_matrix.py` | 13 | matrice outillage 6 chemins : `tools[]`/`tool_choice`/`strict`/nom long/`input_schema` invalide (**L4** ; 7 tests couvrent A8) — 13 fonctions → **30 cas** |
| **Total** | **302** | 17 fichiers (comptage **mesuré** : `^def test_` / `^async def test_` par fichier) |

> `test_conversion_golden.py` ne compte que 2 fonctions mais **46 cas** via
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

1. **Le comportement de l'upstream réel n'est pas testé en live.** Tous les
   tests sont hermétiques (upstream simulé). **Une tentative de test réel a
   été faite** (voir ci-dessous) et elle est **bloquée au niveau du compte**,
   pas de la connectivité : le proxy en cours sur `127.0.0.1:4000` est
   joignable et répond, mais aucun amont n'accepte la requête. Donc **aucune
   cible réelle n'a pu être validée**. **C'est en particulier le cas de la
   limite de 64 caractères des cibles Chat** : on ne peut pas établir depuis ce
   dépôt qu'un amont **rejette** effectivement un nom d'outil > 64 caractères. Le
   correctif A8 applique donc la limite **par construction** — le proxy n'émet
   plus aucun nom > 64 vers une cible Chat, et restitue le nom d'origine au
   client — sans avoir reproduit le rejet en réel. Ce volet reste **déclaré comme
   risque**, pas comme panne observée, et la tentative ci-dessous montre qu'il ne
   peut pas être tranché sans accès amont.
2. **La bascule `max_completion_tokens` (D2) est latente**, pas active : aucune
   route configurée ne cible aujourd'hui un modèle o-series (`gpt-5.6-luna` est
   remappé vers `muse-spark-1.3-contributor`). Le mécanisme est correct et
   testé, mais son déclenchement en production dépend de la configuration.
3. **Le cas « client envoyant deux formes de limite divergentes » sur
   `/v1/chat/completions` et sur un corps Responses natif** n'a pas été observé
   en conditions réelles ; la précédence est décidée et verrouillée, mais le
   comportement d'un upstream face à la coexistence des deux champs n'est pas
   confirmé.
4. **Les documents et images** sont couverts au niveau de la conversion, mais
   pas d'un bout à l'autre avec un upstream réel renvoyant un contenu analysé.

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
| idem, avec `tool_choice` forcé | nom de 80 car. | `503` — `DataPolicyError (403)` : opt-in data du workspace requis |

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
3. ❌ **Non établi** : qu'un amont **rejette** effectivement un nom > 64. Tous les
   échecs observés ont une autre cause (session free manquante, `DataPolicyError`
   d'opt-in workspace, solde à zéro). La cible est en outre **intermittente**
   (mêmes modèles : `200` puis `503`), donc un échec isolé ne serait de toute
   façon pas imputable au nom. Cette question porte sur la **nécessité** de la
   limite, pas sur le correctif A8 : celui-ci applique la limite **par
   construction**, indépendamment de ce que ferait l'amont.

**Le témoin reste la règle.** Sur `deepseek-v4-flash-free`, le témoin à nom court
échouait déjà en `503` : dans ce cas la sonde **refuse de conclure** au lieu de
produire un « A8 non confirmé » trompeur. Même discipline que pour les mutations :
sans témoin vert, rien n'est imputable à la variable testée.

---

## 10. Fichiers principaux

| Fichier | Rôle |
|---|---|
| `config/effort_policy.py` | source unique d'effort (`resolve_effort`, `MODEL_MAX`) |
| `config/effort_caps.py` | plafonds par modèle (config-driven) |
| `app/protocol/mapping.py` | conversions, `_set_output_token_limit`, `ResponsesStreamEmitter` |
| `opencode.py` | handlers, `ensure_min_tokens`, relais `store`/`truncation` |
| `scripts/gen_golden_fixtures.py` | générateur des 46 goldens |
| `tests/test_protocol_matrix.py` | matrice 6 chemins × 15 axes |
| `tests/test_effort_policy.py` | contrat d'effort, décision §7.1 |
| `tests/test_conversion_golden.py` | verrou du contrat V1 |
| `tests/test_docs_drift.py` | gate code ↔ doc bidirectionnel |
| `docs/conversion-matrix.md` | matrice documentaire (règle d'effort incluse) |
| `docs/_drift_manifest.json` | source machine-lisible du gate doc |
