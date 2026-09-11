# PLAN — Audit & complétion des conversions de protocole (effort · contexte · raisonnement · cache · tools · documents)

Date : 2026-09-10. Statut : proposition, aucun code modifié.
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
| A1 (4 mappings divergents) | **confirmée** | `test_axis_effort_p2_*`, `test_axis_effort_p4_*` |
| A2 (`xhigh`/`max` écrasés, `minimal` filtré) | **corrigée au hotfix** | `test_axis_effort_p4_no_level_is_lost`, `_minimal_is_recognized` |
| A3 (P6 perd l'effort) | **corrigée au hotfix** | `test_axis_effort_p6_reaches_anthropic` |
| A4 (plancher absent sur P3) | **confirmée — gap connu** | `test_axis_min_tokens_floor_p3_is_a_known_gap` |
| A5 (`cache_control` tools perdu) | **confirmée** | `test_axis_cache_control_on_tools_p2_is_lost` |
| A6 (usage cache non testé) | **traitée ici** | `test_axis_cache_read_tokens_extracted` (×3), `_creation_tokens_extracted` |
| A7 (réécriture cache conditionnelle) | à couvrir L3 | — |
| A8 (tools : 3 mécanismes) | **contrat prouvé** | `test_axis_tool_name_roundtrip_is_identity` (×8), orphelins |
| A9 (documents asymétriques) | **confirmée couverture** | `test_axis_document_url_p2_becomes_file`, `_p6` |
| A10 (tokens aveugles aux médias) | **confirmée** | `test_axis_token_estimate_ignores_media_size` |
| A11 (faux streaming) | **confirmée — gap connu** | `test_axis_responses_stream_is_not_incremental_known_gap` |
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
`test_tools_matrix.py` + `test_documents_matrix.py` : pour les 6 chemins — `tools[]`/`tool_choice`/`strict`/nom long/`input_schema` invalide, puis `document` (base64 PDF, URL, `file_id`, texte), image, et `tool_result` portant document/image. Chaque perte silencieuse est soit corrigée, soit **déclarée** dans `docs/conversion-matrix.md` (la ligne « PERDU silencieusement » pour l'image est à mettre à jour).

*Livré* : **`tests/test_tools_matrix.py`** (25 cas — axes `tools[]`,
`tool_choice` nommé, `strict`, nom long, `input_schema` invalide × les 6 chemins).
Le volet documents est couvert par **`tests/test_documents_contract.py`** (dont les
2 verrous A25) plutôt que par un `test_documents_matrix.py` séparé — le
regroupement est assumé, le contenu du plan est couvert.
**Reste ouvert** : l'écart **A8 résiduel** (nom d'outil > 64 vers une cible Chat
sur P2), déclaré et marqué `xfail(strict=True)` dans la matrice : cf. §11.9.

**L5 — Streaming incrémental `/v1/responses`** *(2 j)* — traite A11, A12
Émettre les vrais événements Responses (`response.created`, `output_item.added`, `output_text.delta`, `reasoning_summary_text.delta`, `function_call_arguments.delta`, `output_item.done`, `response.completed` avec `usage`) pour P5/P6, en dérivant des deltas chat/Anthropic déjà consommés. `test_responses_stream_contract.py` : ordre contractuel, un delta par fragment, usage final, `err_stream` gardant un terminal cohérent. À faire **après** L2/L3 (mêmes zones de `opencode.py`).

**L6 — Goldens étendus + gate doc** *(1 j)*
`gen_golden_fixtures.py` : ~11 → ~35 fixtures (une par case de matrice). Mise à jour `docs/conversion-matrix.md` (6 chemins × 15 axes, pertes assumées) et `docs/_drift_manifest.json` : `min_count` relevé, `must_contain` complété des fonctions non listées (`anthropic_to_openai_responses`, `openai_chat_to_responses`, `_responses_to_chat_response`, `_responses_to_anthropic_response`, `sanitize_tool_names`). Le gate `test_docs_drift` lie code ↔ doc dans les deux sens : une fonction non documentée casse le CI.

**L7 — E2E & corpus** *(1,5 j)* — V3, V4 — **TERMINÉ**
`test_e2e_protocol_matrix.py` : 6 chemins × stream/non-stream avec capture du corps amont + corpus round-trip. Inclut les sous-chemins free-model et failover sur P2 stream et P4 stream.

*État final* : 26 fonctions → **34 cas collectés**, **tous verts** (les 3 cas
P4-stream sont désormais de **simples tests de régression** : A24 est corrigée,
les marqueurs `xfail(strict=True)` ont été **retirés**), plus les 5 verrous
`test_l13_*` ajoutés par le lot L13. Suite complète hors
`docker` : `1966 passed, 1 skipped, 23 deselected, 1 xfailed` — **`GATE OK`,
exit 0** (chiffres du dernier passage de `scripts/gate.ps1` ; le `1 xfailed` est
l'écart A8 résiduel déclaré, voir §11.9). Écart au texte du
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

## 6. Matrice de couverture cible (15 axes × 6 chemins)

| Axe | P1 | P2 | P3 | P4 | P5 | P6 |
|---|---|---|---|---|---|---|
| effort entrée | natif | `_effort_to_reasoning` + caps | **4ᵉ mapping, sans caps** | dict en dur (A1/A2) | natif `reasoning.effort` | **perdu (A3)** |
| budget thinking↔niveau | natif | ratio 16000/10000/4000 | par famille | 4096/10000/16000 | idem | absent |
| plancher max_tokens | oui | oui | **non (A4)** | oui | oui | oui |
| thinking multi-tours | strip synthétique | `reasoning_content` | passthrough | `_local_signature` | résumé `reasoning` droppé | droppé assumé |
| `redacted_thinking` | natif | cache borné 512 | passthrough | cache borné | — | — |
| ordre blocs réponse | natif | golden | passthrough | à tester | à tester | à tester |
| `cache_control` messages | natif | partiel | passthrough | n/a | partiel | n/a |
| `cache_control` tools | natif | **perdu (A5)** | passthrough | n/a | — | — |
| usage cache (read/creation) | natif | 7 sites, **0 test (A6)** | passthrough | à tester | partiel | partiel |
| tools schéma/strict | natif | `_normalize_tool_schema` | passthrough | profils | `_sanitize` | profils |
| tool_choice | natif | golden | passthrough | dict↔str | dict↔str | dict↔str |
| noms d'outils longs | natif | sanitize/restore | passthrough | sanitize/restore | remap historique | remap |
| orphelins tool_result | — | `_drop_orphan_tool_messages` | idem | idem | `_drop_orphan_responses_input` | idem |
| documents (PDF/URL/file_id) | natif | `{type:file}` + replis | passthrough | PDF-URL seul | `input_file` | `input_file` |
| images (+ tool_result) | natif | data-URI + drops | passthrough | data-URI | `input_image` | `input_image` |
| streaming incrémental | natif | `openai_stream` | natif | `_anthro_to_oai_stream` | **non (A11)** | **non (A11)** |

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

**Noms d'outils** : la référence API Anthropic donne `tool_use.name` en `maxLength: 200, minLength: 1` et **sans restriction de charset** (seul `tool_use.id` est contraint à `^[a-zA-Z0-9_-]+$`) ; LiteLLM cite pour sa part `^[a-zA-Z0-9_-]{1,128}$`. Dans tous les cas la limite Anthropic est **supérieure** aux 64 caractères d'OpenAI : le sanitize ne se justifie donc que **vers** OpenAI/Responses, jamais vers Anthropic. LiteLLM construit une **forward map par requête (original → sanitize)** parce qu'un sanitize naif est *lossy* : `foo/bar` et `foo_bar` se replieraient tous deux sur `foo_bar`, provoquant soit un 400 (doublon), soit une **mauvaise traduction retour** (restaurer `foo_bar` vers `foo/bar` alors que l'appelant avait reellement enregistre `foo_bar`). Notre sanitize par hachage doit etre verifie sur ce cas precis (A8).

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
| **L7** | **A24**, **A25** | P4-stream ne renvoie plus 0 octet (A24) ; nom de fichier des documents plus perdu (A25) | `test_e2e_protocol_matrix.py` (**34**), `test_documents_contract.py` (**17**) |
| **L13** | B1, B4 | Contrat `reasoning_content` mesuré + repli « retry-once » sur `/chat/completions` (voir §11.9) | `test_e2e_protocol_matrix.py::test_l13_*` (5) |
| **L4** *(partiel)* | A9, **résidu A8** | Matrice outillage des 6 chemins ; écart A8 (nom > 64 vers Chat sur P2) **déclaré** et suivi par `xfail(strict=True)` | `test_tools_matrix.py` (8 fonctions → **25** cas) |

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
- **⚠️ Écart trouvé en vérifiant B4 (résidu A8, NON corrigé, déclaré)** : le
  volet B4 portait sur la **restauration** au retour, pas sur la **sanitize**
  à l'aller — et cette dernière est **asymétrique entre chemins**.
  `sanitize_tool_names` n'est appelé que sur les chemins **Responses**
  (`_sanitize_native_responses_request` L3061, `_chat_to_responses_request`
  L3410). Sur les chemins **Chat**, un nom d'outil > 64 caractères part **tel
  quel** vers l'amont : mesuré, un nom de 80 car. arrive à **80 car.** côté Chat
  (`anthropic_to_openai` ne pose aucun `_tool_name_map`). Le sens P4 vers
  Anthropic est correct (limite 200 : un nom de 80 passe légitimement).
  **Risque** : un upstream Chat strict peut rejeter en 400. **Pourquoi non
  corrigé ici** : la correction demande de plomber `name_map` dans
  `anthropic_to_openai`/`openai_to_anthropic` et de le faire remonter au
  handler — c'est le périmètre du **lot L4** (`test_tools_matrix.py`, qui
  **existe** désormais), pas celui du volet B4. La perte est
  **déclarée** (§7.2 du rapport) plutôt que silencieuse. *Aucun verrou* : c'est
  précisément ce que L4 doit apporter.
