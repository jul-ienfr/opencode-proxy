# PLAN — Audit & complétion des conversions de protocole (effort · contexte · raisonnement · cache · tools · documents)

Date : 2026-09-10. Statut : **exécuté** — les 16 lots (L0→L16) sont traités ; l'état
final, les écarts actés et les points encore ouverts sont en §11.9/§11.10. Le
correctif **A8** a été livré après coup par le lot L4 (voir §11.9).
Périmètre : `opencode.py` (13 225 l., 213 defs), `app/protocol/mapping.py` (3 398 l.), `protocol_mapping.py` (shim ADR-006), `config/effort_caps.py`, `protocol/tokens.py`, `streaming/sse.py`, `docs/conversion-matrix.md`, `docs/v1-response-golden/`, `scripts/gen_golden_fixtures.py`, `tests/`.

Règle du plan : **aucun correctif avant la fin du Lot 1** (matrice de vérité). On prouve d'abord, on patche ensuite.

---

## 1. Cartographie vérifiée — 6 chemins réels

Deux protocoles amont seulement (`config/settings.py:554` `KNOWN_PROTOCOLS`, `config.yaml` : `protocol: openai|anthropic`). Trois portes d'entrée → **6 chemins**.

| # | Entrée | Amont | Requête : convertisseur | Réponse (non-stream) | Réponse (stream) |
|---|---|---|---|---|---|
| P1 | `POST /v1/messages` *(opencode.py:8713)* | anthropic | passthrough + `strip_synthetic_thinking` (8819) | passthrough | passthrough (`anthropic_stream` 9084) |
| P2 | `POST /v1/messages` | openai | `anthropic_to_openai` (mapping.py:711/1239) | `openai_to_anthropic` (1381) | `openai_stream` (11615) |
| P3 | `POST /v1/chat/completions` *(11161)* | openai | passthrough + guards (11269) + effort local (11224) | passthrough | passthrough |
| P4 | `POST /v1/chat/completions` | anthropic | `openai_to_anthropic_request` (1442) | `anthropic_to_openai_response` (1801) | `_anthro_to_oai_stream` (12556) |
| P5 | `POST /v1/responses` *(13154)* | openai | `_sanitize_native_responses_request` / `_chat_to_responses_request` | `openai_chat_to_responses` (13657) | **1 seul `response.completed`** (13658) |
| P6 | `POST /v1/responses` | anthropic | `openai_responses_to_anthropic` (1972) | `anthropic_to_openai_responses` (13482) | **1 seul `response.completed`** (13588) |

Sous-chemins par chemin : non-stream, stream, **free-model swap** (`_try_free_model_first`, 6 sites d'émission `response.completed` : 13302, 13588, 13658, 13949, 14063 + `err_stream` 13548/13886/13984), bascule de clé, erreur amont.

Goldens actuels (`docs/v1-response-golden/`, 11 fichiers) : P2 (3 fns), P4 (2), P6 (1 + 1 SSE). **P1, P3, P5 : zéro golden.** Aucun golden documents / `cache_control` / effort / streaming chat↔anthropic.

---

## 2. Anomalies établies (à confirmer par test au Lot 1 — pas encore des correctifs)

### Effort / raisonnement

- **A1 — 4 mappings divergents pour la même notion d'effort.** `_effort_to_reasoning` + `clamp_effort` (mapping.py:251-285, P2) ; mapping Responses (2129-2162, P6) ; `openai_to_anthropic_request` **court-circuite `_effort_to_reasoning`** avec un dict codé en dur `{low:4096, medium:10000, high:16000}` (1793-1796, P4) ; 4ᵉ mapping famille-par-famille dans le handler chat (opencode.py:11224-11264, P3 : `glm-5*`, `deepseek-v4*`, sinon) **sans plafond modèle**. Un même `effort: high` ne produit pas la même chose selon la porte.
- **A2 — `xhigh`/`max` écrasés, `minimal` supprimé.** P4 : tout niveau hors dict → 16000 (mapping.py:1795) ; `minimal` est explicitement filtré (1794) alors que c'est une valeur OpenAI légitime.
- **A3 — P6 perd l'effort vers l'amont Anthropic.** `openai_responses_to_anthropic` écrit `result["reasoning_effort"]` (2132) puis **`return` immédiat** (2134) : aucune clé `thinking`. Or P6 est un passthrough Anthropic (opencode.py:13220 → 13247) : le corps part avec un champ non-Anthropic, sans budget. Effort probablement ignoré en amont.
- **A4 — plancher `max_tokens` absent sur P3.** `ensure_min_tokens` (8635, `thinking.default_min_tokens` + `THINKING_MODELS`) appelé en 6203, 8768, 10096, 13174 — **jamais** dans le handler `/v1/chat/completions` (11162+).

### Cache

- **A5 — `cache_control` au niveau `tools[]` perdu en P2.** Niveau message traité (mapping.py:756-1043) ; boucle `tools` (1070-1133) reconstruit `{"type":"function","function":{…}}` sans reporter `cache_control` → breakpoint perdu, hit-rate de préfixe dégradé.
- **A6 — comptabilité cache non testée.** `_extract_cache_tokens` / `_extract_cache_creation_tokens` (140-165) + réémissions en 1436-1437, 1846-1848, 2219, 2298, 2982-2989, 3044-3056, 3373-3380 : 7 sites ; **2 seules références à `cache_control` dans tout `tests/`** (test_proxy.py ; l'autre est `test_static_cache_asgi`, hors sujet).
- **A7 — réécriture de prompt conditionnelle.** `_restructure_for_cache` (288-350) limité aux `CACHE_REWRITE_MODELS`, avec `supports_cache_control = not model.startswith("glm-5")` (719) : deux exceptions en dur sans test de contrat.

### Contexte (système, historique, tools, orphelins)

- **A8 — outils : 3 mécanismes empilés sans contrat commun.** `_normalize_tool_schema` (491, profils 477), `sanitize_tool_names`/`restore_tool_name` (2336/2406), `_drop_orphan_tool_messages` (85) / `_drop_orphan_responses_input` (112) — appliqués inégalement selon le chemin (guards en 11269-11274 et 13222-13227, pas symétriquement en P2/P4 selon conversion ou passthrough).
- **A9 — documents : couverture asymétrique.** Aller P2 : `document` → `{type:"file"}` avec replis `[document:url:…]` (817-862, 927-948) ; retour P4 : `file` → `document` PDF-URL seulement (1534-1546, 1637-1649) ; Responses : `input_file` (1920-1963, 2533-2566, 2743-2790). **Un seul fichier de test contient le mot `document`** (test_conversion_images.py) : aucun verrou golden documents.
- **A10 — estimation de tokens aveugle aux médias.** `_extract_text` (188-215) réduit image/document à `[image:type]`/`[document:type]` : `count_tokens` (11151) et l'estimation d'entrée en stream sous-estiment structurellement le vision/PDF.

### Streaming & contrat de sortie

- **A11 — P5/P6 « faux streaming ».** Pour `stream: true`, les 6 sites n'émettent qu'un `response.completed` terminal (13588, 13658, 13949, 14063…) : aucun `response.output_text.delta`, `response.reasoning_summary_text.delta`, `response.output_item.added`, `response.function_call_arguments.delta`. TTFB = durée totale de génération.
- **A12 — `_responses_sse_to_chat_deltas` (3094) n'a qu'un golden** (`sse_deltas`) et aucun test de bout en bout des états (`ResponsesSseState` 3074).

### À trancher (non concluable par lecture seule)

Ordre des blocs `thinking`/`text`/`tool_use` en multi-tours sur P4/P5 ; interaction `_handle_web_search`/`_handle_web_fetch` (8160-8450, mutation du contexte par préfixe user) avec `tool_result` ; comportement `_restructure_for_cache` face aux `tool_result` orphelins.

### Établies par confrontation aux specs officielles (voir §9)

A13-A23 : `output_config.effort` jamais lu, forme `thinking.enabled` rejetée sur Claude 4.7+, `reasoning_effort` envoyé à un upstream Anthropic, overrides d'effort au mauvais emplacement, `max_completion_tokens` jamais émis, `thinking.display` non géré, ventilation d'usage manquante, cache Anthropic (plafond 4 + top-level + TTL) non géré, faux streaming Responses confirmé, enum d'effort OpenAI sous-exploité, **invariant `max_tokens > budget_tokens` violable**. Ces onze points ne sont pas des hypothèses : ils sont confirmés par la documentation officielle et le code de référence (LiteLLM), donc directement corrigeables (§9.5 pour l'ordre).

---

## 3. Déjà solide (ne pas refaire)

`test_conversion_images.py` (24 tests), `test_thinking_e2e.py` (17, dont ordre `thinking_delta* → signature_delta → content_block_stop`), `test_tool_compat.py`, `test_tool_name_sanitization.py`, `test_tool_schema_normalization.py`, `test_tool_schema_strict.py`, `test_effort_caps.py`, `test_effort_mapping.py`, `test_proxy.py` (55 Ko), guards orphelins (test_proxy.py:141-224). Le verrou golden existe (`test_conversion_golden.py` + `scripts/gen_golden_fixtures.py` + `test_docs_drift.py` lisant `docs/_drift_manifest.json`) : **on l'étend, on ne le remplace pas**.

---

## 4. Méthode de vérification — 4 étages

- **V1 · Matrice de capacités.** Test paramétré 6 chemins × 15 axes, exécutant les fonctions réelles sur un corps témoin et **assertant la présence/absence de chaque champ** (jamais « ça ne plante pas »). Toute case non couverte est déclarée *gap connu* dans le test : impossible d'ajouter un chemin sans le déclarer.
- **V2 · Goldens déterministes.** Extension de `CASES` dans `scripts/gen_golden_fixtures.py` : un cas par chemin × axe sensible (effort, `cache_control` tools, PDF base64, document URL, `file_id`, image dans `tool_result`, multi-tours thinking, `tool_choice` forcé, sanitize/restore nom d'outil, usage cache). Fixtures SSE rejouées **ligne par ligne** (pattern déjà en place).
- **V3 · E2E ASGI avec faux amont.** `httpx.ASGITransport` sur `opencode.app`, amont fake qui **capture le corps exact reçu** : seul niveau qui attrape overrides de route, swaps free-model et passthrough.
- **V4 · Propriétés & corpus.** Round-trip A→B→A sur corpus versionné (`tests/fixtures/protocol_corpus/`) : idempotence des champs neutres, invariants (ids d'outils, ordre des blocs, nombre d'images/documents, budget ≤ plafond modèle).

Gate : `powershell -File scripts/gate.ps1` (ruff → mypy prod → `pytest -k "not docker" --cov-fail-under=45` → bench → pip-audit → gitleaks → compose).

---

## 5. Lots de travail

**L0 — Plan persisté + inventaire brut** *(0,5 j)* — **FAIT**
Ce document. Inventaire par `grep` de **tous** les sites appelant une conversion (fonction → fichier:ligne), figé ici comme checklist de couverture.

### L0.1 Inventaire figé des sites de conversion (2026-09-10, après hotfix)

| Fonction (`mapping.py`) | Sites d'appel (`opencode.py`) |
|---|---|
| `anthropic_to_openai` | 9768, 9770 |
| `openai_to_anthropic` | 9837, 10049 |
| `openai_to_anthropic_request` | 12378 |
| `anthropic_to_openai_response` | 12448, 12568 |
| `openai_responses_to_anthropic` | 13223, 13236 |
| `anthropic_to_openai_responses` | 13323, 13505, 13610 |
| `_chat_to_responses_request` | 6196, 9773, 10101, 11442, 11641 |
| `_sanitize_native_responses_request` | 6192 |
| `_responses_to_chat_response` | 6620, 11550 |
| `_responses_to_anthropic_response` | 6615, 10038 |
| `_responses_sse_to_chat_deltas` | 10442, 11990, 12015 |

- **Sites d'émission `response.completed` (faux streaming A11)** : 13325, 13611, 13681, 13972, 14086.
- **Sites d'appel `ensure_min_tokens`** : 6203, 8768, 10102, 13190 — **jamais** dans `/v1/chat/completions` (11167) → A4 confirmée.
- **Sites `cache_control` (`mapping.py`)** : émission 399, 798, 804, 1077, 1099, 1110 ; report 835-836, 1036-1037, 1062-1063, 1076-1077, 1089-1090, 1098-1099 ; Responses 2741, 2821, 2905. La boucle `tools` (1129-1190) n'en reporte **aucun** → A5 confirmée.

### L0.2 Résultat du Lot 1 — matrice de vérité

`tests/test_protocol_matrix.py` : **53 cas**, 6 chemins × 15 axes. Statut des anomalies :

| Anomalie | Statut | Preuve (test) |
|---|---|---|
> **Note de lecture (15/09/2026).** Les temoins cites dans ce tableau portent leur nom
> **actuel** : plusieurs ont ete renommes quand le defaut qu'ils decrivaient a ete corrige
> (`..._is_a_known_gap` -> `..._is_wired`, `..._is_lost` -> `..._is_preserved`,
> `..._is_not_incremental_known_gap` -> `..._is_incremental`). Un ancien nom aurait inverse
> le sens de la lecture : il aurait fait passer un trou ferme pour un trou ouvert.

| A1 (4 mappings divergents) | **confirmée** | `test_axis_effort_p2_*`, `test_axis_effort_p4_*` |
| A2 (`xhigh`/`max` écrasés, `minimal` filtré) | **corrigée au hotfix** | `test_axis_effort_p4_no_level_is_lost`, `_minimal_is_recognized` |
| A3 (P6 perd l'effort) | **corrigée au hotfix** | `test_axis_effort_p6_reaches_anthropic` |
| A4 (plancher absent sur P3) | **confirmée — gap connu** | `test_axis_min_tokens_floor_p3_is_wired` |
| A5 (`cache_control` tools perdu) | **confirmée** | `test_axis_cache_control_on_tools_p2_is_preserved` |
| A6 (usage cache non testé) | **traitée ici** | `test_axis_cache_read_tokens_extracted` (×3), `_creation_tokens_extracted` |
| A7 (réécriture cache conditionnelle) | à couvrir L3 | — |
| A8 (tools : 3 mécanismes) | **contrat prouvé** | `test_axis_tool_name_roundtrip_is_identity` (×8), orphelins |
| A9 (documents asymétriques) | **confirmée couverture** | `test_axis_document_url_p2_becomes_file`, `_p6` |
| A10 (tokens aveugles aux médias) | **confirmée** | `test_axis_token_estimate_ignores_media_size` |
| A11 (faux streaming) | **confirmée — gap connu** | `test_axis_responses_stream_is_incremental` |
| A12 (`_responses_sse_to_chat_deltas` peu testé) | **traitée ici** | `test_axis_chat_stream_conversion_produces_deltas`, `_reasoning_delta` |
| A13-A16, A23 | **corrigées au hotfix** | `test_effort_mapping.py` (§10) |

Deux tests sont écrits comme **détecteurs de correction** (A4, A11) : ils passent aujourd'hui en documentant le trou, et échoueront dès que L2/L5 corrigeront — signal explicite pour les retourner. C'est volontaire : un gap non déclaré est invisible, un gap déclaré est traçable.

**L1 — Matrice de vérité & preuve des 12 anomalies** *(1,5 j)* — **FAIT** (voir L0.2)
`tests/test_protocol_matrix.py` (V1) + un test par anomalie A1→A12, écrits **avant** tout correctif (rouges attendus pour A1-A7, A11). Sortie : chaque anomalie passe à *confirmée* / *invalidée* avec la sortie de test comme preuve. Aucun correctif dans ce lot.

**L2 — Source unique de vérité pour l'effort** *(1 j)* — traite A1, A2, A4
`config/effort_policy.py` : `resolve_effort(fields, model) -> {reasoning_effort | thinking_budget | none}`, plafonds `effort_caps` appliqués **partout**, `minimal` géré, tableau budget↔niveau unique. Recâbler les 4 sites (mapping.py:251, 1793, 2129 ; opencode.py:11224). Parité `ensure_min_tokens` sur P3. Tests : `test_effort_all_paths.py` (même entrée → même sortie sur les 4 sites, et ≤ plafond modèle).
*Décision requise avant code (A3) :* contrat P6 — soit `openai_responses_to_anthropic` émet `thinking.budget_tokens`, soit le relais `reasoning_effort` est consommé explicitement. Recommandation **RÉVISÉE par les specs (§9)** : ni l'un ni l'autre — Anthropic a déprécié `thinking:{type:"enabled",budget_tokens}` (400 sur Claude 4.7+) ; la cible est `thinking:{type:"adaptive"}` + `output_config.effort`. La question ne se pose donc plus en termes de budget mais de **niveau d'effort** (voir L9).

**L3 — Contexte & cache** *(1,5 j)* — traite A5, A6, A7, A8, A10
Préservation `cache_control` au niveau `tools[]` (P2) ; `test_cache_contract.py` couvrant les 7 sites d'usage cache et les breakpoints par chemin ; contrats explicites `CACHE_REWRITE_MODELS` et exception `glm-5` ; guards d'orphelins homogènes par chemin ; estimation tokens médias par comptage réel (images : ratio dimensions ; documents : tokens du texte extrait ou marqueur documenté) dans `protocol/tokens.py` + test `count_tokens` vision/PDF.

**L4 — Tools & documents : matrice complète** *(1,5 j)* — traite A9 — **PARTIELLEMENT TERMINÉ**
`test_tools_matrix.py` + `test_documents_matrix.py` : pour les 6 chemins — `tools[]`/`tool_choice`/`strict`/nom long/`input_schema` invalide, puis `document` (base64 PDF, URL, `file_id`, texte), image, et `tool_result` portant document/image. Chaque perte silencieuse est soit corrigée, soit **déclarée** dans `docs/conversion-matrix.md` (la ligne « PERDU silencieusement » pour l'image a été **rétablie et corrigée** en §11.10 : elle était trop large — `base64` et `url` traversent intacts, seul un `source.type` `file`/inconnu devient `[image:…]`).

*Livré* : **`tests/test_tools_matrix.py`** (**30 cas** — axes `tools[]`,
`tool_choice` nommé, `strict`, nom long, `input_schema` invalide × les 6 chemins,
dont les 7 tests A8 (5 `test_a8_*`, l'axe P2 renommé, la réversibilité)).
Le volet documents est couvert par **`tests/test_documents_contract.py`** (dont les
2 verrous A25) plutôt que par un `test_documents_matrix.py` séparé — le
regroupement est assumé, le contenu du plan est couvert.
**Plus rien d'ouvert sur cet axe** : l'écart **A8 résiduel** (nom d'outil > 64
vers une cible Chat sur P2) a été **corrigé par le lot L4**. L'ancien
`xfail(strict=True)` qui le portait a été levé — il est passé en XPASS au câblage
de la sanitize, ce qui a forcé la correction du marqueur : cf. §11.9.

**L5 — Streaming incrémental `/v1/responses`** *(2 j)* — traite A11, A12
Émettre les vrais événements Responses (`response.created`, `output_item.added`, `output_text.delta`, `reasoning_summary_text.delta`, `function_call_arguments.delta`, `output_item.done`, `response.completed` avec `usage`) pour P5/P6, en dérivant des deltas chat/Anthropic déjà consommés. `test_responses_stream_contract.py` : ordre contractuel, un delta par fragment, usage final, `err_stream` gardant un terminal cohérent. À faire **après** L2/L3 (mêmes zones de `opencode.py`).

**L6 — Goldens étendus + gate doc** *(1 j)*
`gen_golden_fixtures.py` : ~11 → ~35 fixtures (une par case de matrice). Mise à jour `docs/conversion-matrix.md` (6 chemins × 15 axes, pertes assumées) et `docs/_drift_manifest.json` : `min_count` relevé, `must_contain` complété des fonctions non listées (`anthropic_to_openai_responses`, `openai_chat_to_responses`, `_responses_to_chat_response`, `_responses_to_anthropic_response`, `sanitize_tool_names`). Le gate `test_docs_drift` lie code ↔ doc dans les deux sens : une fonction non documentée casse le CI.

**L7 — E2E & corpus** *(1,5 j)* — V3, V4 — **TERMINÉ**
`test_e2e_protocol_matrix.py` : 6 chemins × stream/non-stream avec capture du corps amont + corpus round-trip. Inclut les sous-chemins free-model et failover sur P2 stream et P4 stream.

*État final* : 27 fonctions → **35 cas collectés**, **tous verts** (les 3 cas
P4-stream sont désormais de **simples tests de régression** : A24 est corrigée,
les marqueurs `xfail(strict=True)` ont été **retirés**), plus les 5 verrous
`test_l13_*` ajoutés par le lot L13 et le test de restauration A8 en streaming.
Suite complète hors `docker` : **`GATE OK`, exit 0**, et **aucun `xfail`** — le
dernier portait l'écart A8, désormais corrigé (voir §11.9) ; chiffres exacts du
dernier passage en §8 du rapport. Écart au texte du
plan, à acter : le corpus V4 est **embarqué dans le fichier de test**
(`CORPUS_TEXT/TOOLS/IMAGES/DOCUMENTS/REASONING`), il n'existe pas de
`tests/fixtures/protocol_corpus/`. Couverture : 5 cas de corpus × 2 chemins
(P2, P4) + 3 tests de propriétés transverses.

Le lot a produit **deux anomalies hors inventaire** (§11.8) : **A24** (P4-stream,
0 octet silencieux — **corrigée**) et **A25** (nom de fichier des documents
perdu — corrigé, `tests/test_documents_contract.py`).

**L8 — Rapport & clôture** *(0,5 j)*
`RAPPORT_AUDIT_CONVERSIONS_2026-09-10.md` : matrice finale remplie, anomalies confirmées → correctif → test de verrouillage, anomalies invalidées avec preuve, pertes résiduelles assumées et documentées. Gate complet vert, `test_docs_drift` inclus.

### Lots ajoutés après confrontation aux specs officielles — voir §9

**L9 — Bascule thinking legacy → adaptive + `output_config.effort`** *(2 j)* — traite A13, A14, A16
Supprimer la génération de `thinking: {type:"enabled", budget_tokens}` vers Anthropic (forme dépréciée, 400 sur Claude 4.7+) au profit de `thinking: {type:"adaptive"}` + `output_config.effort`. Lire `output_config.effort` (jamais lu par aucun convertisseur aujourd'hui) et faire écrire les overrides de route sur `output_config.effort` (et non `body["effort"]`). Tests : un client qui envoie `output_config.effort` obtient bien un `reasoning_effort` en P2 ; un client Chat qui envoie `reasoning_effort` obtient `output_config.effort` (et non `thinking.enabled`) en P4 ; aucun chemin n'émet plus la forme rejetée.

**L10 — Enum effort OpenAI complet** *(0,5 j)* — traite A2 (suite)
`xhigh`/`max`/`minimal`/`none` reconnus nommément dans la politique unique (L2) et plafonnés par modèle, au lieu du repli actuel `xhigh|max → high` et `minimal → low`.

**L11 — Conformité cache Anthropic** *(1 j)* — traite A20
Compter et plafonner les breakpoints explicites à 4 (400 au-delà), gérer le `cache_control` **top-level** (automatic caching) et le TTL `{"type":"ephemeral","ttl":"1h"}`, et documenter que le cache est invalidé quand la config de thinking/effort change (nos overrides d'effort par route font varier l'effort — donc le cache). Test : un corps converti ne dépasse jamais 4 breakpoints ; ordre `tools → system → messages` respecté.

**L12 — Usage & display** *(0,5 j)* — traite A18, A19
Remonter `output_tokens_details.thinking_tokens` dans les conversions d'usage, et traiter `thinking.display` (`"omitted"`/`"summarized"`) : avec `display: "omitted"`, **aucun** `thinking_delta` n'est émis, seul le `signature_delta` — un proxy qui attend des deltas de thinking doit tolérer ce cas.

**L13 — Transport du raisonnement vers Chat + restore des noms d'outils** *(1 j)* — traite B1, B4 — **TERMINÉ**
`reasoning_content` est une extension vendeur, pas du spec : documenter le contrat réel par upstream (accepte/ignore/rejette), définir un encodage de repli pour les upstreams stricts, et prouver que `restore_tool_name` est appliqué sur TOUTES les voies de retour (Anthropic accepte 200 caractères, OpenAI 64 — le sanitize n'a de sens que vers OpenAI/Responses).

*Livré* : `docs/reasoning-content-contract.md` (les 3 contrats Chat/Anthropic/
Responses mesurés par sonde) + repli « retry-once » implémenté sur
`/chat/completions` (il n'existait que pour `/responses`). 5 tests de
verrouillage `test_l13_*`, vérifiés par mutation. Détail : §11.9.

**L14 — `max_completion_tokens`** *(0,5 j)* — traite B2, A17
Émettre `max_tokens` ou `max_completion_tokens` selon le type d'upstream ; `max_tokens` vers un modèle de raisonnement Chat = rejet probable. Test par famille d'upstream.

**L15 — Conformité Responses (requête + usage + événements)** *(1,5 j)* — traite B3, B5, B6, B7, B8
Relayer `store` (avec avis confidentialité : défaut `true` = rétention ≥30 j) et `truncation` ; traduire `cache_control` → `prompt_cache_breakpoint` sur les parts ; remonter `cache_write_tokens` et `reasoning_tokens` dans la compta ; `sequence_number` sur chaque événement émis ; document URL → `input_file.file_url` en direction Responses ; capturer le reasoning depuis `output_item.done` (pas `.added`).

**L16 — Invariant `max_tokens > budget_tokens`** *(0,5 j)* — traite A23
Appliquer `budget <= max_tokens - 1`, et supprimer le thinking quand `max_tokens <= 1024` (minimum du budget). Coordonner avec `ensure_min_tokens` (A4) : la releve de `max_tokens` doit etre faite **avant** le calcul du budget, sinon l'invariant est calcule sur une valeur qui va changer. Test : pour `max_tokens` in {512, 1024, 4096} × effort in {low, medium, high, max}, le corps emis satisfait toujours l'invariant — ou ne porte pas de thinking du tout.

Charge : **~11 jours-homme pour L0-L8, ~15 avec L9-L12, ~18 avec L9-L15, ~19 avec L9-L16**. Parallélisable après L1 sur deux fronts ({L2, L3} et {L4, L6, L7}) ; L5 en série (partage `opencode.py` avec L2).

---

## 6. Matrice de couverture cible (16 axes × 6 chemins)

> Le titre annonçait « 15 axes » : il y en a **16** (recompté à l'audit L1).
> La colonne **P1** a été **falsifiée** par A26 (§11.11) et les colonnes P2, P3 et P5
> ont été reprises ligne par ligne à l'audit L1 : les corrections sont portées dans le
> tableau, et les cellules restées fausses ou imprécises sont listées en §11.12.

| Axe | P1 | P2 | P3 | P4 | P5 | P6 |
|---|---|---|---|---|---|---|
| effort entrée | natif ⚠️ | `resolve_effort` + caps modèle | `resolve_effort` + caps (L2) | `resolve_effort`→`output_config.effort` + `thinking.adaptive` | `output_config.effort`→`reasoning_effort` ; natif si free `/responses` | `output_config.effort` + `thinking.adaptive`, **A3 corrigée** |
| budget thinking↔niveau | natif ⚠️ | table unique 4 paliers, aucun budget émis | idem (L2) | **aucun budget émis** (le niveau reste un niveau) | **budget non lu** : clé `thinking` non recopiée | **aucun budget émis** — choix, pas trou |
| plancher max_tokens | oui ⚠️ | oui | **oui (corrigé L2)** | oui (relevé **par champ**) | oui | oui ; précédence `max_tokens`→`max_completion_tokens`→`max_output_tokens` |
| thinking multi-tours | strip synthétique ⚠️ | `reasoning_content` | passthrough, **sans repli retry-once** | **signature HMAC locale forgée émise vers l'amont** (trou) | résumé `reasoning` droppé | droppé assumé (golden) |
| `redacted_thinking` | natif ⚠️ | cache 512 en **écriture seule** (aucune réinjection) | passthrough | **perdu en silence** (le cache ne vit que sur P2) | — | **perdu en silence** |
| ordre blocs réponse | natif ⚠️ | golden (non-stream) ; **SSE non verrouillé** | passthrough | **normalisé** `reasoning`→texte→`tool_calls` (entrelacement perdu) | **déterministe** : `reasoning`→texte→`function_call` | **déterministe** (golden) |
| `cache_control` messages | natif ⚠️ | **corrigé 15/09** : garde `supports_cache_control` posée aux 2 sites manquants, témoin **par site** (§12.2) | passthrough | n/a (aucune occurrence) | **client perdu** ; breakpoints auto-injectés | **perte muette** |
| `cache_control` tools | natif ⚠️ | **corrigé (L3)** ; `prompt_cache_breakpoint` reste un no-op | passthrough | abandonné (boucle reconstruite) | transporté mais **non traduit** (L15 non fait) | **perte muette** |
| usage cache (read/creation) | natif ⚠️ | read ok ; trous = sites handler, compta DB, `cache_creation` en stream | passthrough (usage amont) | read remonté ; **`cache_creation` jamais remonté**, pas d'E2E | read ok, **création perdue** | read ok ; **création jetée** (`cache_write_tokens` absent du dépôt) |
| tools schéma/strict | natif ⚠️ | `_normalize_tool_schema` (aucun `strict` émis) | passthrough | profils ; **`strict` abandonné (non déclaré)** | `_normalize_tool_schema` + profil modèle | profils ; `strict` jamais reporté |
| tool_choice | natif ⚠️ | golden | passthrough (exception `web_search`/`web_fetch`) | dict→dict ; **chaîne passée verbatim** | dict→dict (`any`→`required`) | dict→dict ; **pas de sens retour** |
| noms d'outils longs | **converti (A26)** ⚠️ | sanitize/restore | **verbatim non sanitizé — trou A8 ouvert** | **aucun remap** (nom verbatim ; 200 car. légitimes) | aller sanitizé, **retour non restauré** | **pas de remap** ; côté free Chat, non câblé |
| orphelins tool_result | — | `_drop_orphan_tool_messages` | idem (câblage non testé) | **garde câblée** avant conversion | `_drop_orphan_responses_input` | **corrigé 15/09** : le corps réellement envoyé est refiltré (resynchronisation depuis le corps Chat filtré) + `strip_synthetic_thinking` ; témoin qui mord (§12.6) |
| documents (PDF/URL/file_id) | natif ⚠️ | `{type:file}` + replis | passthrough | **les 3 formes traitées** (base64, `file_id`, URL ; URL non-PDF → texte) | base64/`file_id` ok, **URL→texte** | `input_file` → 3 formes (TESTÉ) |
| images (+ tool_result) | natif ⚠️ | data-URI + **placeholders** (pas de drop silencieux) | passthrough | data-URI / URL ; **images dans `tool_result` préservées** | `input_image` | `input_image` ; **part inconvertible = drop muet** |
| streaming incrémental | **converti (A26)** ⚠️ | `stream_gen` (`opencode.py:10167`) | relais verbatim ligne à ligne | payant **réellement incrémental** ; jambe free Chat **corrigée 15/09** (0 octet → corps converti, MESURÉ) | **séquence conforme, émission bufferisée (A11 partiel)** | **idem — TTFB = durée totale** (A11 partiel, §12.11) ; **mais** le 503 mensonger de P6-stream est corrigé (jambe free désormais réellement tentée, §12.9) |

> **Provenance.** `natif` = aucune conversion, le corps et la réponse traversent tels
> quels. Ce tableau est **la cible** ; l'état constaté et sa provenance
> (`TESTÉ` / `MESURÉ` / `DÉDUIT` / `NON TESTÉ`) sont en §11.12.
>
> ⚠️ **P1** — colonne **falsifiée** par A26 (§11.11) : « natif » ne vaut que pour la
> **jambe payante**. Dès que le modèle payant a un équivalent free à endpoint
> `/chat/completions`, corps et réponse sont **convertis** dans les deux sens. La
> mention « natif » seule était fausse : elle décrivait le chemin nominal sans
> franchir la bascule free, et la jambe free n'était exercée que sur les chemins à
> protocole `openai`.
>
> ⚠️ **État des trous au 15/09/2026.** Cette matrice décrit **la cible** ; l'état vérifié de chacun des 13 trous déclarés (ce qui est corrigé, réfuté, mesuré, ou encore ouvert) est tenu **par trou** au **§12**, avec la preuve associée. Une cellule ci-dessus peut donc être en avance ou en retard sur l'état réel : en cas de doute, c'est le §12 qui fait foi.
>
> ⚠️ **P3 — trou A8 ouvert** : `sanitize_tool_names` n'est appelé **nulle part** dans
> `opencode.py`. Un nom d'outil > 64 caractères part donc **verbatim** vers un amont
> Chat sur P3, et le lot L4 ne l'a pas couvert (il ne traite que P2 et les
> convertisseurs). La cellule « passthrough » était exacte au sens littéral mais
> **trompeuse** : elle masquait ce trou.

---

## 7. Risques & garde-fous

1. **`opencode.py` = 13 225 lignes, 213 defs** : L2 et L5 touchent les mêmes zones → séquencement strict, un lot = un commit, pas de refonte opportuniste.
2. **Contrat V1 gelé (ADR-006)** : le golden est un *verrou*. Toute régénération de fixture doit être relue en diff avant commit ; interdiction de régénérer « pour faire passer ».
3. **`test_docs_drift` bloquant** sur v1/conversion/clients : L6 n'est pas optionnel.
4. **Budget de boot** : CI rouge si `import opencode` > 1200 ms ou si httpx/rich/tiktoken/click sont chargés au boot → `config/effort_policy.py` sans import lourd (règle `effort_caps` : `yaml_get` seul).
5. **Décisions ouvertes** : contrat d'effort P6 (A3), granularité des deltas Responses (L5), politique documents non-PDF (repli texte vs `document`).

---

## 8. Definition of Done

- Les 90 cases de la matrice sont **vertes par test** ou **documentées comme perte assumée** dans `docs/conversion-matrix.md`.
- Chaque anomalie A1-A23 : confirmée/invalidée avec preuve ; pour les confirmées, un test échoue sans le correctif. Les 8 différences issues de la confrontation aux specs (A13-A16, A21-A23) sont **déjà confirmées** par la documentation officielle et le code de référence : elles ne relevent pas du Lot 1 mais d'un correctif direct.
- `scripts/gate.ps1` vert intégralement (`ruff`, `mypy` prod 0 erreur, `pytest -k "not docker"` couverture ≥ 45 %, `test_docs_drift` inclus) et budget d'import respecté.
- Goldens ≥ 30 fixtures couvrant les 6 chemins ; `RAPPORT_AUDIT_CONVERSIONS_2026-09-10.md` écrit.

---

## 9. Confrontation aux specifications officielles (2026-09-10)

Documentation primaire consultée (endpoints `.md` officiels, récupérés et fouillés le 2026-09-10) :

- Anthropic — [Extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking.md), [Thinking](https://platform.claude.com/docs/en/build-with-claude/thinking.md), [Steering thinking](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost.md), [Troubleshooting thinking](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting.md), [Effort](https://platform.claude.com/docs/en/build-with-claude/effort.md), [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching.md)
- OpenAI — [Reasoning models](https://developers.openai.com/api/docs/guides/reasoning.md), [Streaming Responses](https://developers.openai.com/api/docs/guides/streaming-responses.md), [Responses create](https://developers.openai.com/api/reference/resources/responses/methods/create.md), [Function calling](https://developers.openai.com/api/docs/guides/function-calling.md), [OpenAPI officiel](https://raw.githubusercontent.com/openai/openai-openapi/master/openapi.yaml)

### 9.1 Ce que les specs confirment dans notre approche (à ne PAS changer)

| Point | Spec | Notre code |
|---|---|---|
| Ordre de streaming thinking | `thinking_delta*` puis **un** `signature_delta` juste avant `content_block_stop` ([trace complète](https://platform.claude.com/docs/en/build-with-claude/thinking.md)) | conforme : `_thinking_flush`, 8588, 9675 |
| Signature sur blocs thinking | le client doit **renvoyer les blocs tels quels** ; un bloc modifié/reconstruit → 400 `thinking blocks cannot be modified` | notre strip sélectif des blocs à signature **locale** vers Anthropic est le bon réflexe (8819) |
| `cache_control` sur system + dernier message | placements valides ; l'ordre de cache est `tools → system → messages` | conforme (731, 737, 1043) |
| Budget thinking ↔ `max_tokens` | le thinking consomme `max_tokens` ; budget trop bas vs `max_tokens` → réponse vide/tronquée | justifie notre `ensure_min_tokens` (8635) |

### 9.2 Différences constatées — notre implémentation n'est pas optimale

**A13 (CRITIQUE) — `output_config.effort` n'est lu par aucun convertisseur.**
`output_config` apparaît **0 fois** dans `app/protocol/mapping.py`. Le seul lecteur est `opencode.py` (8792, 11214) et c'est **pour le log uniquement**. Or c'est *le* champ d'effort de l'API Messages d'aujourd'hui ([Effort](https://platform.claude.com/docs/en/build-with-claude/effort.md) : « Set `output_config.effort` on the request »). Conséquence : un client moderne (Claude Code et consorts) qui envoie `output_config.effort` voit son effort **journalisé puis jeté** en P2 — aucun `reasoning_effort` n'est émis vers l'upstream. Le commentaire de code « Claude Code sends: … OR effort: low/medium/… » (1147, 2125) décrit un champ qui n'est plus celui de l'API.

**A14 (CRITIQUE) — on génère la forme de thinking désormais rejetée.**
`openai_to_anthropic_request` (mapping.py:1796) émet `thinking: {"type": "enabled", "budget_tokens": N}`. La spec est explicite : cette forme est **dépréciée** sur Claude 4.6 et **rejetée avec un 400** sur Claude 4.7+ ([extended-thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking.md), [troubleshooting](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting.md)). La cible est `thinking: {"type": "adaptive"}` + `output_config.effort`. Idem pour le budget dérivé du ratio 16000/10000/4000 : ce ratio est une **invention locale**, sans équivalent documenté.

**A15 (CRITIQUE) — on envoie `reasoning_effort`, un nom de champ OpenAI, à un upstream Anthropic.**
`openai_responses_to_anthropic` écrit `result["reasoning_effort"]` puis `return` (mapping.py:2132-2134), et P6 étant un passthrough Anthropic (opencode.py:13220 → 13247), ce champ part tel quel vers Anthropic, **sans aucune clé `thinking`**. Le nom de champ Anthropic est `output_config.effort`. Confirmé : A3 est réel, et sa gravité est plus élevée que « effort perdu » — c'est « effort perdu **et** champ non conforme envoyé ».

**A16 — les overrides de route écrivent au mauvais endroit.**
`body["effort"] = effort_override` (opencode.py:8778, 12379, 13240) écrit un champ **top-level `effort`**, alors que la spec place l'effort sous `output_config.effort`. Ces overrides ne sont donc pas vus par un upstream Anthropic conforme.

**A17 — `max_completion_tokens` jamais émis.**
0 occurrence dans tout le dépôt. Nos conversions n'émettent que `max_tokens` (1050, 1707, 2095). À vérifier par test : les upstreams OpenAI de type modèles de raisonnement qui exigent `max_completion_tokens` peuvent rejeter `max_tokens` — risque de 400 non couvert.

**A18 — `thinking.display` non géré.**
La spec ajoute `display: "omitted" | "summarized"` : avec `omitted`, **aucun `thinking_delta` n'est émis**, seuls le `content_block_start` et le `signature_delta`. Nos consommateurs de flux raisonnent comme si les deltas de thinking arrivaient toujours — cas à couvrir (et opportunité : `omitted` réduit le TTFB).

**A19 — ventilation d'usage manquante.**
`output_tokens_details.thinking_tokens` (lecture seule, observabilité) n'est remonté nulle part ; notre usage ne transporte que `input_tokens`/`output_tokens`/`cache_*`.

**A20 — cache : plafond et formes non gérés.**
Max **4 breakpoints explicites** au niveau bloc, sinon 400 ; il existe désormais une **automatic caching** via un `cache_control` **top-level**, et un TTL `{"type":"ephemeral","ttl":"1h"}` ([prompt-caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching.md)). Notre conversion pose ses propres breakpoints (system + dernier message + ceux du client) sans compter. Aggravant : « Changing the top-level effort value between requests invalidates prompt caching » — nos overrides d'effort par route qui varient d'une requête à l'autre **cassent le cache** par construction.

**A21 (confirmé par la spec) — le « faux streaming » Responses est bien non conforme.**
La spec documente comme ensemble minimal consommé par un client : `response.created`, `response.output_text.delta`, `response.completed` (+ `error`) ([streaming-responses](https://developers.openai.com/api/docs/guides/streaming-responses.md)). Nous n'émettons que `response.completed` (13588, 13658, 13949, 14063), **sans même le `response.created`** : un client qui attend `response.created` avant d'afficher peut rester bloqué. A11 passe de « suspicion » à **confirmé**.

**A22 — enum d'effort OpenAI sous-exploité.**
`reasoning.effort` / `reasoning_effort` acceptent, selon le modèle : `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` ([reasoning](https://developers.openai.com/api/docs/guides/reasoning.md) ; `xhigh` = deep research/agentique long, `max` = tâches les plus complexes). Notre repli `xhigh|max → high` et `minimal → low` **écrase** ces niveaux avant même que les plafonds par modèle s'appliquent.

### 9.3 Ce que la documentation invalide dans le plan initial

| Point du plan | Statut après specs |
|---|---|
| A5 (`cache_control` tools perdu) = anomalie majeure | **Déclassée** : l'upstream OpenAI n'a pas de `cache_control`, et l'ordre `tools → system → messages` rend le breakpoint message suffisant. Le sujet réel est inverse : nous *ajoutons* `cache_control` sur des messages Chat (731/737/743), champ non standard. |
| A3 « contrat P6 : budget `thinking` ou relais `reasoning_effort` » | **Reformulée** : les deux options sont obsolètes ; cible = `output_config.effort`. |
| A11 « suspicion de faux streaming » | **Confirmée** par la spec (A21). |
| Ratio budget↔effort 16000/10000/4000 | **Invalidé comme référence** : aucun mapping officiel n'existe ; c'est une heuristique locale à assumer comme telle. |
| Priorité : L2 (politique d'effort) | **Confirmée, mais enrichie** : sans L9 (`output_config.effort` + adaptive), L2 recâblerait 4 sites vers une cible déjà fausse. |

### 9.4 Constatations cote OpenAI (rapport de recherche spec, 2026-09-10)

Sources : [Reasoning](https://developers.openai.com/api/docs/guides/reasoning.md), [Streaming](https://developers.openai.com/api/docs/guides/streaming-responses.md), [Responses create](https://developers.openai.com/api/reference/resources/responses/methods/create.md), [Function calling](https://developers.openai.com/api/docs/guides/function-calling.md), [OpenAPI officiel](https://raw.githubusercontent.com/openai/openai-openapi/master/openapi.yaml).

**B1 — `reasoning_content` n'est pas un champ officiel de Chat Completions.**
Le `delta` officiel ne contient que `content`, `function_call` (déprécié), `refusal`, `role`, `tool_calls` ; l'objet message n'a aucun champ raisonnement. C'est une convention vendeur (DeepSeek/GLM/etc.). Notre proxy en fait **le** transport du raisonnement en P2 (41 sites) : c'est conforme à l'écosystème réel mais pas à la spec. Action (L13) : documenter le contrat réel (quels upstreams l'acceptent/ignorent) et décider d'un encodage de repli pour les upstreams stricts.

**B2 — `max_tokens` déprécié, incompatible avec les modèles o-series.**
Il faut émettre `max_completion_tokens` vers un upstream Chat de type raisonnement (renforce A17/L14). `max_output_tokens` a la même sémantique côté Responses.

**B3 — Caching Chat : pas de `cache_control`, mais un vrai équivalent.**
OpenAI utilise `prompt_cache_breakpoint: {mode:"explicit"}` **sur les content parts** (text, image_url, input_audio, file, tool messages), `prompt_cache_options.{mode,ttl}` (ttl = `"30m"`), et rapporte `cached_tokens` + `cache_write_tokens`. Notre traduction `cache_control → upstream OpenAI` est donc doublement fausse : le nom du champ **et** le placement (bloc vs part). Action (L11) : traduire `cache_control` → `prompt_cache_breakpoint` sur les parts, et `cache_write_tokens` dans la compta (A6).

**B4 — Limite de nom d'outil confirmée : 64, `[a-zA-Z0-9_-]`.**
Notre sanitize/restore est donc **mandaté** vers OpenAI/Responses ; Anthropic accepte 200 (`tool_use.name`, réf. API). Vérifié : `openai_to_anthropic_request` ne sanitize pas (bon sens), mais il faudra prouver que **restore** est appliqué sur TOUTES les voies de retour.

**B5 — Defaults Responses à écho.**
`store` défaut `true` (rétention ≥30 j) et `truncation` défaut `"disabled"` (400 en dépassement de contexte, pas de troncature silencieuse). Notre proxy ne relaie ni l'un ni l'autre : le client ne contrôle ni sa confidentialité ni son mode d'échec. Action (L15) : relayer `store` (ou le forcer explicitement avec un avis), relayer `truncation`.

**B6 — Streaming Chat : `stream_options.include_usage` REQUIS pour le chunk d'usage final.**
Déjà conforme sur nos voies aller (opencode.py:10073, 11613) — à verrouiller par test. Attention : le champ `obfuscation` est désormais présent par défaut sur chaque chunk — un parseur strict qui copie les clés connues doit le tolérer.

**B7 — Événements Responses : payloads exacts connus, requis pour L5.**
`output_text.delta` porte `{content_index, delta, item_id, logprobs[], output_index, sequence_number}`, `function_call_arguments.delta` n'a **ni** `content_index` **ni** `name`, `reasoning_summary_text.delta` utilise **`summary_index`**, et **chaque** événement porte `sequence_number`. Le raisonnement en streaming ne coule que si `reasoning.summary` est opt-in ; le `encrypted_content` d'un reasoning item n'est complet qu'à `output_item.done` (pas `.added`).

**B8 — Shapes multimodales NON interchangeables.**
Chat `file` : `{file_data?, file_id?, filename?}` — **pas de `file_url`** → notre repli texte `[document:url:…]` vers Chat est le seul possible (justifié). Responses `input_file` : `{file_data|file_id|file_url}` → notre `[document:unmapped]` vers Anthropic doit devenir `input_file.file_url` en direction Responses. Chat `detail` : `auto|low|high` (pas `"original"`, réservé à Responses `input_image`).

### 9.5 Ordre d'exécution révisé

L0 → L1 (matrice de vérité, + tests A13-A22) → **L9 en PREMIER** (la cible de conversion doit être juste avant de factoriser) → L2 (politique unique, désormais cadrée par L9) → L3/L11 (cache) → L4 (tools/documents) → L5 (streaming Responses conforme, cf. A21) → L10/L12 (enum + usage/display) → L13/L14/L15 (transport Chat, max_completion_tokens, conformité Responses) → L16 (invariant budget/max_tokens) → L6/L7/L8.

### 9.6 Confrontation a la pratique de reference (LiteLLM) — corroboration decisive

Source : [litellm/llms/anthropic/chat/transformation.py](https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/llms/anthropic/chat/transformation.py) (+ `litellm/constants.py`). LiteLLM est le proxy multi-protocole de reference de l'ecosysteme ; lŕ oů la doc laisse un choix, son code le tranche.

**Table d'equivalence de facto standard** (`REASONING_EFFORT_TO_OUTPUT_CONFIG_EFFORT`, l.265) :
`low→low`, `minimal→low`, `medium→medium`, `high→high`, `xhigh→xhigh`, `max→max`.
→ Confirme A2 et le durcit : notre repli `xhigh|max → high` est une **perte d'information que l'ecosysteme ne fait pas**, et `minimal` doit devenir `low`, pas disparaitre. La cible est `output_config.effort` (l.1612), jamais `reasoning_effort` (A15).

**Invariant `max_tokens > budget_tokens`** (`cap_thinking_budget_to_max_tokens`, l.1298) : Anthropic exige `max_tokens > budget_tokens`. LiteLLM plafonne le budget a `max_tokens - 1`, et **supprime completement le thinking** si `max_tokens <= 1024` (minimum du budget). D'ou une anomalie nouvelle et grave :

**A23 (CRITIQUE) — nous pouvons emettre un budget superieur a `max_tokens`.**
`openai_to_anthropic_request` fixe `budget_tokens` jusqu'a 16000 (mapping.py:1793-1796) sans jamais le comparer a `max_tokens`, alors qu'un client Chat envoie couramment `max_tokens` = 4096 ou moins. Requete invalide. `ensure_min_tokens` (8635) releve bien `max_tokens`, mais il n'est pas appele sur toutes les voies (A4) et n'est **pas coordonne** avec le calcul du budget. Correctif : L16.

**Gating par modele avec repli explicite** : `_supports_model_capability(model, "supports_output_config")` (l.416) + `DROP_UNSUPPORTED_OUTPUT_CONFIG_WARNING` — l'effort n'est supporte que sur Opus 4.5+, Sonnet 4.6+, Mythos Preview ; ailleurs LiteLLM **retire** le champ avec un avertissement au lieu de l'envoyer. Notre equivalent (plafonds `effort_caps`) est coherent en esprit, mais nous envoyons quand meme le champ aux modeles non supportes.

**Legacy conserve en shim, `adaptive` en tete** (l.1251-1286) : `type="adaptive"` est la voie moderne, `type="enabled"` + `budget_tokens` le repli de compatibilite — l'inverse exact de ce que fait notre proxy (A14).

**Noms d'outils** : la référence API Anthropic donne `tool_use.name` en `maxLength: 200, minLength: 1` et **sans restriction de charset** (seul `tool_use.id` est contraint à `^[a-zA-Z0-9_-]+$`) ; LiteLLM cite pour sa part `^[a-zA-Z0-9_-]{1,128}$`. Dans tous les cas la limite Anthropic est **supérieure** aux 64 caractères d'OpenAI : le sanitize ne se justifie donc que **vers** OpenAI/Responses, jamais vers Anthropic. LiteLLM construit une **forward map par requête (original → sanitize)** parce qu'un sanitize naif est *lossy* : `foo/bar` et `foo_bar` se replieraient tous deux sur `foo_bar`, provoquant soit un 400 (doublon), soit une **mauvaise traduction retour** (restaurer `foo_bar` vers `foo/bar` alors que l'appelant avait reellement enregistre `foo_bar`). Notre sanitize par hachage devait etre verifie sur ce cas precis (A8) : **c'est fait** — `test_protocol_matrix.py::test_axis_tool_name_roundtrip_is_identity` (paramétré, notamment sur `name/with/slashes` et `outil.avec.points`) verrouille l'invariant « sanitize puis restore == nom d'origine », et `test_tool_name_sanitization.py::TestCollision` couvre le cas de collision. La map étant construite **par requête**, la mauvaise traduction retour est empêchée par construction.

### 9.7 Différé — corrigé en hotfix le 2026-09-10

**A13, A14, A15, A23 + défaut `adaptive` sans budget sont CORRIGÉS** (hotfix hors lots, en attendant L9/L16 qui les généralisent). Détail dans §10.

### 9.8 Points que la documentation ne tranche pas (à vérifier expérimentalement, upstream réel)

1. Un upstream Anthropic (ou un gateway openai-compatible) **rejette-t-il** un champ inconnu (`reasoning_effort`, `effort` top-level) par 400 ou l'ignore-t-il ? Détermine si A15/A16 sont des bugs silencieux ou des erreurs dures.
2. Les upstreams OpenAI de ce proxy acceptent-ils `max_tokens` ou exigent-ils `max_completion_tokens` (A17) ?
3. Les fournisseurs compatibles OpenAI utilisés ici tolèrent-ils `cache_control` (champ non standard) sur les messages ?
4. `display: "omitted"` est-il accepté par les modèles effectivement routés ?

Ces 4 questions se tranchent au Lot 1 (V1/V3) : elles ne se déduisent pas de la doc.

---

## 10. Hotfix appliqué le 2026-09-10 (avant les lots)

Trois anomalies cassaient des requêtes en production immédiatement : elles ont été corrigées hors séquence, sans attendre L0/L1. Les lots L9 et L16 restent au plan pour **généraliser** ces correctifs aux chemins non couverts ici.

### 10.1 Ce qui a été corrigé

| Anomalie | Correctif | Fichier |
|---|---|---|
| **A23** budget > `max_tokens` | Plus aucun `budget_tokens` émis : `thinking: {type:"adaptive"}` + `output_config.effort`. L'invariant `max_tokens > budget_tokens` devient **structurellement inviolable**. | `mapping.py` |
| **A14** forme `enabled` rejetée en 400 (Claude 4.7+) | Émission de `adaptive` (forme recommandée) au lieu de `enabled`. | `mapping.py` |
| **A15** `reasoning_effort` envoyé à Anthropic | Remplacé par `output_config.effort` sur les 2 sites P6. | `mapping.py` |
| **A13** `output_config.effort` jamais lu | Lu en priorité dans P2 et P6, avant le top-level historique. | `mapping.py` |
| **A16** overrides au mauvais emplacement | Les 3 sites écrivent `output_config.effort`. | `opencode.py` |
| **défaut `adaptive` sans budget** | `thinking.adaptive` **sans** budget tombait sur `xhigh` (effort **maximal**) : il vaut désormais `high` (défaut documenté), dans les 2 convertisseurs. | `mapping.py` |

### 10.2 Deux helpers ajoutés

- `_effort_to_anthropic(effort, model)` — plafond par modèle puis repli vocabulaire Anthropic `low|medium|high|xhigh|max` ; `minimal → low` (correspondance LiteLLM), `none`/vide → aucun effort, niveau inconnu → `high` (défaut documenté) plutôt que relayé en 400.
- `_apply_anthropic_effort(result, effort, model, source=)` — écrit `output_config.effort` **et** `thinking.adaptive` ; respecte un `thinking: {type:"disabled"}` explicite.

### 10.3 Preuves

- **402 tests** des suites conversion/proxy/thinking/golden : verts.
- `ruff check .` : vert.
- `mypy` : **8 erreurs préexistantes** (l. 2677-2689 avant patch), aucune introduite — vérifié par `git stash` comparatif.
- Golden `multiturn_thinking_strip.json` régénéré via `scripts/gen_golden_fixtures.py` : **1 seule ligne** change (`reasoning_effort: "xhigh" → "high"`), ce qui fige le bug corrigé plutôt que de le tolérer.
- Contrôle end-to-end : `max_tokens=4096` + `effort=high` → `{thinking:{type:adaptive}, output_config:{effort:high}}`, aucun budget, invariant respecté.

### 10.4 Changement de comportement à connaître

Le golden prouve qu'un client envoyant `thinking: {type:"enabled"}` **sans** `budget_tokens` obtenait auparavant `reasoning_effort: "xhigh"` — soit le raisonnement le plus coûteux par défaut. Il obtient maintenant `"high"`. C'est un **changement de coût à la baisse** : à surveiller si une route comptait sur l'ancien comportement.

### 10.5 Ce que le hotfix ne couvre PAS (reste aux lots)

- **L9** : gating par modèle (`supports_output_config` à la LiteLLM) — nous envoyons encore `output_config` même aux modèles qui ne le supportent pas ; LiteLLM le retire avec avertissement.
- **L16** : coordination budget/`ensure_min_tokens` (A4) — sans objet désormais pour Anthropic, mais la logique de relève de `max_tokens` reste non coordonnée pour les autres voies.
- **A17/A18/A19/A20/A22** : inchangés (voir §9.2).
- Le legacy `thinking.enabled` + `budget_tokens` reste **lu** en entrée (compatibilité clients anciens) — c'est voulu et testé.

---

## 11. Lots L2–L15 : état d'avancement

### 11.1 Lots terminés

Les nombres entre parenthèses sont des **cas collectés** (mesurés via
`pytest --co`), pas des fonctions : plusieurs fichiers sont massivement
paramétrés.

| Lot | Anomalies | Correctif | Tests (cas collectés) |
|---|---|---|---|
| **L2/L9/L10** | A1, A2 | Source unique d'effort (`resolve_effort`) ; plus de dict codé en dur repliant `xhigh`/`max` sur 16000 ; `_effort_to_anthropic` couvre tout l'enum | `test_effort_policy.py` |
| **L3** | A4, A5, A6, A7, A8 | Contrat cache : `cache_control` sur les outils n'est plus perdu ; plancher `max_tokens` | `test_cache_contract.py` |
| **L11** | A20 | Conformité cache Anthropic : ≤4 breakpoints (au-delà = 400 amont), TTL, placement top-level | `test_cache_contract.py` |
| **L12** | A18, A19 | `output_tokens_details.reasoning_tokens` remonté (était codé en dur à 0 : la part facturée la plus chère d'un modèle de raisonnement s'affichait toujours nulle) ; `thinking.display` géré | `test_usage_display.py` |
| **L5** | A11, A21 | **Streaming Responses réellement incrémental** (voir §11.2) | `test_responses_stream_contract.py` (**49**) |
| **L14** | A17, B2 | `max_completion_tokens` (voir §11.3) | `test_max_completion_tokens.py` (**34**) |
| **L15** | B5, B6, B7, B8 | Relais `store`/`truncation` ; conformité shapes multimodales | `test_responses_conformance_l15.py` (**27**) |
| **L7** | **A24**, **A25** | P4-stream ne renvoie plus 0 octet (A24) ; nom de fichier des documents plus perdu (A25) | `test_e2e_protocol_matrix.py` (**35**), `test_documents_contract.py` (**17**) |
| **L13** | B1, B4 | Contrat `reasoning_content` mesuré + repli « retry-once » sur `/chat/completions` (voir §11.9) | `test_e2e_protocol_matrix.py::test_l13_*` (5) |
| **L4** | A9, **résidu A8** | Matrice outillage des 6 chemins ; écart A8 (nom > 64 vers Chat sur P2) **corrigé** — sanitize à l'aller, remap historique/`tool_choice`, restauration non-stream **et** streaming | `test_tools_matrix.py` (13 fonctions → **30** cas, dont 7 pour A8) |

### 11.2 L5 — le « streaming » Responses n'en était pas un (A11/A21)

**Défaut réel.** Les 5 sites de `/v1/responses` en mode `stream: true` émettaient
un **unique** événement `response.completed`, sans même le `response.created`
initial :

```python
payload = _json_dumps_str({"type": "response.completed", "response": oai_resp})
sse_body = f"data: {payload}\n\ndata: [DONE]\n\n".encode()
```

Aucun delta n'existait. Un client qui attend `response.created` avant d'afficher
restait bloqué jusqu'à la fin de la génération, puis tout apparaissait d'un bloc.
La latence perçue était celle du **temps total**, pas du premier token.

**Correctif.** `ResponsesStreamEmitter` + `responses_stream_events()` produisent
la séquence conforme : `created` → `in_progress` → par item (`output_item.added`
→ deltas → `output_item.done`) → `completed`. Le texte est découpé en tranches de
64 caractères : l'objectif est la progression perçue, pas de simuler un
tokenizer — le contenu final est identique, seul le découpage diffère.

Points de spec verrouillés par test (B7) : `output_text.delta` porte
`logprobs[]` ; `function_call_arguments.delta` n'a **ni** `content_index` **ni**
`name` ; `reasoning_summary_text.delta` utilise **`summary_index`** ; chaque
événement porte un `sequence_number` strictement croissant depuis 0 ;
`encrypted_content` n'est lu qu'à `output_item.done` (jamais `.added`, où il est
partiel).

### 11.3 L14 — `max_completion_tokens` : une limite de coût silencieusement perdue (A17)

**Défaut réel mesuré.** `max_completion_tokens` avait **0 occurrence** dans le
dépôt. P4 ne lisait que `max_tokens` :

```python
"max_tokens": oai_body.get("max_tokens", 16384),
```

Un client Chat envoyant `max_completion_tokens: 4096` (la forme moderne
recommandée) **entrait avec 4096 et ressortait avec 16384** — sans erreur ni
trace. Une limite courte posée pour borner le coût devenait inopérante.

**Correctif, volontairement asymétrique** — c'est le point à ne pas casser :

- **en lecture**, les trois formes sont acceptées partout (`max_tokens`,
  `max_completion_tokens`, `max_output_tokens`) : plus aucune perte ;
- **en écriture**, la forme moderne n'est émise que vers les préfixes qui
  l'exigent réellement (`o1`/`o3`/`o4`/`gpt-5`, surchargeables via
  `thinking.max_completion_models`).

Basculer plus large serait **pire que le bug** : une passerelle tierce ignorant
`max_completion_tokens` n'appliquerait *aucune* limite de sortie — coût non
borné. B2 ne mandate le basculement que pour les modèles o-series ; nos upstreams
réels (DeepSeek, GLM, MiMo…) documentent et acceptent `max_tokens`. Un test
(`test_default_prefix_list_is_restricted_to_o_series`) échoue délibérément si
quelqu'un élargit la liste sans vérifier que l'upstream rejette `max_tokens`.

Destination Anthropic → toujours `max_tokens` : c'est le seul champ qu'Anthropic
connaît, quelle que soit la forme reçue du client.

### 11.4 L15 — `store` et `truncation` n'étaient relayés nulle part (B5)

Un client envoyant `store: false` (exigence de confidentialité, la réponse étant
sinon conservée ≥30 j) voyait sa consigne **ignorée sans trace**. `truncation`
n'était pas relayé non plus : le client ne pouvait pas choisir `"auto"` plutôt
que le 400 en dépassement de contexte.

Correctif : relais des deux champs depuis le corps d'origine, y compris sur le
chemin Anthropic (où `anthropic_to_openai` ne les transporte pas). Aucune valeur
par défaut n'est **posée** — n'émettre que si le client l'a envoyé préserve la
sémantique upstream et évite de transformer une absence en décision de rétention
que le proxy n'a pas à prendre. Les valeurs invalides sont écartées plutôt que
propagées (400 évitable).

B6 (`stream_options.include_usage`) et B8 (`input_file.file_url`,
`detail`/Chat) étaient **déjà conformes** : verrouillés par test de
non-régression.

### 11.5 Preuves : les tests échouent bien contre le code d'origine

Un test vert ne prouve rien s'il passe aussi contre le bug. Les six défauts
corrigés ont donc été **réintroduits un par un** pour vérifier que la suite passe
au rouge :

| Mutation réintroduite | Test qui doit échouer | Résultat |
|---|---|---|
| Suppression de `response.created` | `…stream_contract.py -k created` | rouge ✔ |
| Lecture de `max_tokens` seul (A17) | `…max_completion_tokens.py` | rouge ✔ |
| Relais `store`/`truncation` désactivé | `…conformance_l15.py` | rouge ✔ |
| Élargissement des préfixes (B2) | `…max_completion_tokens.py` | rouge ✔ |
| Retour au terminal unique sur le wire | `…stream_e2e.py` | rouge ✔ |
| Un site revenu au payload brut | `…stream_contract.py -k bare_terminal` | rouge ✔ |

Cette dernière ligne est une **garde structurelle** : les tests unitaires
validaient le constructeur d'événements mais n'auraient pas vu un site du handler
qui ne l'appelle pas — exactement le défaut d'origine, où les 5 sites fabriquaient
leur payload terminal à la main. Le test lit désormais la source et refuse la
réapparition du motif.

Vérification bout en bout indépendante : `test_responses_stream_e2e.py` monte
l'app FastAPI, appelle le **vrai** `/v1/responses` avec `stream: true` et un
upstream Chat SSE simulé, et constate sur le wire les 9 événements dans l'ordre
(`created` → `in_progress` → item → `content_part` → `delta` → `done` →
`completed`), le texte reconstruit à l'identique, l'usage final et la sentinelle
`[DONE]`.

### 11.6 Revue adversariale des lots L5/L14/L15 — 4 correctifs incomplets détectés

Les lots L5/L14/L15 ci-dessus ont été soumis à une **revue adversariale** (relecture
du code réel, chaque constat reproduit par exécution, pas par lecture seule). Elle a
trouvé quatre défauts : trois sont des correctifs **incomplets** de ces lots (le
mécanisme était écrit, mais un maillon de la chaîne le court-circuitait), le
quatrième un défaut **pré-existant** que L16 devait précisément traiter.

| Réf | Défaut | Cause | Correctif |
|---|---|---|---|
| **D3** (L5) | `output_item.added` livrait l'item **complet** | l'émetteur passait `{**item}` ; un client qui amorce son accumulateur avec `.added` puis ajoute les deltas obtenait tout **en double** | `_empty_item_for_added()` : identité conservée, contenu vidé ; `encrypted_content` réservé à `.done` (B7 réellement tenu) |
| **D2** (L14) | la limite disparaissait à l'étage suivant | P2 écrivait `max_completion_tokens`, puis `_chat_to_responses_request` ne relisait que `max_tokens`/`max_output_tokens` → **aucune** borne sur le wire | lecture des **trois** formes, priorité historique inchangée |
| **D1** (L15) | `store`/`truncation` n'atteignaient pas le wire sur `/v1/responses` | `openai_responses_to_anthropic` (P6) ne les copiait pas, donc le relais recevait une source **déjà vide** | relais dans le handler, au seul endroit qui connaît la destination (`/responses`), pour ne pas envoyer ces clés à un upstream Anthropic |
| **D4** (L5) | `stream: false` recevait du **SSE** | deux sites de repli (clés en pause) émettaient sans consulter le mode demandé | ces sites respectent désormais le mode : JSON si `stream: false` |
| **L16/A4** | `ensure_min_tokens` **abaissait** une limite | `current` retenait la première forme vue ; si elle était sous le minimum, **les deux** champs étaient ramenés au minimum — `{max_output_tokens:16, max_tokens:100000}` → `100000` détruit, ramené à 256 | relèvement **par champ**, jamais d'abaissement |

Enseignement à retenir pour les prochains lots : **un correctif de conversion se
vérifie sur la chaîne complète, pas sur le helper**. D1, D2 et D3 étaient tous les
trois corrects *au niveau du helper testé* et neutralisés *un maillon plus loin* —
c'est pourquoi les tests unitaires existants restaient verts. Les nouveaux tests
`test_review_findings_d1_d4.py` vérifient donc le **câblage** (bout en bout via
`TestClient`), et le mutation-test a servi de critère d'acceptation : retirer
l'appel du handler doit faire rougir la suite. Le premier jet du test D1 ne le
faisait pas (il appelait le helper directement) — il a été remplacé pour cette
raison exacte.

### 11.7 Décision produit : « thinking sans niveau » = maximum du modèle

**Question tranchée** (décision utilisateur, 2026-09-10) : que faire quand un
client demande du raisonnement **sans nommer de niveau** — `thinking:
{type:"adaptive"}` ou `{"type":"enabled"}` sans `budget_tokens` ?

**Décision** : on fournit **le maximum que le modèle cible sait faire**, adapté
à chaque modèle — et non un défaut arbitraire.

| Modèle | Plafond (`effort_caps`) | Niveau retenu |
|---|---|---|
| `deepseek-v4-flash` | max | `max` |
| `muse-spark-1.3-contributor` | xhigh | `xhigh` |
| `glm-5-air`, modèle inconnu | high | `high` |

**Implémentation** (`config/effort_policy.py`) : `extract_requested_level`
retourne la sentinelle `MODEL_MAX` au lieu de `DEFAULT_LEVEL`, et
`resolve_effort` la traduit via `max_level_for_model(model)`. La sentinelle ne
peut pas fuir : elle n'existe qu'entre ces deux fonctions (vérifié sur P2/P4/P6).

**Ce qui ne change pas** : un niveau **explicite** garde toujours la main
(`output_config.effort: low` reste `low`, jamais relevé au maximum) et reste
borné par le plafond du modèle. Un budget explicite reste converti par
`BUDGET_TO_LEVEL_TABLE`. L'absence totale de demande d'effort ne déclenche
toujours aucun raisonnement.

**Conséquence** : la fixture `multiturn_thinking_strip.json` passe de `xhigh` à
`max` (régénérée en §11.5, diff relu : seule la valeur `expected.reasoning_effort`
change). Le comportement « `high` par défaut » que documentait le hotfix
précédent est **abrogé**.

### 11.8 Deux anomalies hors inventaire, trouvées par le lot L7

Ces deux-là ne figuraient dans **aucun** axe de la matrice. Elles sont sorties en
traversant le handler jusqu'au fil — précisément ce que les lots précédents ne
faisaient pas. Détail complet au rapport §3.6 et §3.7.

- **A24 — P4-stream renvoyait 0 octet en silence.** `_anthro_to_oai_stream`
  (`opencode.py:12569`) **assigne** `anthro_body` (L12578 jambe free, L12732
  retour payant) sans le déclarer `nonlocal` : Python en fait une locale, donc
  la première lecture (L12576) levait `UnboundLocalError`, **avalée** par le
  `except Exception` de L13052. Le client recevait un `text/event-stream` vide,
  sans erreur. Pré-existant (`HEAD` a la même structure), et invisible parce
  qu'**aucun test ne mentionnait `_anthro_to_oai_stream`**. **CORRIGÉE** :
  ajout d'`anthro_body` à la déclaration `nonlocal` (`opencode.py:12570`).
  Vérifié par analyse AST **et** bytecode (`co_freevars`), reproduction isolée,
  et mutation. Les 3 tests P4-stream sont désormais de simples tests de
  régression (plus de `xfail`) : observés **rouges avant**, **verts après**, et
  de nouveau rouges si on retire `anthro_body` du `nonlocal`. Un balayage AST de
  tout le fichier confirme que c'était le **seul** cas réel.

  > **Correction d'une erreur de conduite.** Le tour précédent avait livré A24
  > comme « hors périmètre, diagnostiqué mais non corrigé », en s'appuyant sur
  > un commentaire auto-écrit (« L7 n'a pas mandat de modifier `opencode.py` »).
  > C'était **faux** : le plan liste explicitement `opencode.py` dans son
  > périmètre (ligne 4), aucun document ni consigne ne l'exclut, et des lots
  > antérieurs (L9) l'avaient déjà modifié. La contrainte avait été inventée
  > puis présentée comme une règle du projet. A24 est donc corrigée ici.

- **A25 — nom de fichier des documents perdu en silence.** Le convertisseur
  lisait `block.get("name")` pour nommer un document, alors que le champ client
  est **`title`** (vérifié contre `DocumentBlockParam` du SDK :
  `source, type, cache_control, citations, context, title` — pas de `name`). La
  condition était donc toujours fausse : `rapport.pdf` devenait
  `document.pdf`. 3 sites corrigés (document base64, document texte, document
  dans `tool_result`). Trouvée en **refusant d'accepter une assertion
  affaiblie** dans le test de bout en bout : `"filename" in serialized` (la clé)
  avait remplacé `"rapport.pdf" in payload` (la valeur). Le test existant
  `test_p2_document_name_becomes_filename` **verrouillait le bug** en testant un
  corps non conforme (`name`), donc jamais envoyé par l'API.

### 11.9 L13 — contrat `reasoning_content` + repli upstream strict — **FAIT**

- **Contrat documenté et mesuré** : `docs/reasoning-content-contract.md`. Les 3
  contrats (Chat / Anthropic / Responses) y sont établis par sonde, pas par
  narration. Points clés : `reasoning_content` n'est dans **aucune** spec
  OpenAI (convention vendeur DeepSeek/GLM/Kimi) ; la signature Anthropic ne
  franchit **jamais** la frontière vers Chat ; le champ n'est **jamais** émis
  vers Anthropic (le raisonnement y devient un bloc `thinking` natif) ; un
  historique « thinking sans texte » reçoit le placeholder `" "` (préserve la
  position du champ).
- **Écart comblé** : le filet « retry-once » existait pour les items `reasoning`
  de `/responses` mais **pas** pour `reasoning_content` sur
  `/chat/completions`. Un historique porteur partait donc sans aucun repli vers
  une cible Chat stricte. Le garde existant exigeait `oai_body.get("input")`
  (clé propre à `/responses`), donc la branche Chat n'était **jamais** atteinte.
- **Correctif** : `anthropic_to_openai` pose le marqueur interne
  `_has_synthetic_reasoning_items` **ssi** un message porte du
  `reasoning_content` ; `_serialize_json_body` le retire avant le wire ; le
  handler, sur 400/422, retire le champ de tous les messages et rejoue **une
  seule fois**. Sans raisonnement, aucun marqueur → **aucun** rejeu des requêtes
  ordinaires. Politique : le raisonnement est un **enrichissement, jamais un
  bloquant** — on dégrade (perte de mémoire du raisonnement) plutôt que
  d'échouer.
- **Verrous** : 5 tests `test_l13_*` dans `test_e2e_protocol_matrix.py`
  (transport, non-sérialisation du marqueur, repli 400, retry unique, pas de
  rejeu sans raisonnement). Vérifiés par **mutation**.
- **Volet B4 (restore des noms d'outils)** : **vérifié** — `restore_tool_name`
  est bien appliqué sur les **4 voies de retour** (3 sites d'appel dans
  `mapping.py` : `_responses_to_chat_response` L3466, `_responses_to_anthropic_response`
  L3528, `_responses_sse_to_chat_deltas` L3725) + le streaming via
  `state.tool_name_map`. Les tests `test_tool_name_sanitization.py` couvrent la
  restauration côté Chat et côté Anthropic.
- **✅ Écart trouvé en vérifiant B4 (résidu A8) — CORRIGÉ depuis par le lot L4** :
  le volet B4 portait sur la **restauration** au retour, pas sur la **sanitize**
  à l'aller — et cette dernière était **asymétrique entre chemins**.
  `sanitize_tool_names` n'était appelé que sur les chemins **Responses**
  (`_sanitize_native_responses_request`, `_chat_to_responses_request`). Sur les
  chemins **Chat**, un nom d'outil > 64 caractères partait **tel quel** vers
  l'amont : mesuré, un nom de 80 car. arrivait à **80 car.** côté Chat
  (`anthropic_to_openai` ne posait aucun `_tool_name_map`). Le sens P4 vers
  Anthropic était déjà correct (limite 200 : un nom de 80 y passe légitimement).
  **Risque identifié, puis mesuré et écarté** : un upstream Chat strict *pouvait*
  rejeter un nom > 64 en 400. La mesure sur le fil ne le confirme **pas** : cet
  amont ne rejette ni ne tronque (nom de 72 car. rendu identique, 3/3, sur un
  chemin sans sanitize ni restauration). Le correctif se lit donc comme une
  **mise en conformité** au contrat OpenAI/Chat, et non comme la réparation d'une
  panne reproduite — détail mesuré en §9 du rapport.
  **Correctif (lot L4)** : `_sanitize_chat_tools` sanitize à l'aller en
  **réutilisant** `sanitize_tool_names` (une seule source de vérité), l'historique
  `tool_calls[].function.name` et le `tool_choice` nommé suivent le rename, la
  `name_map` remonte au handler, et la restauration couvre le non-stream **et** le
  streaming. Verrouillé par 7 tests dédiés (5 `test_a8_*`, l'axe P2 renommé et la
  réversibilité) + 1 test e2e de flux, et
  **mutation-testé 6/6** (chaque élément neutralisé fait rougir son test).
- **Vérification en conditions réelles — PARTIELLEMENT CONCLUANTE.** Le proxy en
  cours sur `127.0.0.1:4000` est joignable et a reçu de vraies requêtes. Sur
  `muse-spark-1.3-contributor` et `mimo-v2.5` (P2), des **`200`** ont été
  obtenus. Résultat exploitable, après vérification des dumps du proxy :
  - ✅ **Sur P2 vers Chat, le nom de 80 caractères partait NON sanitizé** vers un
    vrai amont — observé dans `logs/free400_msg_aa3ac6d9fd40-31e.json`. C'est ce
    diagnostic, relevé **avant correction**, qui a établi le défaut sur le fil et
    motivé le lot L4.
  - ✅ **Sur la jambe Responses, la sanitize à 64 fonctionne en production** : le
    nom émis est `a`×57 + `-86f336`, digest `sha1("a"×80)[:6]` recalculé
    **identique** à la sortie de `sanitize_tool_names`.
  - ⚠️ **Piège de lecture, corrigé** : les `200` ne prouvent **pas** qu'un amont
    accepte 80 caractères — sur cette jambe le nom était **sanitizé à 64**.
  - ✅ **Tranché depuis, dans le sens du NON-rejet** : cet amont ne rejette **pas**
    un nom > 64, et ne le tronque pas non plus. Mesuré sur le seul chemin sans
    ambiguïté — un modèle free à endpoint non-`/responses` (`mimo-v2.5-free`), où
    `opencode.py:6197-6199` recopie le corps sans conversion, sans sanitize et
    sans restauration : nom de **72 caractères** envoyé, `200`, nom rendu
    **identique, 3/3 rondes**, témoin court vert à chaque ronde. Les échecs
    observés gardent leurs autres causes (`401 CreditsError` payant,
    `400 MissingSessionID` free, `403 DataPolicyError` d'opt-in).
  - ⚠️ **Deux découvertes annexes de cette mesure.** (a) L'amont n'accepte que
    `tool_choice: "auto"` : `required` et les formes nommées sont refusés en 400 —
    limitation de l'amont, **pas** un défaut de conversion, et A8 en est disculpé
    par construction (la branche passthrough n'appelle jamais `anthropic_to_openai`).
    (b) **Lacune résiduelle déclarée** : sur la jambe free à endpoint
    non-`/responses`, les noms > 64 partent **non sanitizés** — même classe d'écart
    que A8, sans effet observable ici puisque l'amont tolère.

### 11.10 Reste à faire

- **L4 — livré intégralement.** `tests/test_tools_matrix.py` compte **30 cas**
  (13 fonctions, 6 chemins). L'écart **A8 résiduel** décrit ci-dessus (nom
  d'outil > 64 caractères vers une cible Chat sur **P2**) est **corrigé** :
  sanitize à l'aller (`_sanitize_chat_tools`, réutilisant `sanitize_tool_names`),
  remap de l'historique et du `tool_choice`, remontée de la `name_map` au handler,
  et restauration non-stream **et** streaming. L'ancien `xfail(strict=True)` a été
  levé (passé en XPASS au câblage) et le golden `p2_tools_long_name_strict_schema`
  régénéré. Le volet documents reste couvert par
  `tests/test_documents_contract.py`, pas par un `test_documents_matrix.py`
  séparé.
- **`docs/conversion-matrix.md`** — ✅ l'écart **A8** y est désormais documenté
  comme **corrigé** (bloc « Noms d'outils : 200 vs 64 — écart A8 CORRIGÉ », qui
  conserve le diagnostic sur le fil et le digest vérifié, et signale que le golden
  A8 est le seul des 46 à avoir changé). ✅ **Tranché — la ligne « PERDU
  silencieusement » pour l'image, demandée par L4.** Elle **existait bien**, mais
  dans une version antérieure de ce document : c'est la refonte de
  `docs/conversion-matrix.md` au commit `b6c6543` qui l'a **supprimée** sans la
  remplacer, d'où un grep vide sur le main. Elle est désormais **rétablie,
  corrigée** : « PERDU silencieusement » était **faux pour `base64` et `url`** (les
  octets traversent intacts — ni fetch, ni ré-encodage) et **vrai pour un cas
  étroit** — un `source.type` `file` (`file_id`) devient le texte `[image:file]`
  (`mapping.py:1165-1169`) sans que le client en soit informé. La même correction a
  été portée dans `docs/clients-compat.md`, qui affirmait la version générale
  fausse. Détail (trou latent en direction réponse, chemins sans test P1/P3) :
  `docs/conversion-matrix.md`, section « Mapping champs ».
- **L16** — le volet A4 (`ensure_min_tokens`) est **fait** (voir §11.6) : le
  relèvement se fait par champ et n'abaisse plus jamais une limite. Le volet
  Anthropic reste sans objet (A23 structurellement inviolable, plus aucun
  `budget_tokens` émis).
- **Limites de priorité — TRANCHÉ** — `ensure_min_tokens` (corps client) et
  `_set_output_token_limit` (convertisseur) ont des priorités différentes entre
  `max_tokens` et `max_output_tokens`. Le premier relève désormais chaque champ
  séparément, donc le conflit d'**écrasement** est éliminé. Pour la **lecture**,
  la précédence est fixée et documentée dans `_set_output_token_limit` :
  `max_tokens` → `max_completion_tokens` → `max_output_tokens`, première forme
  valide gagnante (`max_tokens` est canonique côté Anthropic et historique côté
  Chat). Un client envoyant deux formes divergentes suit `max_tokens`.

### 11.11 A26 — parité de protocole de la jambe free (mesurée, corrigée)

**Anomalie hors inventaire**, trouvée en reprenant la matrice L1 ligne par ligne :
la colonne P1 du §6 annonçait « natif » et elle est **fausse**.

**Défaut mesuré** (11/09/2026, proxy `:4000`) — sur `/v1/messages`, quand le modèle
**payant** routé déclare `protocol: anthropic` **et** que son équivalent free
utilise `/chat/completions` :

- le corps Anthropic partait **verbatim** vers un endpoint Chat — un endpoint Chat
  ne lit pas le champ `system` top-level (**system prompt perdu**) et n'attend pas
  `tools[].input_schema` ;
- la réponse Chat était rendue **verbatim** au client Anthropic, sous **HTTP 200**
  (`choices` en non-stream, `chat.completion.chunk` en stream) : ni `content`, ni
  `stop_reason`, ni `usage.input_tokens`.

Cas vivant : route `haiku` → `minimax-m2.5` (`protocol: anthropic`) →
`mimo-v2.5-free` (endpoint Chat). **Contrôle décisif** : la route `opus` →
`kimi-k2.6` (`protocol: openai`) atteint **le même** modèle free par le **même**
endpoint et fonctionnait ; le témoin de prompt système est honoré sur `opus` et
ignoré sur `haiku`. C'est donc la déclaration de protocole du modèle **payant**, et
non le modèle free, qui déclenchait le défaut.

**Pourquoi la matrice L1 ne l'avait pas vu** : la jambe free n'était exercée que
sur des chemins à protocole `openai`, où la recopie verbatim est correcte, et la
cellule P1 décrivait le chemin « natif » sans franchir la bascule free. Un test vert
sur un chemin voisin ne couvre pas la cellule — cf. la règle de provenance du §4.

**Correctif** : conversion dans les deux sens, non-stream **et** stream.
`_try_free_model_first` : `anthropic_to_openai` à l'aller (`opencode.py:6205`),
`openai_to_anthropic` au retour (`opencode.py:6669`). `anthropic_stream` :
conversion du corps avant envoi (`opencode.py:9190`) puis nouveau convertisseur de
flux `app/protocol/chat_sse_to_anthropic.py` pour le relais, avec vidage de fin de
flux (clôture Anthropic émise même si l'amont Chat se tait sans `[DONE]`).

**Verrous** : `tests/test_free_leg_protocol_parity.py` (5 cas),
`tests/test_chat_sse_to_anthropic.py` (48 cas),
`test_e2e_protocol_matrix.py::test_free_model_subpath_p1_stream_converts_chat_to_anthropic`,
et le golden `docs/v1-response-golden/p1_free_leg_tool_name_restored.json` (le retour
restaure le nom raccourci — 64 car. — vers sa forme longue — 124 car.).
**Mutations** : 4/4 mordent (aller et retour, non-stream et stream), fichier
restauré à l'identique (sha256 vérifié).

**Défaut dans le verrou lui-même, corrigé** — `_run_free` vidait `oc.FREE_MODEL_MAP`
**en place** ; or c'est l'objet de `config.settings`, donc la table live était détruite
pour toute la session de tests et le gate complet rougissait sur
`test_go_only_routing.py::test_live_models_muse_spark_13_free_map`
(`KeyError`). Remplacé par `monkeypatch.setattr` (liaison restaurée en sortie).
Causalité prouvée par mutation inverse. Détail et leçons : rapport §3.10.

**Reste ouvert, déclaré** : le même schéma subsiste sur **P4 stream**
(`_anthro_to_oai_stream`, `opencode.py:12783`) — le consommateur attend de
l'Anthropic, l'endpoint free rend du Chat. Le test existant
`test_free_model_subpath_p4_stream` ne le voit pas : son stub renvoie
`ANTHRO_SSE_LINES`, une forme que l'endpoint free réel ne produit pas.

### 11.12 L1 — reprise de la matrice de vérité, ligne par ligne

**Méthode.** Chacune des 6 colonnes du §6 a été reprise axe par axe, en confrontant la
cellule du plan à ce que le code fait réellement, avec un `file:line`. Vocabulaire de
provenance strict appliqué à chaque affirmation : **TESTÉ** (un test nommé verrouille),
**MESURÉ** (mesure traçable : golden, log, requête réelle), **DÉDUIT** (lecture de code
seule), **NON TESTÉ** (rien ne l'établit). Une cellule n'est « couverte » que si un test
**passe l'axe** : un test qui appelle un convertisseur sans l'axe ne compte pas.

**Résultat.** Le lot L1 tel que livré était **incomplet et, par endroits, faux**. Les
corrections sont portées au §6 ; leur nature :

| Chemin | Cellules fausses | Cellules imprécises | Nature du constat |
|---|---|---|---|
| **P1** | **toute la colonne** (« natif ») | — | Falsifiée par A26 (§11.11) : défaut **mesuré en réel**. Corrigé. |
| **P2** | axe 8 (`cache_control` tools « perdu (A5) ») ; axe 9 (« 7 sites, 0 test (A6) ») ; axe 16 (`openai_stream`) | axes 1, 2, 5, 7, 10, 15 | A5 et A6 étaient **corrigées** (L3 livré, tests présents) ; le nom du générateur de flux était faux. |
| **P3** | axe 1 (« 4ᵉ mapping, sans caps ») ; axe 2 (« par famille ») ; axe 3 (« non (A4) ») | axe 12 (trompeuse) | A1 et A4 étaient **déjà corrigées en HEAD** par le lot L2 : les cellules décrivaient le code d'avant L2. |
| **P5** | 11 cellules (confusion P5-Chat / P5-Responses, `tool_choice` « dict↔str », ordre des blocs « à tester », images, documents-URL…) | — | Colonne établie par analogie avec P2/P6 : les deux sous-chemins P5 ne se comportent pas pareil. |
| **P4** | **5 cellules fausses** (effort « dict en dur (A1/A2) », budget « 4096/10000/16000 », `redacted_thinking` « cache borné », noms longs « sanitize/restore », documents « PDF-URL seul ») | 5 imprécises (dont **signature locale forgée émise vers l'amont**, `cache_creation` jamais remonté) | Reprise complète. **Défaut mesuré** : jambe free Chat → **0 octet** au client. |
| **P6** | **2 cellules fausses** (« perdu (A3) » — en fait corrigée et testée ×3 ; « remap » — inexistant) | 8 imprécises (dont ordre des blocs déjà verrouillé par golden, `cache_control` en **perte muette**, garde orphelins **contournée**) | Reprise complète. A11 à moitié corrigée : séquence conforme, émission bufferisée. |

#### 11.12.1 Trous ouverts trouvés par la reprise (déclarés, non corrigés)

1. **A8 ouvert sur P3** — `sanitize_tool_names` n'est appelé **nulle part** dans
   `opencode.py` : un nom d'outil > 64 caractères part verbatim vers un amont Chat sur
   P3. Le lot L4 (`58f788e`) ne couvre que P2 et les convertisseurs.
2. **A8 — retour P5 non restauré** — `openai_chat_to_responses`
   (`app/protocol/mapping.py:2710`) n'accepte pas de `name_map` et recopie
   `fn.get("name")` tel quel (`mapping.py:2750`) ; ses 4 sites d'appel
   (`opencode.py:13763`, `13946`, `14063`, `14176`) ne passent aucune table → le client
   reçoit le nom **raccourci**.
3. **P4 stream — même schéma que A26** — `_anthro_to_oai_stream` (`opencode.py:12783`)
   attend de l'Anthropic d'une jambe free qui rend du Chat ; le test
   `test_free_model_subpath_p4_stream` ne le détecte pas (son stub est de forme
   anthropic).
4. **P2 → `/responses`, chemin entier absent de la matrice** — via `custom_routes`,
   10 des 11 cibles de `muse-spark-1.3-contributor` partent vers `/responses`
   (`opencode.py:9826-9827`, retour `10121`).
5. **Axe « jambe free » absent** — la décision prise côté payant (plafond d'effort,
   profil de schéma, `max_completion_tokens`) n'est **pas recalculée** après la bascule
   (`opencode.py:6208-6209`, `10173-10191`).
6. **Garde `supports_cache_control` absente** — `mapping.py:1363` et `1390` ne la
   portent pas : un `cache_control` client sur un bloc image/`tool_result` peut fuiter
   vers un amont `glm-5*`. Non testé.
7. **A11 partiel** — la séquence SSE de P5/P6 est conforme et verrouillée (49 cas,
   `tests/test_responses_stream_contract.py`), mais l'émission est **entièrement
   bufferisée** (`opencode.py:14009-14070` consomme tout l'amont puis renvoie
   `Response(content=sse_body)`) : le TTFB vaut la durée totale de génération.
8. **`cache_control` → `prompt_cache_breakpoint` est un no-op** —
   `_cache_control_to_openai_breakpoint` (`mapping.py:621-640`, docstring « Réservé —
   NON ÉMIS ») ; `cache_write_tokens` n'existe que dans le plan (§209, §329) : le lot
   L15 ne l'a pas fait.
9. **`thinking` top-level jamais recopié par P5** — `openai_responses_to_anthropic`
   (`mapping.py:2441-2576`) ignore `thinking.budget_tokens`.
10. **Zéro golden P3** — les 47 goldens sont `p1_/p2_/p4_/p5_/p6_` ; le seul `p1_`
    (`p1_free_leg_tool_name_restored.json`) a été ajouté pour A26, et il n'existe
    **toujours aucun golden P3**.

#### 11.12.2 Trouvailles propres à P4 et P6

**P4 — le défaut d'A26 en streaming, mesuré.** La colonne P4 du §6 annonçait
`_anthro_to_oai_stream` sans dire que la **jambe free** rend **0 octet** au client : le
swap free (`opencode.py:12736-12744`) ne convertit ni l'aller ni le retour quand
l'endpoint free est un endpoint Chat, alors que P1 le fait désormais
(`opencode.py:9193-9203`). Le non-stream, lui, est correctement corrigé (le paramètre
`protocol="anthropic"` est partagé par P1 et P4 via `opencode.py:12566-12573`).
`test_free_model_subpath_p4_stream` **ne détecte pas** ce défaut : il stubbe
`ANTHRO_SSE_LINES` (`tests/test_e2e_protocol_matrix.py:909`) — une forme que l'endpoint
free réel ne produit pas — et n'asserte **aucun contenu** (`:914-921`), contrairement à
`test_p4_chat_to_anthropic_stream:817`.

**P4 — signature locale forgée émise vers l'amont.** Le `reasoning_content` d'un
historique multi-tours devient un bloc `thinking` portant une **signature HMAC locale
forgée**, et `strip_synthetic_thinking` n'est appelé que par `/v1/messages`
(`opencode.py:8888`, contre `:11433-11437` pour P4) : le bloc forgé part donc vers
l'amont Anthropic. Le golden `p4_thinking_strip_local_signature.json:38-43` verrouille
même son **présence**, malgré son nom. Non mesuré (un 400 amont est attendu, non
constaté).

**P6 — garde orphelins contournée.** `_drop_orphan_responses_input` filtre bien
`body["input"]` (`opencode.py:13399-13407`), mais `anthro_body` n'est **jamais
reconstruit** après (la branche est un `pass`) : le `tool_result` orphelin atteint quand
même l'amont Anthropic. DÉDUIT.

**P6 — A3 est corrigée.** `openai_responses_to_anthropic` délègue à `resolve_effort` et
écrit `output_config.effort` + `thinking.adaptive` (`mapping.py:2604-2621`,
`_apply_anthropic_effort` `:470-500`), verrouillé par trois tests
(`test_effort_mapping.py:298`, `test_effort_policy.py:241`,
`test_protocol_matrix.py:139`).

**Trou non déclaré, P4 comme P6** : `cache_creation_input_tokens` est jeté côté réponse
(`mapping.py:2315-2317` pour P4, `:2682-2698` pour P6) et `cache_write_tokens` n'existe
**nulle part** dans le dépôt — seulement dans le plan.

#### 11.12.3 Limites de cette reprise

Elle est **documentaire et statique** : elle prouve ce que le code fait (lecture +
tests nommés), pas ce que l'amont réel accepte. Les mesures bout en bout disponibles
sont : A26 (§11.11), le lot A8, et le **0 octet** de la jambe free de P4 en streaming.

Trois réserves explicites :
1. **Aucun test n'exerce la jambe free de P6** : les E2E P6 stubent
   `_try_free_model_first → None` (`tests/test_e2e_protocol_matrix.py:448`) ; le
   correctif y est partagé mais **non mesuré sur P6**.
2. Le garde axe 14 de `tests/test_protocol_matrix.py:614` ne vérifie qu'une
   **sous-chaîne de source** (`assert "responses_stream_events(" in src`) : il **ne passe
   pas l'axe** de l'incrémentalité.
3. Le cap d'effort par défaut (`effort_caps.default: high`) n'a pas été exécuté : sa
   valeur exacte est **DÉDUITE**, non mesurée.

---

### 11.13 P4 en streaming, A27, et validation en réel (14/09/2026)

Cette section clôt les deux points restés ouverts : le **0 octet** de P4 en streaming,
et l'absence de mesure sur un proxy vivant.

#### 11.13.1 P4 streaming — 0 octet (mesuré, corrigé)

Deux défauts distincts, tous deux sur `/v1/chat/completions` avec un modèle payant routé
à `protocol: anthropic` dont l'équivalent free est un endpoint **Chat** (cas vivant :
route `haiku` → `minimax-m2.5` → `mimo-v2.5-free`) :

1. **Aller** (`opencode.py:12742`) : le corps converti en Anthropic partait vers un
   endpoint Chat — `system` top-level et `input_schema` jamais lus. Corrigé en renvoyant
   le **corps client** (déjà de forme Chat, cf. `anthro_body = openai_to_anthropic_request(body)`
   en `opencode.py:12533`), augmenté de `stream_options.include_usage`.
2. **Retour** (`opencode.py:13026`) : le flux Chat revenait à un parseur qui n'exploite
   que des événements Anthropic (`if not line.startswith("data:"): continue`, puis
   `ev["type"]`). Aucune ligne exploitable ⇒ **0 octet** sous HTTP 200. Corrigé par une
   conversion `Chat SSE → Anthropic SSE` (`app/protocol/chat_sse_to_anthropic.py`) insérée
   avant le parseur, avec un état **frais par tentative**.

Le fanion `_free_chat_stream` est déclaré **avant** la bascule free (même classe de piège
qu'A24, §11.6) et remis à `False` au repli payant.

**Un piège rencontré pendant le correctif** : `chat_sse_to_anthropic_events` rend des
`str`, pas des `bytes` ; mon `.decode()` levait `AttributeError`, **avalé par le
`except Exception` du handler** — le symptôme redevenait « 0 octet sous HTTP 200 »,
indistinguable du défaut d'origine. C'est la démonstration que le `except Exception`
large est le véritable amplificateur de ces bugs : il transforme une erreur de
programmation en défaut métier silencieux.

#### 11.13.2 A27 — 500 au lieu de 503 quand toutes les clés Anthropic sont en pause

Mesuré en réel : toutes les clés Anthropic en pause ⇒ `_get_auth_headers("anthropic")`
rend `None` **sans** lever `AllKeysPausedError` ; la jambe free échoue (429) ; le repli
payant propage ce `None` ; `opencode.py:12647` faisait alors
`a_headers.get("x-api-key", "")` ⇒ `AttributeError: 'NoneType' object has no attribute 'get'`
⇒ **HTTP 500 « Erreur interne du serveur »**, là où la branche streaming rend un 503
propre (`opencode.py:12560`).

Corrigé par un garde 503 (et un `account_alias` tolérant). **Le même motif existe à
l'identique dans un second handler** (`grep 'account_alias = _alias_for_key(a_headers.get'`
⇒ 2 occurrences, une seule corrigée) : ce jumeau reste **non corrigé et non mesuré**.

#### 11.13.3 Validation en réel (proxy `:4000`, avant/après redémarrage)

Le proxy tournait depuis le 11/09 19:48 sur le code **d'avant** les correctifs (commits
du 12/09 00:59→02:16). Sonde : `/v1/messages` et `/v1/chat/completions`, `model: haiku`,
plus le témoin `opus`.

| Chemin | Avant redémarrage | Après |
|---|---|---|
| P1 non-stream `/v1/messages` | **500** « Erreur interne du serveur » | 200, corps **ANTHROPIC** `content='Bonjour'` |
| P1 stream `/v1/messages` | 200, **16 007 o de Chat non converti** (`chat.completion.chunk`, 0 `message_start`) | 200, **ANTHROPIC** (`message_start` + `content_block_delta`, aucun `chat.chunk`) |
| P4 non-stream `/v1/chat/completions` | **500** (A27) | 200, `choices[0].message.content='bonjour'` |
| P4 stream `/v1/chat/completions` | 200, **0 octet** | 200, **7 559 o de Chat**, texte reçu |
| Témoin `opus` (protocole `openai`) | 200 ANTHROPIC `'Bonjour'` | 200 ANTHROPIC `'Bonjour'` |

Le témoin `opus` atteint **le même** modèle free et **le même** endpoint et n'a jamais été
cassé : c'est la déclaration de protocole du modèle **payant** qui déclenchait les trois
défauts, pas la jambe free elle-même.

Sur P4, la sortie `chat.completion.chunk` est **le format correct** (le client a appelé
`/v1/chat/completions`) ; sur P1, le même format était le symptôme du défaut (le client
avait appelé `/v1/messages`).

**Réserves** : cette validation est ponctuelle (une requête par chemin, un seul
fournisseur free, stations en 429 intermittents). Elle ne remplace pas les tests
hermétiques, elle les confirme.

#### 11.13.4 Preuves de non-régression

* `tests/test_e2e_protocol_matrix.py::test_free_model_subpath_p4_stream` : le test
  existant stubait `ANTHRO_SSE_LINES` — une forme que l'endpoint free **ne produit
  jamais** — et n'assertait aucun contenu : il **verrouillait** le défaut (classe A25,
  §11.7). Réécrit avec `CHAT_CHUNK_LINES`, assertions de contenu et assertion d'aller Chat.
* Mutations : 2/2 mordent sur P4 (aller, retour) ; fichiers restaurés à l'identique (sha256).
* 294 tests des chemins voisins verts ; régénération golden : aucun des 47 goldens
  préexistants modifié.

> ⚠️ **Correction (15/09/2026)** — la phrase « 1/1 sur A27 (retour au code d'origine) »
> était **fausse** et est retirée : aucune des trois mutations d'A27 (dépouillement seul,
> garde seul, retour COMPLET au code d'origine) ne fait échouer les témoins. Voir §12.1.

## 12. Trous déclarés de l'audit — corrections du 15/09/2026

Cette section est le registre de vérité des 13 trous déclarés dans la synthèse de l'audit
(§9 « ce qui reste ouvert » du rapport). Chaque entrée dit : le défaut tel qu'il a été
**mesuré** (et non tel qu'il avait été supposé), le correctif, le témoin, la **mutation**
(correctif neutralisé → le test doit rougir), et ce qui reste ouvert.

Vocabulaire inchangé : `TESTÉ`, `MESURÉ`, `DÉDUIT`, `NON TESTÉ`.

### 12.1 A27 (jumeau `/v1/messages`) — la jambe free rend des en-têtes `None`

**Défaut mesuré.** Trace de production : `AttributeError: 'NoneType' object has no
attribute 'get'` dans `chat_completions`, remontée en **HTTP 500 nu**.

**Mécanisme réel** (la première rédaction de ce plan accusait `_get_auth_headers`, c'était
faux) : `_try_free_model_first` rend **`None`** dans le créneau des en-têtes sur son chemin
nominal de hedge — le code le dit lui-même (`resp = resp_headers = None`, puis le
commentaire « resp_headers stays None for hedge »). **Huit sites** de dépouillement
écrivaient `resp, headers, _actual_model, _actual_ip = free_result`, écrasant des en-têtes
payants **valides** par ce `None`. `_get_auth_headers` ne rend jamais `None`
(`entry.get("api_key", API_KEY)`), il n'était pas en cause.

**Correctif.** Les 14 sites de dépouillement utilisent désormais `_` pour ce créneau
(homogénéité) ; le contrat est écrit dans la docstring de `_try_free_model_first` ; le
garde `503` (message « plus de clé et pas de free model ») est conservé en **défense en
profondeur**, avec un commentaire corrigé.

**Limite de preuve, dite franchement.** Les deux témoins
(`test_p4_nonstream_jambe_free_sans_entetes_ne_fait_pas_500`,
`test_p1_nonstream_jambe_free_sans_entetes_ne_fait_pas_500`) passent et assertent le
comportement observable — **mais ils ne mordent pas** : ni la mutation du dépouillement
seul, ni celle du garde seul, ni le retour **complet** au code d'origine ne les rendent
rouges. Sonde à l'appui : le site `opencode.py:12650` **est** atteint, le stub **est**
appelé, et le `503` observé vient du chemin d'erreur normal
(`« All API keys exhausted (rate limited) »`), pas de la lecture fautive. Conclusion
assumée : la correction d'A27 repose sur la **mesure live** et la lecture du code, pas sur
un test qui mord. Classe A25 inversée : ici le harnais est *trop permissif* pour
reproduire, pas complice.

### 12.2 `supports_cache_control` — deux sites sur quatre sans garde (TROU 8)

**Défaut MESURÉ.** Dans `anthropic_to_openai` (`app/protocol/mapping.py`), quatre sites de
report du `cache_control` sur le message converti. Deux portent le garde (`L1377`, `L1399`),
deux non (`L1363`, `L1390`) — alors que la variable est définie dans la même fonction
(`supports_cache_control = not model.startswith("glm-5")`). Un modèle sans support recevait
donc un `cache_control` par ces deux branches. Asymétrie entre sites jumeaux, pas décision.

**Atteindre ces sites est le point dur** — et c'est ce qui a fait échouer ma première
version du témoin : elle passait par les sites **déjà gardés**, la mutation ne mordait pas.
Les deux lignes vivent dans des branches qui exigent `tool_calls` **et** `not is_asst` : il
faut un message **utilisateur** contenant un bloc `tool_use` (un `tool_use` remplit
`tool_calls` quel que soit le rôle, L1230-1244) ; `L1363` exige en plus `image_parts`.

**Correctif + témoin.** Garde ajouté aux deux sites ; témoins distincts par site dans
`tests/test_cache_control_support.py`. **Mutation** : neutraliser `L1363` fait rougir le
témoin « avec image », neutraliser `L1390` fait rougir le témoin « sans image » —
discrimination par site ; fichier restauré (sha256 identique).

### 12.3 `cache_control` → `prompt_cache_breakpoint` : décision tranchée (TROU 10)

**Défaut réel, mais documentaire.** `_cache_control_to_openai_breakpoint`
(`mapping.py:621`) est un no-op **assumé** (« Réservé — NON ÉMIS »), et argumenté : B3 place
`prompt_cache_breakpoint` sur les **content parts**, pas sur un message ni sur une
définition d'outil ; émettre au mauvais niveau risque un 400 pour un gain nul. Or la
docstring de `_carry_cc` (`mapping.py:1455`) affirmait le contraire : « **on émet AUSSI**
l'équivalent OpenAI réel ». Deux docstrings du même fichier se contredisaient.

**Correctif.** La docstring fausse est corrigée (elle dit maintenant la décision et sa
raison). Le comportement est verrouillé par
`test_cache_control_sur_outil_est_transporte_sans_inventer_le_champ_openai` : le breakpoint
de l'outil est transporté, `prompt_cache_breakpoint` n'est pas inventé.

**Reste ouvert, assumé** : la traduction par part (lot **L15**) n'est pas faite, et
`cache_write_tokens` n'est toujours remonté nulle part. Ce n'est pas un oubli : c'est un
travail rattaché à L15, refusé ici parce qu'un placement non conforme est pire que
l'absence.

### 12.4 « `thinking` top-level jamais copié » — RÉFUTÉ par la mesure (TROU 11)

**L'affirmation de l'audit est fausse.** Mesure sur `_anthropic_to_responses_request`
(constructeur de corps pour l'endpoint `/responses`) :

| entrée | sortie |
|---|---|
| sans `thinking` | pas de champ `reasoning` |
| `thinking: {type: disabled}` | pas de champ `reasoning` |
| `thinking: {type: enabled, budget_tokens: 256}` | `reasoning: {summary: auto, effort: low}` |
| `… budget_tokens: 8000` | `… effort: medium` |
| `… budget_tokens: 32000` | `… effort: high` |

Le `thinking` racine **est** converti, et le budget **pilote** l'effort. L'axe était donc
déjà couvert, sans témoin pour le verrouiller.

**Action.** `tests/test_thinking_effort_mapping.py` (8 cas) verrouille le comportement
mesuré, y compris l'échelle d'effort. La déclaration d'audit est retirée. Portée : ce
constructeur de requête uniquement — les autres chemins bâtissant un corps Responses ne
sont pas couverts par ce fichier.

### 12.5 P4 émettait une signature **forgée** vers l'amont (TROU 4)

**Défaut MESURÉ.** Le proxy fabrique un bloc `thinking` « synthétique » et le signe
localement (`_local_signature`). Sur P4, `strip_synthetic_thinking` n'était pas appelé :
le bloc partait **signé d'une signature locale** vers un amont Anthropic, en non-stream
(amont payant) **et** en stream (jambe free Anthropic). P1 le faisait (`L8898`), P4 non.

**Correctif.** Appel ajouté au **passage unique** des deux jambes P4
(`opencode.py:12543-12561`, juste après `anthro_body = openai_to_anthropic_request(body)`).

**Témoin + mutation.** `tests/test_p4_synthetic_thinking.py` (3 cas) ; correctif neutralisé
→ **2 rouges** avec le message exact du bloc forgé, tandis que le témoin P1 **reste vert**
(la morsure vise P4, pas le strip en général) ; sha256 restauré.

**Limite.** La mesure porte sur des seams amont doublés, pas sur un vrai 400 Anthropic.

### 12.6 P6 : garde orphelin contourné **et** signature forgée non retirée (TROU 5)

**Défaut MESURÉ (lecture du code, corroborée par le sous-agent du TROU 4).** Dans le
handler `/v1/responses`, `anthro_body` est construit (`L13474`, resynchronisé `L13487`)
**avant** la garde orphelin, qui filtre `body` (`L13492`/`L13494`). Le bloc qui suivait
(`L13495-13497`) ne faisait **rien** tout en ayant l'air de garder : `pass` et commentaire
« keep for completeness ». Un message `tool` orphelin partait donc vers l'amont Anthropic
dès que le client parlait Responses, alors que le même orphelin est écarté sur les autres
chemins (`L7219`, `L9934`, `L11445`).

**Correctif — et un premier correctif FAUX, ce qui est instructif.** J'ai d'abord appliqué
`_drop_orphan_tool_messages` au corps Anthropic. Le témoin écrit à ce moment-là a **échoué**,
et il avait raison : cette fonction travaille au format **Chat** (`role: tool` /
`tool_call_id`) et ne trouve donc rien dans un corps Anthropic — le correctif ne corrigeait
rien. Le vrai correctif **resynchronise** : `anthro_body` est reconstruit depuis le `body`
**déjà filtré**, exactement le motif utilisé deux lignes plus haut pour `web_search`.
`strip_synthetic_thinking` est en outre appelé sur le corps P6.

**Témoins (fermé).** `tests/test_p6_orphan_and_thinking.py`, fichier **autonome** (harnais
ASGI + doubles amont copié du témoin P4, pour ne pas dépendre du fichier E2E que le
sous-agent 13 tenait). Trois cas : orphelin non transmis, contre-témoin de contenu intact
(« question » doit survivre à la garde), et absence de signature locale. **Mutation** : le
bloc `pass` d'origine rétabli → le témoin rougit avec le message exact du `tool_result`
orphelin ; fichier restauré à l'octet (sha256 identique).

**Mesure honnête sur le `strip` P6.** Retirer l'appel au `strip` **ne fait pas rougir** les
témoins P6 (les témoins P4, eux, restent verts eux aussi) : cet appel est donc de la
**défense en profondeur, pas un correctif prouvé nécessaire**. Il est conservé parce qu'un
client peut renvoyer un historique qui a transité par P1/P4 et porterait alors une signature
locale, mais c'est une hypothèse, pas une mesure — dit tel quel.

### 12.7 Aucun golden P3 — fermé, et un défaut neuf mesuré au passage (TROU 12)

**Ce que la mesure a d'abord appris** (et qui corrige une hypothèse de l'audit) : sur P3, le
relais Chat→Chat **n'appelle aucune fonction de `app/protocol/mapping.py`** — les mutations
de corps sont dans `opencode.py` (`ensure_min_tokens` l.11437, garde orphelin l.11445,
`reasoning_effort` l.11420) et ne sont pas atteignables par le harnais golden. La **seule**
conversion `mapping.py` de P3 est `_chat_to_responses_request`, appliquée quand l'endpoint
amont est `/responses` (jambe payante `opencode.py:11606`, jambe free `muse-spark`
`opencode.py:6206`). Le golden porte donc sur **ce** chemin — un Chat→Chat pur n'aurait
rien verrouillé de plus que P2.

**Golden ajouté** : `docs/v1-response-golden/p3_chat_to_responses_bounds_stop_tools.json`
(48ᵉ fixture) + `test_p3_golden_present` et `test_p3_tool_names_and_token_bounds`.
Il fige : `max_tokens 512 → max_output_tokens 512` **prioritaire** sur
`max_completion_tokens 300` (axe A17/L14) ; `tools[].function.parameters → tools[].parameters`
(+ `additionalProperties: false` du profil muse-spark) ; nom d'outil de 124 → 64 caractères
avec `_tool_name_map` restaurable (axe A8) ; renommage de l'historique ; `tool` orphelin
écarté ; `stream` conservé.

**Preuves** : 51 tests golden verts ; **les 47 goldens préexistants sont inchangés**
(sha256 identiques 47/47, et `git status` ne montre que la nouvelle fixture) ; générateur
idempotent (47/47 payloads re-rendus identiques, en dry-run **sans écriture**) ; `_call` ≡
`call_fn` vérifié sur 48/48. **Mutations** : trois mutations de source **en mémoire** (plugin
pytest hors dépôt, aucun fichier du dépôt touché) — lecture sans `max_tokens`, sans
`sanitize_tool_names`, sans `_remap_responses_history_names` → **les trois mordent**
(2 échecs chacune).

**Défaut neuf, MESURÉ, non corrigé ici** : `_chat_to_responses_request` **perd
silencieusement** `stop` / `stop_sequences` et `stream_options` d'un client P3 quand l'amont
est `/responses` (champs absents du corps amont). Conséquence possible : la réponse ne
s'arrête pas sur la séquence demandée. Le golden **fige le constat** et le test le rend
explicite — un correctif devra donc régénérer la fixture. Je ne l'ai pas corrigé parce que
la cible (API Responses) **n'a peut-être pas** d'équivalent `stop` : inventer un champ au
risque d'un 400 est exactement l'erreur évitée au §12.3. **À trancher contre la spec**, puis
soit corrigé, soit inscrit comme perte résiduelle assumée et **tracée** (pas silencieuse).

### 12.8 Jambe free de P6 : aucun test — fermé, et un défaut révélé (TROU 13)

Trois témoins ajoutés dans `tests/test_e2e_protocol_matrix.py` (section dédiée) :
`test_p6_jambe_free_nonstream_endpoint_chat` et `test_p6_jambe_free_nonstream_endpoint_responses`
(couvent **déjà verts** — la jambe free fonctionnait sur les deux formes d'endpoint : le trou
était bien un trou de **couverture**, pas un défaut), plus un troisième cas qui a révélé le
défaut du §12.10.

**Preuve de morsure** — 4 mutations, en **worktree git jetable** (l'arbre de travail n'a
jamais été touché ; `opencode.py` restauré byte-identique, sha256 `ef05e577…`) :
`_try_free_model_first` → `None` : **2 échecs** ; retour Chat non converti : échec du volet
Chat ; aller non converti : échec ; `stream=False` non forcé : **ne mord pas** — le chemin
était alors inatteignable (c'est le défaut lui-même), et l'agent l'a déclaré au lieu de le
maquiller.

### 12.9 P6-stream : le 503 annonçait une jambe free qui n'avait pas lieu (TROU 15)

**Défaut MESURÉ** (même corps client, clé payante en pause) :

| requête | avant | après |
|---|---|---|
| `stream: false` | 200 + texte free, **1 appel** amont | inchangé |
| `stream: true` | **503** « free model will be tried on next attempt », **0 appel** amont | 200 + texte free |

Le message annonçait un essai qui n'avait pas lieu, alors que le code de streaming situé
plus bas (après le `return` du non-stream) force `stream = False` en amont **et tente déjà
la jambe free**.

**Correctif — en deux temps, le premier insuffisant.** La branche `is_stream` laisse
désormais filer vers ce code de streaming ; mais un **`return` 503 inconditionnel** situé
juste après le bloc free interceptait encore le flux : une sonde a montré que la branche
était bien atteinte et que **zéro** appel amont partait quand même. Ce `return` est donc
conditionné, et un drapeau `_paused_sans_cle` interdit d'appeler le payant sans clé — si la
jambe free ne donne rien, le 503 renvoyé est **véridique** (« no free model available »).

**Témoin + mutation.** `test_p6_jambe_free_stream_bufferise_et_sans_fuite_de_format` : il
était marqué `xfail` (défaut mesuré, non corrigé) ; le marqueur est **retiré** car il passe.
Mutation : court-circuit rétabli → **il rougit** (« jambe free attendue une fois, appels :
[] »), fichier restauré à l'octet.

### 12.10 Jambe free : ce qui est recalculé, ce qui ne l'est pas (TROU 7, partiel) — **supplanté par §12.13**

**Mesuré par lecture du code** (`_try_free_model_first`, `opencode.py:6075+`) : la
déclaration de l'audit (« la décision prise côté payant n'est pas recalculée après la
bascule ») est **trop large**. Ce qui EST recalculé : la conversion est refaite **avec le nom
du modèle free** (`{**body, "model": free_model}`), donc tout ce qui est indexé par modèle
l'est aussi — profil de schéma (`muse-spark` → `additionalProperties: false`, verrouillé par
le golden P3 du §12.7), garde `supports_cache_control` (§12.2), champ de borne (`max_tokens`
→ `max_output_tokens` selon l'endpoint) : L6204 (anthropic→`/responses`), L6206
(chat→`/responses`), L6215 (anthropic→Chat).

Ce qui **n'est PAS** recalculé : **l'effort**. Le corps Chat part **tel quel** vers l'endpoint
free Chat (`free_body = dict(body)`, L6218) : le `reasoning_effort` décidé pour le modèle
**payant** (`_resolve_effort(body, model_id)`, L11423, qui applique `thinking.effort_caps`)
est donc réutilisé pour le modèle free — aucun site d'effort n'existe dans
`_try_free_model_first` (vérifié : zéro occurrence). Si le plafond du modèle free est plus bas
que celui du payant, le free reçoit un niveau qu'il ne devrait pas.

**Statut : mesuré, non corrigé à cette date** — le correctif (recalculer la décision d'effort
avec le modèle free) doit venir **avec un témoin qui mord**, donc pas seulement une lecture
de code. C'est le seul volet du TROU 7 qui survit à la mesure.

### 12.11 A8 sur P3 : l'aide existait, personne ne l'appelait (TROU 2)

**Défaut mesuré.** Sur P3 (client Chat → modèle dont l'amont est Chat), un nom d'outil de
**102 caractères** partait **verbatim** vers l'amont, qui plafonne à 64 (rejet attendu), et le
retour rendait en outre le nom raccourci. L'audit annonçait « `sanitize_tool_names` jamais
appelé dans `opencode.py` » : c'est exact, **mais la formulation induisait la mauvaise
correction** — dans un corps Chat le nom vit sous `function.name`, pas à plat, donc l'aide
« forme Responses » n'aurait rien vu. Le vrai constat est meilleur et plus gênant :
**`_sanitize_chat_tools`, `_remap_chat_history_names` et `_remap_chat_tool_choice` existaient
et étaient du code mort**, appelés nulle part. Ils n'étaient pas bogués ; ils n'étaient pas
câblés — et l'audit les croyait câblés par le lot L4.

**Correctif.** Nouvelle aide publique `sanitize_chat_tool_names(body)` appelée en tête de la
branche `protocol == "openai"` du handler `chat_completions` (`opencode.py:11476`), et
`restore_chat_response_tool_names(data, name_map)` au retour (`mapping.py:3106` et `3134`,
construites sur les trois aides mortes). Deux **pièges mesurés** expliquent pourquoi
« restaurer la réponse » ne suffisait pas : le non-stream rendait `resp.content`, les **octets
amont verbatim** (le nom restauré n'y était donc pas), et le stream **réémettait la ligne
brute** (`line.encode()`). Les deux sont corrigés (re-sérialisation du seul cas renommé ;
`yield` du chunk parsé restauré).

**Témoin + mutations.** `tests/test_p3_tool_names.py` (8 cas) ; le harnais est **importé** du
fichier E2E, jamais recopié, et le double amont rejoue **le nom qu'il a reçu** : le test ne
présuppose donc pas le comportement corrigé (défaut de classe A25 évité). Sans correctif :
**5 rouges**. Cinq mutations (aller neutralisé, `yield` brut, restauration inerte, map
retirée) mordent, `ruff` OK, CRLF intact, sha256 restaurés à l'octet.

**Limites déclarées.** Cache de réponses et jambe free réelle de P3 non couverts (stubs) ;
une entrée `per-file-ignores` a été ajoutée à `pyproject.toml` pour ce fichier de test, sur le
motif déjà présent dans le dépôt (Ré-export de fixtures).

### 12.12 P2 vers `/responses` : la couverture manquait, et deux défauts en sont sortis

**Couverture (le trou lui-même).** Deux témoins ajoutés dans `tests/test_e2e_protocol_matrix.py`
(section dédiée) : `test_p2_jambe_free_endpoint_responses_nonstream` (**vert** — le chemin
fonctionnait : c'était bien un trou de couverture) et
`test_p2_jambe_free_endpoint_responses_stream_sans_cle_payante` (**xfail**, voir ci-dessous).
Mutations en **worktree jetable** (l'arbre n'a jamais été touché) : neutraliser
`_chat_to_responses_request` → **mord** (« corps non converti en forme Responses ») ; laisser
`free_body = dict(body)` → **mord** (« modèle free non swappé »). Deux autres mutations
(`stream` forcé à `False`, retour non converti) **ne mordent pas** : l'une est indistinguable
en non-stream, l'autre est masquée par le défaut D2. Déclaré, pas maquillé.

**Défaut D2 — un 200 du modèle free jeté comme un échec (corrigé, partiellement).**
`_try_free_model_first` ne lisait la réponse `/responses` que si le `content-type` était JSON
(`opencode.py:6595`). Or quand on demande `stream: true` à `/responses`, la réponse est un
**flux SSE** : `rdata` restait vide, la réponse **200** était classée « empty JSON response »,
le repli payant prenait le relais — et clés en pause, le client recevait **503** alors que le
modèle free avait répondu. Même classe que le 503 mensonger de P6-stream (§12.9).

Correctif appliqué : le corps est collecté (`.text`, à défaut les lignes asynchrones — cas
d'un flux réellement diffusé) et l'objet Responses complet est reconstruit depuis
`response.completed`, à défaut le dernier `response.*` portant `output`
(`_collect_responses_sse_object`, `_free_responses_body_object`).

**Ce qui reste ouvert, dit franchement** : le repli du handler P2 (`opencode.py:10051-10067`)
rend sa réponse en **JSON sans regarder `is_stream`**. Après correctif, un client *streaming*
reçoit donc **200 avec le texte** (au lieu du 503) mais en `application/json` — la perte de
données est réparée, le **cadrage SSE** ne l'est pas. Le témoin reste donc `xfail` sur cette
seule assertion, et la mutation associée n'est pas revendiquée.

**Défaut D1 — comptabilité à zéro (mesuré, non corrigé).** Le même repli lit
`usage.prompt_tokens` / `usage.completion_tokens` (`opencode.py:10026-10027`) alors qu'une
réponse `/responses` porte `input_tokens` / `output_tokens` : la consommation de la jambe free
sur ce chemin compte **0**. Le contenu rendu au client, lui, est correct. Non corrigé faute de
témoin : une correction sans test qui mord serait exactement ce que cet audit reproche.

**Limites déclarées.** Le chemin `stream_gen` de la jambe free `/responses` (clés payantes
*disponibles*) n'est pas exécutable dans ce proxy et n'est donc couvert par **aucun** test ;
outils, `tool_choice` et images sur ce chemin ne le sont pas non plus.

### 12.13 TROU 7 : l'effort de la jambe free est désormais recalculé

**Correctif.** Dans `_try_free_model_first` (bloc inséré vers `opencode.py:6220-6253`) : si le
corps destiné au free porte un `reasoning_effort` **décidé pour le modèle payant**, il est
recalculé avec `free_model` via la **source unique** (`_resolve_effort`, `config.effort_policy`)
— aucune table locale, et rien n'est inventé quand le client n'a rien demandé. Le recalcul ne
peut que **rabaisser** le niveau.

**Témoin + mutation.** `tests/test_free_leg_effort_recompute.py` (5 cas) ; le témoin passe par
le **handler réel** `chat_completions` et asserte le corps **réellement remis à la couche
réseau** (couple mesuré : payant `deepseek-v4-flash`, plafond `max` ; free
`deepseek-v4-flash-free`, plafond `high`). Mutation (`if False and _paid_effort is not None`) :
**mord** — `assert 'max' == 'high'`, avec le message qui nomme les deux plafonds. sha256
restauré ; `ruff` OK.

**Limites déclarées par la mesure.** (1) Seul le chemin « endpoint free = Chat » est prouvé —
c'est le seul où un écart de plafond réel existe. (2) Sur `/responses`, le bloc est **inerte** :
`_chat_to_responses_request` clampe déjà l'effort en aval (mesuré, A/B identique) — c'est
pourquoi la déclaration initiale de l'audit (« la décision payante n'est pas recalculée »)
était **trop large**. (3) Aucun couple à plafonds divergents n'existe pour `muse-spark-*`
(`xhigh`/`xhigh`) : non prouvé par un témoin rouge, dit tel quel.

**Faille trouvée APRÈS coup, en vérifiant la garantie « ne peut que rabaisser » — et corrigée.**
La garantie ne tenait pas par construction, seulement par chance de configuration. Mesure sur la
config réelle : `_resolve_effort` donne la **priorité** à `effort` et `output_config.effort` sur
`reasoning_effort`.

| Sonde passée à `_resolve_effort` (plafond du modèle = `xhigh`) | Niveau rendu |
|---|---|
| `{"reasoning_effort": "high"}` | `high` |
| `{…, "reasoning": {"effort": "max"}}` | `high` |
| `{…, "thinking": {"type": "adaptive"}}` | `high` |
| `{…, "effort": "max"}` | **`max`** ← dépasse la décision payante |
| `{…, "output_config": {"effort": "max"}}` | **`max`** ← idem |

Or le bloc recopiait **tout** le corps (`dict(body)`) avant d'y réinjecter la décision payante :
les formes plus prioritaires restaient présentes et pouvaient donc **relever** le niveau au-dessus
de la décision payante. Le correctif rend la garantie structurelle : la sonde ne porte plus QUE
`{"reasoning_effort": _paid_effort}`.

**Portée exacte de cette faille, dite sans dramatiser.** Avec la configuration actuelle, aucun
couple payant/free n'a de plafond free **supérieur** au payant : la faille était donc **latente**
(inatteignable en l'état) et non active en production. Elle devenait active dès qu'un couple
futur aurait un plafond free plus haut — c'est pourquoi elle est corrigée maintenant plutôt que
laissée au hasard d'une future configuration.


### 12.14 TROU 9 (A11) : bufferisation **mesurée en live**, non corrigée à cette date

**Ce qui est mesuré** (proxy relancé sur le code corrigé, requêtes réelles, `max_tokens` 400) :

| Requête | TTFB | total | ratio |
|---|---|---|---|
| P1 stream (`/v1/messages`, client Anthropic) | 21,06 s | 24,36 s | **0,86** |
| P6 stream (`/v1/responses`, client Responses) | 26,11 s | 26,11 s | **1,00** |

Le premier octet arrive **avec le dernier** : la revendication de l'audit (TTFB = durée totale
de génération) est **confirmée** sur P6.

**Réserve honnête sur la mesure.** Le « contraste » P1 n'en est pas un : sur cette route les
deux requêtes empruntent la **jambe free**, qui bufferise elle aussi. Le chemin payant n'a pas
pu être mesuré — la clé payante répond « unauthorized » sur ce poste. La mesure dit donc ce
qu'un client subit **ici**, pas la différence payant/free.

**Contrainte structurelle, établie par lecture du code (et non supposée).** Le battement de
cœur **existe déjà** : `_SSE_KEEPALIVE_INTERVAL = 15 s` (`opencode.py:8102`), commentaire
documenté comme « harmless to clients », émis par `_sse_pump` (`opencode.py:8110-8121`). Mais il
ne peut émettre qu'**entre deux `yield` du générateur** : si le générateur bloque toute la
génération **avant sa première émission** — ce que fait précisément le chemin P5/P6 —, le pump
ne peut rien faire. Conséquence pour tout correctif futur : il doit émettre **avant d'attendre
l'amont**, et non se contenter d'envelopper le générateur existant avec `_sse_keepalive`. Un
correctif qui se contenterait de l'enveloppe ne changerait **rien** et son témoin ne pourrait
pas mordre — c'est le critère de jugement retenu.

**Portée réelle de la mesure.** Seul **P6** a pu être chronométré. **P5** n'est pas mesurable
sur ce poste : il exige soit une clé payante valide (celle du poste répond « unauthorized »),
soit des clés **en pause** (seul état qui déclenche le repli free) — deux conditions absentes.
Quant à la ligne **P1** du tableau (ratio 0,86), elle ne documente pas le chemin **payant** : elle
reflète elle aussi la jambe free. Aucune ligne de ce tableau ne mesure donc le payant, et P5 n'a
pas été mesuré du tout.

**Pourquoi ce n'est pas corrigé dans cette passe.** Un vrai incrémental demande un
convertisseur **SSE → SSE Responses** qui n'existe pas dans le dépôt : l'inventaire ne trouve
que le sens inverse (`_responses_sse_to_chat_deltas`, `mapping.py:3826`) et un
`responses_stream_sse` qui prend une **liste d'événements déjà calculée**. C'est un chantier,
pas un correctif de trou. Le correctif borné envisagé (en-têtes et battement de cœur immédiats,
contenu toujours bufferisé) n'a **pas** été écrit : le déclarer fermé sans l'avoir fait serait
exactement le travers que cet audit corrige.

### 12.15 A8 sur P5/P6 : restauration absente au retour (TROU 3), et deux écarts mesurés

**Mesure dans les deux sens — c'est elle qui a tranché la formulation.** Sur P5
(`/v1/responses` → `muse-spark` → amont `/responses`), l'**aller raccourcit bien** le nom de
78 caractères à ≤64 et la `_tool_name_map` accompagne le corps : le trou est donc
« **restauration absente au retour** », comme l'annonçait l'audit. Sur P6
(`/v1/responses` → `minimax-m3` → amont Anthropic), en revanche, l'amont reçoit le nom client
**tel quel** et aucune map n'est construite : le restore-retour y est un **no-op mesuré**.
Deux situations différentes que la formulation unique de l'audit confondait.

**Correctif.** `anthropic_to_openai_responses(anthro, model, name_map=None)` et
`openai_chat_to_responses(chat_resp, model, name_map=None)` (`mapping.py:2626` et `2716`)
restituent `function_call.name` via `restore_tool_name` — **symétrie exacte** avec les jumelles
`_responses_to_chat_response` / `_responses_to_anthropic_response`, aucune table nouvelle.
`opencode.py:13886` extrait la carte de la clé privée existante (`_TOOL_NAME_MAP_KEY`) et la
propage aux **7 sites** d'appel.

**Témoin + mutations.** `tests/test_trou3_a8_responses_restore.py` (5 cas, harnais E2E ASGI,
zéro réseau) ; le témoin P5 asserte que le client reçoit **son** nom d'origine, pas celui de
l'amont. Trois mutations, chacune annulée et vérifiée au sha256 :
**MUT-A** (restauration neutralisée dans `openai_chat_to_responses`) → **mord** : c'est ce
point-là qui porte la preuve. **MUT-C** (extraction de la carte neutralisée) → **mord**.
**MUT-B** (même neutralisation dans `anthropic_to_openai_responses`) → **ne mord pas**, et
c'est déclaré : la symétrie est posée, mais comme P6 ne construit aucune map, ce point est
**aujourd'hui inerte** — le témoin P6 ne pourra mordre qu'après correction de l'aller P6.

**Écarts mesurés, non corrigés, et pourquoi.**

1. **P6, aller** : le nom part **verbatim** (78 car.) vers un amont Anthropic. L'amont tolère
   200 caractères, donc rien ne casse — mais A8 est **absent de cette jambe**. Trou distinct,
   hors périmètre de ce correctif, et laissé ouvert plutôt que masqué.
2. **Amont P5 en Responses natif** : le handler passe `data` verbatim, le convertisseur est
   court-circuité — les noms ne sont pas restaurés non plus. Testé en caractérisation.
3. **P5 en streaming** : le collecteur ne garde que `content`/`reasoning_content` du flux Chat
   amont, donc un `tool_calls` d'amont **n'atteint jamais** la réponse convertie (mesuré :
   `output: []`). Aucun témoin streaming n'est donc possible **avant** ce correctif-là — et
   c'est un défaut à part entière, pas une simple limite de test.

**Limite de méthode.** Tout est hermétique : aucun de ces tests ne parle à un vrai amont.

### 12.16 Trous encore ouverts à cette date

| Trou | État | Où |
|---|---|---|
| 2 — A8 noms d'outils sur P3 | **fermé** (§12.11) — l'aide existait mais était du **code mort** | `opencode.py:11476`, `mapping.py:3106/3134` |
| 3 — A8 restauration des noms sur P5 (`name_map`) | **fermé** (§12.15) — l'aller P5 raccourcissait bien, c'est le retour qui ne restaurait pas ; **P6 aller reste verbatim** (trou distinct, mesuré) | `mapping.py:2626/2716`, 7 sites `opencode.py` |
| 5 — témoins P6 (orphelin + signature) | **fermé** (§12.6) | `tests/test_p6_orphan_and_thinking.py` |
| 6 — P2 → `/responses` | **fermé** (§12.12) : 1 témoin vert + 1 `xfail` (cadrage SSE du repli), 2 défauts neufs dont 1 corrigé | `tests/test_e2e_protocol_matrix.py` |
| 7 — axe « jambe free » | **fermé** (§12.13) — seul l'effort manquait ; corrigé avec témoin qui mord | `opencode.py:6220-6253` |
| 9 — A11 : P5/P6 bufferisés | **ouvert, mesuré en live** (ratio TTFB/total = 1,00, §12.14) — exige un convertisseur SSE→SSE Responses neuf | `opencode.py` |
| 12 — aucun golden P3 | **fermé** (§12.7) | — |
| 13 — jambe free de P6 sans test | **fermé** (§12.8) | `tests/test_e2e_protocol_matrix.py` |
| 14 — `stop`/`stream_options` perdus vers `/responses` | **mesuré**, à trancher contre spec (§12.7) | `mapping.py:3267` |
| 15 — P6-stream : 503 au lieu de la jambe free | **fermé** (§12.9) | `opencode.py` (handler `responses`) |

Écarts **neufs**, trouvés par ces travaux, **mesurés et laissés ouverts** — ils ne figuraient pas
dans les 13 trous de départ, et aucun n'est maquillé en correction :

| Écart | État | Où |
|---|---|---|
| A8 sur **P6, aller** : le nom d'outil part **verbatim** (78 car.) vers un amont Anthropic, aucune map n'est construite | **mesuré, ouvert** ; l'amont tolère 200 car., donc rien ne casse | `opencode.py` (aller P6) |
| P5 quand l'amont est en **Responses natif** : le handler passe `data` verbatim, le convertisseur est court-circuité | **mesuré, ouvert** (caractérisé par un test) | `opencode.py` (handler `responses`) |
| P5 en **streaming** : le collecteur ne garde que `content`/`reasoning_content`, un `tool_calls` d'amont n'atteint jamais la réponse (mesuré : `output: []`) | **mesuré, ouvert** — empêche aussi tout témoin streaming du trou 3 | `opencode.py` (collecteur P5) |
| Repli **P2 streaming** : rendu en `application/json` sans consulter `is_stream` (le 503 mensonger est corrigé, le cadrage SSE non) | **mesuré, ouvert** — `xfail` déclaré | `opencode.py:10051-10067` |
| **Comptabilité de tokens** de la jambe free `/responses` : lit `prompt_tokens` sur une charge qui porte `input_tokens` → compte **0** (contenu correct) | **mesuré, ouvert** — non corrigé faute de témoin | `opencode.py:10026-10027` |

Ce tableau est tenu à jour : un trou n'est déclaré fermé que lorsque le témoin **mord**.

---

### 12.17 Verdicts finaux des trous 1 et 9 — deux refus argumentés

Ces deux trous ont été repris par des agents dédiés, avec l'exigence explicite : un livrable
prouvé, ou un refus argumenté — jamais un faux correctif. Les deux ont conclu au refus, mesures
à l'appui.

**TROU 9 (A11, P5/P6 bufferisés) — le correctif borné est IMPOSSIBLE, et l'aurait été
nuisible.** Le correctif a d'abord été écrit (helper de flux + ping initial, les 4 sites SSE
reliés), puis **mesuré en HTTP réel** (uvicorn en thread, socket brute, amont bloqué sur un
`threading.Event`) : **TTFB/total reste 1,00 sur P5 comme sur P6**. La cause racine a été tracée
par instrumentation du constructeur `StreamingResponse` dans l'application réelle : il n'est créé
qu'à **t = 625 ms**, soit *après* la libération de l'amont à 610 ms. Le handler n'atteint jamais
son `return` avant la fin de la génération (P5 bloqué dans la boucle `aiter_lines`, P6 dans
`resp.json()`), donc **l'objet réponse n'existe pas encore** au moment où il faudrait émettre :
remplacer `Response` par `StreamingResponse` ne peut rien y changer.

Le refus ne tient pas seulement à l'inefficacité. Le ping, émis en fin de génération, aurait
**remis à zéro le watchdog TTFB et les timeouts idle de la jambe amont** : il aurait donc
**masqué** les blocages que ces garde-fous existent pour détecter. Un correctif qui dégrade
l'observabilité en prétendant l'améliorer justifie le refus.

**Défaut neuf, mesuré au passage :** `resp.json()` est appelé **sans `await`** (`opencode.py`),
exécution donc **synchrone sur la boucle d'événements** — le harnais ASGI de l'agent a été
**gelé** par cet appel. C'est un défaut distinct du trou 9, et il bloquerait de toute façon
l'émission d'un ping côté P6.

**Ce qu'exigerait un vrai correctif (déduit, chiffré).** Rendre le handler *paresseux* :
construire la réponse et ses en-têtes **avant** l'appel amont, puis déplacer toute la
collecte/conversion/journalisation dans un générateur consommé par la réponse. Cela entre en
contradiction avec les `return` de mi-parcours et les branches d'erreur (`AllKeysPausedError`,
`FreeRefusal`, `UpstreamError`) qui renvoient aujourd'hui des 4xx/5xx propres — en-têtes déjà
partis, ces réponses ne sont plus possibles. **2 des 4 sites** sont concernés directement ; P6
exige en plus la correction de l'appel bloquant. C'est le chantier qui avait été mis hors
périmètre, et il le reste : il est maintenant décrit, borné, et non fait — dit tel quel.

Le témoin construit (harnais ASGI piloté directement, assertion d'**ordre causal** et jamais de
durée, car `TestClient` bufferise la réponse entière et ne peut pas exprimer un ordre causal)
**échoue avec le correctif appliqué** : `2 failed, 2 passed`, « aucun octet n'est parti avant la
libération de l'amont ». Autrement dit le témoin **réfute** le correctif. Aucun témoin vert n'a
été fabriqué, et aucune section mutation n'a été inventée pour un correctif inexistant.

**TROU 1 (A27) — aucun témoin ne peut mordre sur ce commit, et le témoin existant est
complice.** Deux mesures décisives. (1) Restaurer le défaut d'origine (l'écrasement des en-têtes
par `None`) aux **14 sites** ne fait échouer **aucun** test : 78 passed / 1 xfailed, identique au
run pristine. (2) Une sonde instrumentée juste avant la garde montre que `a_headers` y est
**toujours un `dict`**, jamais `None` — alors qu'une sentinelle `if True:` au même endroit casse
6 tests, ce qui prouve que le **site** est bien atteint.

La raison est structurelle : `_get_auth_headers` rend toujours un `dict` ; la seule affectation
qui mettrait `None` dans le créneau d'en-têtes du handler `messages` vit **dans** le
`except AllKeysPausedError`, branche qui **retourne sur tous ses chemins**. Atteindre la garde
suppose donc que l'authentification a réussi, c'est-à-dire que `a_headers` est un `dict` : **les
deux conditions de la garde sont mutuellement exclusives**, le corps de la garde est du code
**non atteint** sur ce commit.

Le témoin A27 existant ne verrouille rien : il fait rendre un tuple non-`None` à la jambe free,
le handler répond **200 sans jamais consulter la garde**, et le test **passe sous toutes les
mutations**, y compris celle qui rétablit le défaut d'origine. C'est un test complice, mesuré.

**Remédiation, côté correctif et non côté test** (proposée par la mesure) : soit normaliser le
cas « aucune clé » dans le `except AllKeysPausedError` du handler `messages` en posant
`a_headers = None` avant le retour, pour que la garde teste une valeur **réellement produite** au
lieu d'une variable non liée ; soit coupler la garde à `_get_auth_headers` sur les 13 autres
sites.

**Conséquence, dite sans détour : l'exigence « une correction = un test qui échoue sans elle »
reste INSATISFAITE pour A27.** Le correctif antérieur a bien supprimé la cause racine, mais la
garde ajoutée en « défense en profondeur » est indéfendable en l'état, et le trou 1 reste
**ouvert** — non par manque d'essai, mais parce que l'arbre committé ne permet plus de le
déclencher.

**Confirmation indépendante du constat A27, faite sur l'état committé et non sur la foi du
rapport d'agent.** La question décisive est : `a_headers` peut-il seulement valoir `None` à
l'endroit de la garde ? Réponse au niveau du code, par énumération exhaustive :

- `_get_auth_headers` (L318) a exactement **deux** `return`, tous deux des **`dict`** (L323,
  L328) — jamais `None` ;
- les **14 affectations** à `a_headers` du fichier viennent de `_get_auth_headers`,
  de `dict(resp.headers)`, d'un dépouillement de tuple ou d'un littéral `dict` : **aucune**
  n'assigne `None`. La ligne d'écrasement d'origine, celle qui produisait le 500, **n'existe
  plus**.

Conséquence : à la garde `if a_headers is None and resp.status_code != 200:`, la première
condition est **toujours fausse**. Le corps de la garde est **inatteignable** — ce n'est pas une
garde difficile à déclencher, c'est du code mort.

**Ce que cela change, et une correction de notre propre analyse.** La remédiation proposée plus
haut (« poser `a_headers = None` avant le retour de la branche `except AllKeysPausedError` ») est
**inexacte** : cette branche retourne sur tous ses chemins, la garde n'est donc pas atteinte depuis
elle ; l'assignation déplacerait du code mort sans le rendre vivant. Elle est corrigée ici plutôt
que laissée dans le plan comme une piste valable.

**Verdict A27, définitif sur cet arbre** : le défaut est **corrigé à la racine** (plus aucun
écrasement par `None`), la garde de défense en profondeur est **structurellement morte**, et donc
**aucun témoin ne peut mordre** : il faudrait réintroduire artificiellement l'état interdit pour
que la garde devienne testable. Deux issues honnêtes, au choix du propriétaire du dépôt : soit
**retirer** la garde morte, soit la remplacer par une assertion d'invariant sur `_get_auth_headers`.
Tant que ce choix n'est pas fait, le trou 1 reste **ouvert** — non par insuffisance d'essais, mais
parce que l'exigence « un test qui échoue sans la correction » n'a plus d'objet mesurable ici.

**Suite (tours 31-32) — TROU 1 FERMÉ, et une démonstration que j'ai dû corriger.** Le constat
« la garde est inatteignable » était juste, mais la preuve que j'avais produite ne l'était pas :
elle reposait sur un grephe du motif `a_headers\s*=`, qui **ne matche pas** un dépouillement de
tuple. La démonstration valable vient de la lecture du site réel : ligne 12755, le résultat de la
jambe free est dépouillé en `resp, _, _actual_model, _actual_ip` — le créneau d'en-têtes part dans
`_`, il ne peut donc **jamais** parvenir à `a_headers`. Confirmé par ailleurs : `_get_auth_headers`
(L318) n'a que deux `return`, tous deux des `dict`, et aucune affectation n'écrit `None`.

**Ce qui a été corrigé, et pourquoi c'est une correction et pas un contournement :** le commentaire
qui affirmait « `a_headers` peut valoir None » disait **faux** — c'est le type d'affirmation périmée
que cet audit traque partout ailleurs, et elle vivait dans le code de production. Il est remplacé
par ce que le correctif a réellement fermé : le dépouillement en `_` (L12755) et le ternaire
`if a_headers else "?"`, qui est la protection **réellement atteignable**. La branche `if a_headers
is None` a été **retirée** : elle était inatteignable, et la laisser faisait croire à une défense
qui ne peut pas se déclencher. Le commentaire explique désormais pourquoi elle a été retirée, et
que si un `None` revenait un jour dans ce créneau, c'est le ternaire qu'il faudrait tester.

La suppression est **prouvée sans effet de bord** : la condition étant toujours fausse, retirer la
branche ne peut rien changer — et les 9 tests de la zone passent, plus 1 xfailed, AST et ruff
propres. Les trois docstrings de test qui décrivaient l'ancien mécanisme comme s'il était courant
ont été rectifiées : elles disent maintenant que le `None` ne parvient jamais à `a_headers`, que le
503 observé vient de la traduction « clés en pause » en 503 retryable, et que ces tests **ne
prouvent pas** la garde.

**Ce que cela ne prétend pas.** L'exigence « un test qui échoue sans la correction » reste sans
objet pour A27 : le défaut d'origine n'est plus atteignable, donc aucun test ne peut le distinguer
d'un état sain. C'est écrit plutôt que coché. Le trou est fermé au sens où le code ne porte plus
d'affirmation fausse ni de défense fantôme ; il ne l'est pas au sens d'un témoin mordant, qui
resterait impossible.


*
*
S
u
i
t
e
 
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
 
:
 
C
O
R
R
I
G
É
 
e
t
 
p
r
o
u
v
é
,
 
a
p
r
è
s
 
u
n
 
é
c
h
e
c
 
i
n
s
t
r
u
c
t
i
f
.
*
*




L
e
 
d
é
f
a
u
t
 
:
 
d
a
n
s
 
l
e
 
h
a
n
d
l
e
r
 
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
 
(
`
a
s
y
n
c
 
d
e
f
 
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
 
d
o
n
t
 
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
 
e
s
t


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
)
,
 
l
e
 
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
 
é
t
a
i
t
 
p
a
r
s
é
 
p
a
r
 
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
 
*
*
d
e
 
f
a
ç
o
n
 
s
y
n
c
h
r
o
n
e
*
*
,


d
o
n
c
 
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
.
 
C
e
 
c
o
r
p
s
 
p
e
u
t
 
ê
t
r
e
 
*
*
t
o
u
t
 
u
n
 
f
l
u
x
 
S
S
E
 
a
c
c
u
m
u
l
é
*
*
 
:
 
l
e
 
g
e
l
 
c
r
o
î
t
 
a
v
e
c


s
a
 
t
a
i
l
l
e
,
 
e
t
 
u
n
 
h
a
r
n
a
i
s
 
A
S
G
I
 
a
v
a
i
t
 
é
t
é
 
f
i
g
é
 
p
a
r
 
c
e
t
 
a
p
p
e
l
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
 
:
 
u
n
e
 
a
i
d
e
 
`
_
r
e
s
p
_
j
s
o
n
_
h
o
r
s
_
b
o
u
c
l
e
(
r
e
s
p
)
`
 
q
u
i
 
d
é
p
l
a
c
e
 
l
e
 
p
a
r
s
e
 
d
a
n
s
 
u
n
 
t
h
r
e
a
d
 
v
i
a


`
a
s
y
n
c
i
o
.
t
o
_
t
h
r
e
a
d
`
 
(
p
r
é
c
é
d
e
n
t
 
d
é
j
à
 
é
t
a
b
l
i
 
d
a
n
s
 
l
e
 
m
o
d
u
l
e
)
.
 
C
o
m
p
o
r
t
e
m
e
n
t
 
s
t
r
i
c
t
e
m
e
n
t
 
i
d
e
n
t
i
q
u
e
 
:
 
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
 
e
t
 
l
e
 
c
a
s
 
«
 
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
 
-
>
 
`
{
}
`
 
»
 
p
r
é
s
e
r
v
é
,
 
v
e
r
r
o
u
i
l
l
é
 
p
a
r


u
n
 
t
e
s
t
 
d
o
n
t
 
l
e
 
p
a
r
s
e
u
r
 
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
.




*
*
L
e
 
t
é
m
o
i
n
 
e
s
t
 
c
a
u
s
a
l
,
 
p
a
s
 
t
e
m
p
o
r
e
l
*
*
 
:
 
l
e
 
d
o
u
b
l
e
 
s
i
g
n
a
l
e
 
s
o
n
 
e
n
t
r
é
e
 
d
a
n
s
 
l
e
 
p
a
r
s
e
 
p
u
i
s
 
a
t
t
e
n
d
 
u
n


`
t
h
r
e
a
d
i
n
g
.
E
v
e
n
t
`
 
;
 
u
n
e
 
v
e
i
l
l
e
 
n
'
i
n
c
r
é
m
e
n
t
e
 
q
u
'
*
*
a
p
r
è
s
*
*
 
c
e
t
t
e
 
e
n
t
r
é
e
.
 
A
v
e
c
 
l
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
 
l
a
 
b
o
u
c
l
e
 
r
e
s
t
e


l
i
b
r
e
 
e
t
 
l
a
 
v
e
i
l
l
e
 
p
r
o
g
r
e
s
s
e
 
;
 
a
v
e
c
 
l
e
 
p
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
 
l
'
e
n
t
r
é
e
 
a
 
l
i
e
u
 
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
,
 
g
e
l
é
e
,
 
e
t
 
l
a


v
e
i
l
l
e
 
n
e
 
p
e
u
t
 
p
l
u
s
 
s
'
e
x
é
c
u
t
e
r
.
 
A
u
c
u
n
 
s
e
u
i
l
 
d
e
 
d
u
r
é
e
,
 
a
u
c
u
n
 
`
s
l
e
e
p
`
 
d
e
 
s
y
n
c
h
r
o
n
i
s
a
t
i
o
n
,
 
c
h
i
e
n
 
d
e
 
g
a
r
d
e


`
w
a
i
t
_
f
o
r
`
.
 
S
o
r
t
i
e
 
b
r
u
t
e
 
:
 
V
E
R
T
,
 
p
u
i
s
 
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
 
a
v
e
c
 
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
 
e
t
 
l
e
 
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
,


p
u
i
s
 
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
 
p
u
i
s
 
V
E
R
T
.
 
V
e
r
d
i
c
t
 
o
u
t
i
l
l
é
 
:
 
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
.




*
*
L
'
é
c
h
e
c
 
i
n
s
t
r
u
c
t
i
f
,
 
e
t
 
l
'
e
r
r
e
u
r
 
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
 
q
u
e
 
j
'
a
i
 
f
a
i
l
l
i
 
c
o
m
m
e
t
t
r
e
.
*
*
 
L
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
 
a


c
a
s
s
é
 
*
*
2
6
 
t
e
s
t
s
*
*
.
 
M
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
 
i
m
m
é
d
i
a
t
e
 
—
 
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
 
r
é
p
o
n
s
e
 
a
m
o
n
t
 
d
e
 
l
a
 
s
u
i
t
e
 
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


p
a
r
s
e
 
d
é
p
l
a
c
é
 
d
a
n
s
 
u
n
 
t
h
r
e
a
d
 
»
 
—
 
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
 
d
i
s
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
 
u
n
e


e
r
r
e
u
r
 
d
e
 
v
a
l
i
d
a
t
i
o
n
 
F
a
s
t
A
P
I
,
 
d
o
n
c
 
u
n
e
 
*
*
r
o
u
t
e
 
p
e
r
d
u
e
*
*
.
 
L
a
 
c
a
u
s
e
 
r
é
e
l
l
e
 
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
 
*
*
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
*
*
,
 
s
i
 
b
i
e
n
 
q
u
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


s
'
a
p
p
l
i
q
u
a
i
t
 
à
 
l
'
a
i
d
e
 
e
t
 
q
u
e
 
l
e
 
h
a
n
d
l
e
r
 
n
'
é
t
a
i
t
 
p
l
u
s
 
r
o
u
t
é
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
 
;
 
m
o
n
 
p
o
i
n
t


d
'
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
 
L
'
a
i
d
e
 
e
s
t
 
d
é
s
o
r
m
a
i
s
 
p
l
a
c
é
e
 
*
*
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
*
*
,
 
a
v
e
c
 
u
n
 
c
o
m
m
e
n
t
a
i
r
e


e
x
p
l
i
q
u
a
n
t
 
p
o
u
r
q
u
o
i
 
c
e
t
 
e
m
p
l
a
c
e
m
e
n
t
 
e
s
t
 
r
e
q
u
i
s
,
 
p
o
u
r
 
q
u
e
 
p
e
r
s
o
n
n
e
 
n
e
 
l
a
 
«
 
r
a
n
g
e
 
»
 
a
u
 
m
a
u
v
a
i
s
 
e
n
d
r
o
i
t
.


L
e
ç
o
n
 
r
e
t
e
n
u
e
 
:
 
u
n
e
 
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
l
a
u
s
i
b
l
e
 
n
e
 
r
e
m
p
l
a
c
e
 
p
a
s
 
l
a
 
l
e
c
t
u
r
e
 
d
u
 
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
.




*
*
C
e
 
q
u
i
 
r
e
s
t
e
 
o
u
v
e
r
t
,
 
e
t
 
d
é
c
l
a
r
é
 
t
e
l
 
q
u
e
l
 
:
*
*
 
l
e
 
c
h
a
n
t
i
e
r
 
d
e
 
f
o
n
d
 
—
 
P
5
/
P
6
 
é
m
e
t
t
e
n
t
 
l
e
u
r
 
S
S
E
 
e
n
t
i
è
r
e
m
e
n
t


b
u
f
f
e
r
i
s
é
,
 
d
o
n
c
 
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
 
—
 
r
e
s
t
e
 
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
 
(
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
)
.
 
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
 
a
i
l
l
e
u
r
s
 
d
a
n
s
 
l
e
 
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
 
:
 
s
i
g
n
a
l
é
,
 
n
o
n


c
o
r
r
i
g
é
.
 
E
t
 
l
e
 
m
o
t
i
f
 
d
e
 
p
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
 
e
x
i
s
t
e
 
à
 
*
*
q
u
a
t
r
e
*
*
 
s
i
t
e
s
 
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
 
e
t
 
l
e
 
s
i
t
e
 
A
1
1


L
1
4
0
1
4
)
 
:
 
s
e
u
l
 
L
1
4
0
1
4
 
e
s
t
 
c
o
r
r
i
g
é
,
 
l
e
s
 
t
r
o
i
s
 
j
u
m
e
a
u
x
 
s
o
n
t
 
d
e
s
 
r
é
p
o
n
s
e
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
e
s
 
d
o
n
t
 
j
e
 
n
'
a
i
 
p
a
s


m
e
s
u
r
é
 
l
a
 
t
a
i
l
l
e
 
d
e
 
c
o
r
p
s
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