- **Vérification en conditions réelles — PARTIELLEMENT CONCLUANTE.** Le proxy en
  cours sur `127.0.0.1:4000` est joignable et a reçu de vraies requêtes. Sur
  `muse-spark-1.3-contributor` et `mimo-v2.5` (P2), des **`200`** ont été
  obtenus. Résultat exploitable, après vérification des dumps du proxy :
  - ✅ **Sur P2 vers Chat, le nom de 80 caractères part NON sanitizé** vers un vrai
    amont — observé dans `logs/free400_msg_aa3ac6d9fd40-31e.json`. La première
    moitié de A8 est donc **prouvée sur le fil**, plus seulement hors ligne.
  - ✅ **Sur la jambe Responses, la sanitize à 64 fonctionne en production** : le
    nom émis est `a`×57 + `-86f336`, digest `sha1("a"×80)[:6]` recalculé
    **identique** à la sortie de `sanitize_tool_names`.
  - ⚠️ **Piège de lecture, corrigé** : les `200` ne prouvent **pas** qu'un amont
    accepte 80 caractères — sur cette jambe le nom était **sanitizé à 64**.
  - ❌ **Non établi** : qu'un amont **rejette** un nom > 64. Les échecs observés
    (`401 CreditsError` payant, `400 MissingSessionID` free, `403 DataPolicyError`
    d'opt-in workspace) ont d'autres causes, et la cible est **intermittente**
    (mêmes modèles : `200` puis `503`). L'écart A8 reste donc **déclaré comme
    risque** pour son volet « rejet ». Détail mesuré : §9 du rapport.

### 11.10 Reste à faire

- **L4 (résidu)** — `tests/test_tools_matrix.py` est désormais **livré**
  (25 cas, 6 chemins). Le volet documents reste couvert par
  `tests/test_documents_contract.py`, pas par un `test_documents_matrix.py`
  séparé. Le seul point **non corrigé** est l'écart **A8 résiduel** décrit
  ci-dessus (nom d'outil > 64 caractères vers une cible Chat sur **P2**) :
  la matrice le porte en `xfail(strict=True)`, donc il est **suivi par la suite
  de tests** et non enfoui. Corriger demande de sanitizer à l'aller, de faire
  remonter la `name_map` au handler et de restaurer sur les voies de retour
  non-stream **et** streaming.
- **`docs/conversion-matrix.md`** — ✅ l'écart **A8 résiduel est désormais reporté**
  dans ce tableau (bloc « Pertes déclarées sur l'axe des noms d'outils », avec la
  preuve sur le fil et le digest vérifié). ⚠️ **Reste à faire** : la mise à jour de
  la ligne « PERDU silencieusement » pour l'**image**, demandée par L4 — cette
  ligne n'existe pas dans `docs/conversion-matrix.md` tel qu'il est aujourd'hui
  (grep : aucune occurrence de « PERDU »), donc soit elle vit dans un autre
  document, soit elle n'a jamais été écrite. À clarifier avant de considérer L4
  comme clos.
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
